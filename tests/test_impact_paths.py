from __future__ import annotations

from pathlib import Path

import pytest

from code_scout.impact.git import (
    GitComparison,
    GitRepositoryIdentity,
    GitRevisionIdentity,
)
from code_scout.impact.mapping import (
    ChangedFileSeed,
    ChangedSymbolSeed,
    ChangeSeedResult,
    MappingSide,
    MappingStatus,
)
from code_scout.impact.paths import ImpactCertainty, trace_impact_paths
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.storage import open_storage, upsert_active_facts
from code_scout.workspace.status import WorkspaceStatusReport


def _status(
    repository: Path, *, relationship_state: str = "observed"
) -> WorkspaceStatusReport:
    return WorkspaceStatusReport(
        workspace={
            "id": "workspace",
            "root_path": str(repository),
            "repositories": [{"id": "workspace", "path": "."}],
        },
        index={"state": "ready", "database_path": "unused"},
        freshness={
            "state": "stale",
            "coverage": "complete",
            "coverage_reasons": ["working tree differs from the index"],
        },
        changes={},
        coverage={
            "relationship_extraction": {
                "state": relationship_state,
                "relationship_count": 1 if relationship_state == "observed" else 0,
            }
        },
    )


def _change_seeds(repository: Path) -> ChangeSeedResult:
    identity = GitRepositoryIdentity("workspace", "Workspace", ".", str(repository))
    comparison = GitComparison(
        mode="staged",
        old=GitRevisionIdentity("HEAD", "commit", "a" * 40),
        new=GitRevisionIdentity("index", "index", None),
    )
    return ChangeSeedResult(
        repository=identity,
        comparison=comparison,
        index_state="ready",
        freshness={
            "state": "stale",
            "coverage": "complete",
            "coverage_reasons": ["working tree differs from the index"],
        },
        files=(
            ChangedFileSeed(
                repository=identity,
                old_path="src/changed.py",
                new_path="src/changed.py",
                change_kind="modified",
                binary=False,
                old_ranges=(),
                new_ranges=(),
                mapping_status=MappingStatus.MAPPED,
                mapping_side=MappingSide.NEW,
                mapping_reason=None,
                file_id="file:changed",
                indexed_path="src/changed.py",
                indexed_content_hash="hash",
            ),
        ),
        symbols=(
            ChangedSymbolSeed(
                repository=identity,
                path="src/changed.py",
                file_id="file:changed",
                symbol_id="symbol:changed",
                name="changed",
                kind="function",
                qualified_name="changed",
                start_line=1,
                end_line=3,
                changed_ranges=(),
                mapping_status=MappingStatus.MAPPED,
                mapping_side=MappingSide.NEW,
            ),
        ),
        coverage_gaps=(),
    )


def _file_change_seeds(
    repository: Path, *, path: str, file_id: str
) -> ChangeSeedResult:
    result = _change_seeds(repository)
    return ChangeSeedResult(
        repository=result.repository,
        comparison=result.comparison,
        index_state=result.index_state,
        freshness=result.freshness,
        files=(
            ChangedFileSeed(
                repository=result.repository,
                old_path=path,
                new_path=path,
                change_kind="modified",
                binary=False,
                old_ranges=(),
                new_ranges=(),
                mapping_status=MappingStatus.MAPPED,
                mapping_side=MappingSide.NEW,
                mapping_reason=None,
                file_id=file_id,
                indexed_path=path,
                indexed_content_hash="hash",
            ),
        ),
        symbols=(),
        coverage_gaps=(),
    )


def _entity(
    kind: str, identifier: str, path: str, name: str | None = None
) -> ActiveFact:
    properties: dict[str, object]
    if kind == "file":
        properties = {
            "repository_id": "workspace",
            "path": path,
            "kind": "source",
            "extension": Path(path).suffix,
            "language": "Python",
            "content_hash": "hash",
        }
    else:
        properties = {
            "file_id": f"file:{identifier.removeprefix('symbol:')}",
            "name": name or identifier,
            "kind": "function",
            "qualified_name": name or identifier,
            "start_line": 1,
            "end_line": 3,
        }
    return ActiveFact(
        id=f"fact:{kind}:{identifier}",
        extractor="tree_sitter",
        kind=kind,
        source_file=path,
        identity={"entity_type": kind, "entity_id": identifier},
        properties=properties,
        evidence=(FactEvidence(path, "tree_sitter", 0.9, 1, 3),),
        confidence=0.9,
    )


def _edge(
    identifier: str,
    source_type: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str,
    *,
    extractor: str = "lsp",
    confidence: float = 0.9,
    metadata: dict[str, object] | None = None,
    path: str = "src/caller.py",
) -> ActiveFact:
    return ActiveFact(
        id=f"fact:edge:{identifier}",
        extractor=extractor,
        kind="relationship",
        source_file=path,
        identity={
            "subject": {"type": source_type, "id": source_id},
            "relation": relation,
            "object": {"type": target_type, "id": target_id},
        },
        properties={},
        evidence=(FactEvidence(path, extractor, confidence, 5, 5, metadata or {}),),
        confidence=confidence,
    )


def _service(identifier: str, name: str, path: str) -> ActiveFact:
    return ActiveFact(
        id=f"fact:service:{identifier}",
        extractor="dotnet",
        kind="service",
        source_file=path,
        identity={"entity_type": "service", "entity_id": identifier},
        properties={"name": name, "repository_id": "workspace"},
        evidence=(FactEvidence(path, "dotnet", 0.95, 1, 1),),
        confidence=0.95,
    )


def _contract(identifier: str, name: str, path: str) -> ActiveFact:
    return ActiveFact(
        id=f"fact:contract:{identifier}",
        extractor="dotnet",
        kind="contract",
        source_file=path,
        identity={"entity_type": "contract", "entity_id": identifier},
        properties={"name": name, "kind": "rpc", "path": path},
        evidence=(FactEvidence(path, "dotnet", 0.95, 2, 2),),
        confidence=0.95,
    )


def _seed_graph(repository: Path, facts: list[ActiveFact]) -> None:
    repository.mkdir()
    (repository / "src").mkdir()
    for path in {
        fact.source_file for fact in facts if fact.source_file.startswith("src/")
    }:
        (repository / path).write_text("# fixture\n" * 10, encoding="utf-8")
    with open_storage(repository) as connection:
        upsert_active_facts(connection, facts)
        connection.commit()


def _base_entities() -> list[ActiveFact]:
    return [
        _entity("file", "file:changed", "src/changed.py"),
        _entity("symbol", "symbol:changed", "src/changed.py", "changed"),
    ]


def test_traces_direct_and_transitive_callers_in_reverse_direction_and_stops_cycles(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _entity("file", "file:caller", "src/caller.py"),
        _entity("symbol", "symbol:caller", "src/caller.py", "caller"),
        _entity("file", "file:outer", "src/outer.py"),
        _entity("symbol", "symbol:outer", "src/outer.py", "outer"),
        _edge("direct", "symbol", "symbol:caller", "calls", "symbol", "symbol:changed"),
        _edge(
            "transitive",
            "symbol",
            "symbol:outer",
            "calls",
            "symbol",
            "symbol:caller",
            extractor="heuristic",
            path="src/outer.py",
        ),
        _edge(
            "cycle",
            "symbol",
            "symbol:changed",
            "calls",
            "symbol",
            "symbol:outer",
            path="src/changed.py",
        ),
        # This outgoing edge proves direction: a callee of changed is not affected.
        _edge(
            "wrong-way",
            "symbol",
            "symbol:changed",
            "calls",
            "symbol",
            "symbol:callee",
            path="src/changed.py",
        ),
    ]
    _seed_graph(repository, facts)

    result = trace_impact_paths(
        repository, _change_seeds(repository), _status(repository)
    )
    symbol_paths = [path for path in result.paths if path.changed_seed.type == "symbol"]

    assert [path.affected_entity.id for path in symbol_paths] == [
        "symbol:caller",
        "symbol:outer",
    ]
    assert [len(path.relationships) for path in symbol_paths] == [1, 2]
    assert symbol_paths[0].certainty is ImpactCertainty.CONFIRMED
    assert symbol_paths[1].certainty is ImpactCertainty.INFERRED
    assert symbol_paths[1].relationships[0].dependency.id == "symbol:changed"
    assert symbol_paths[1].relationships[1].dependent.id == "symbol:outer"
    assert "symbol:callee" not in {path.affected_entity.id for path in result.paths}
    assert symbol_paths[0].evidence_ids[0].startswith("relationship:")
    assert symbol_paths[0].citations[0].path == "src/caller.py"
    assert symbol_paths[0].citations[0].start_line == 5
    assert symbol_paths[0].extractors == ("lsp",)
    assert symbol_paths[0].freshness_limitations == (
        "working tree differs from the index",
        "Index freshness is stale.",
    )


def test_transitive_path_requires_each_edge_to_be_corroborated(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _entity("file", "file:caller", "src/caller.py"),
        _entity("symbol", "symbol:caller", "src/caller.py", "caller"),
        _entity("file", "file:outer", "src/outer.py"),
        _entity("symbol", "symbol:outer", "src/outer.py", "outer"),
        _edge(
            "corroborated",
            "symbol",
            "symbol:caller",
            "calls",
            "symbol",
            "symbol:changed",
            metadata={"validation": "corroborated"},
        ),
        _edge(
            "unknown",
            "symbol",
            "symbol:outer",
            "calls",
            "symbol",
            "symbol:caller",
            extractor="heuristic",
            path="src/outer.py",
        ),
    ]
    _seed_graph(repository, facts)

    result = trace_impact_paths(
        repository, _change_seeds(repository), _status(repository)
    )
    paths = {path.affected_entity.id: path for path in result.paths}

    assert paths["symbol:caller"].assessment.validation.value == "corroborated"
    assert paths["symbol:outer"].assessment.validation.value == "unknown"


def test_traces_file_import_dependents_and_respects_max_hops(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _entity("file", "file:importer", "src/importer.py"),
        _entity("file", "file:top", "src/top.py"),
        _edge(
            "import",
            "file",
            "file:importer",
            "imports",
            "file",
            "file:changed",
            extractor="graphify",
            path="src/importer.py",
        ),
        _edge(
            "depends",
            "file",
            "file:top",
            "depends_on",
            "file",
            "file:importer",
            extractor="graphify",
            path="src/top.py",
        ),
    ]
    _seed_graph(repository, facts)

    one_hop = trace_impact_paths(
        repository,
        _change_seeds(repository),
        _status(repository),
        max_hops=1,
    )
    file_paths = [path for path in one_hop.paths if path.changed_seed.type == "file"]

    assert [path.affected_entity.id for path in file_paths] == ["file:importer"]
    assert file_paths[0].relationships[0].relation == "imports"


def test_conflicting_evidence_is_ambiguous_and_results_are_deterministic_and_truncated(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = _base_entities()
    for name in ("zeta", "alpha", "beta"):
        facts.extend(
            [
                _entity("file", f"file:{name}", f"src/{name}.py"),
                _entity("symbol", f"symbol:{name}", f"src/{name}.py", name),
                _edge(
                    name,
                    "symbol",
                    f"symbol:{name}",
                    "calls",
                    "symbol",
                    "symbol:changed",
                    metadata={"resolver_conflicts": ({"field": "target"},)}
                    if name == "alpha"
                    else None,
                    path=f"src/{name}.py",
                ),
            ]
        )
    _seed_graph(repository, facts)
    seeds = _change_seeds(repository)
    status = _status(repository)

    first = trace_impact_paths(repository, seeds, status, max_results=2)
    second = trace_impact_paths(repository, seeds, status, max_results=2)
    symbol_paths = [path for path in first.paths if path.changed_seed.type == "symbol"]

    assert first.to_dict() == second.to_dict()
    assert first.truncated is True
    assert [path.affected_entity.id for path in symbol_paths] == [
        "symbol:alpha",
        "symbol:beta",
    ]
    assert symbol_paths[0].certainty is ImpactCertainty.AMBIGUOUS


def test_absent_relationship_coverage_reports_gaps_without_claiming_safety(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _seed_graph(repository, _base_entities())

    result = trace_impact_paths(
        repository,
        _change_seeds(repository),
        _status(repository, relationship_state="unknown"),
    )

    assert result.paths == ()
    assert {gap.code for gap in result.coverage_gaps} >= {
        "no_supported_impact_path",
        "relationship_coverage_unavailable",
    }
    assert all("safe" in gap.message for gap in result.coverage_gaps)


def test_categorizes_only_reachable_contracts_and_services_with_cited_paths(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _contract("contract:orders", "Orders", "src/changed.py"),
        _service("service:orders", "Orders Service", "src/orders.py"),
        _contract("contract:billing", "Billing", "src/billing.py"),
        _service("service:billing", "Billing Service", "src/billing.py"),
        _edge(
            "changed-contract",
            "file",
            "file:changed",
            "declares_contract",
            "contract",
            "contract:orders",
            extractor="dotnet",
            path="src/changed.py",
        ),
        _edge(
            "orders-service",
            "service",
            "service:orders",
            "produces",
            "contract",
            "contract:orders",
            extractor="dotnet",
            path="src/orders.py",
        ),
        _edge(
            "billing-service",
            "service",
            "service:billing",
            "produces",
            "contract",
            "contract:billing",
            extractor="dotnet",
            path="src/billing.py",
        ),
    ]
    _seed_graph(repository, facts)

    result = trace_impact_paths(
        repository, _change_seeds(repository), _status(repository)
    )

    assert [path.affected_entity.id for path in result.affected_contracts] == [
        "contract:orders"
    ]
    assert [path.affected_entity.id for path in result.affected_services] == [
        "service:orders"
    ]
    assert result.affected_contracts[0].changed_seed.path == "src/changed.py"
    assert result.affected_contracts[0].citations[0].path == "src/changed.py"
    assert [step.relation for step in result.affected_services[0].relationships] == [
        "declares_contract",
        "produces",
    ]
    assert result.category_coverage["contracts"]["state"] == "observed"
    assert result.category_coverage["services"]["state"] == "observed"


def test_changed_contract_reaches_consuming_service_and_file_in_reverse_direction(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _contract("contract:orders", "Orders", "src/changed.py"),
        _service("service:consumer", "Consumer Service", "src/consumer.py"),
        _entity("file", "file:client", "src/client.py"),
        _contract("contract:unrelated", "Unrelated", "src/unrelated.py"),
        _service("service:unrelated", "Unrelated Service", "src/unrelated.py"),
        _edge(
            "changed-contract",
            "file",
            "file:changed",
            "declares_contract",
            "contract",
            "contract:orders",
            extractor="dotnet",
            path="src/changed.py",
        ),
        _edge(
            "service-consumer",
            "service",
            "service:consumer",
            "consumes",
            "contract",
            "contract:orders",
            extractor="heuristic",
            confidence=0.8,
            path="src/consumer.py",
        ),
        _edge(
            "file-consumer",
            "file",
            "file:client",
            "consumes",
            "contract",
            "contract:orders",
            extractor="frontend_api",
            confidence=0.82,
            path="src/client.py",
        ),
        _edge(
            "unrelated-consumer",
            "service",
            "service:unrelated",
            "consumes",
            "contract",
            "contract:unrelated",
            extractor="heuristic",
            path="src/unrelated.py",
        ),
    ]
    _seed_graph(repository, facts)
    seeds = _change_seeds(repository)
    status = _status(repository)

    first = trace_impact_paths(repository, seeds, status)
    second = trace_impact_paths(repository, seeds, status)

    assert first.to_dict() == second.to_dict()
    assert first.truncated is False
    assert [path.affected_entity.id for path in first.affected_services] == [
        "service:consumer"
    ]
    assert "file:client" in {path.affected_entity.id for path in first.paths}
    assert sum(path.affected_entity.id == "file:client" for path in first.paths) == 1
    assert "service:unrelated" not in {path.affected_entity.id for path in first.paths}

    service_path = first.affected_services[0]
    assert [step.relation for step in service_path.relationships] == [
        "declares_contract",
        "consumes",
    ]
    assert service_path.relationships[1].dependency.id == "contract:orders"
    assert service_path.relationships[1].dependent.id == "service:consumer"
    assert service_path.relationships[1].source.id == "service:consumer"
    assert service_path.relationships[1].target.id == "contract:orders"
    assert service_path.certainty is ImpactCertainty.INFERRED
    assert len(service_path.evidence_ids) == 2
    assert service_path.extractors == ("dotnet", "heuristic")
    assert [citation.path for citation in service_path.citations] == [
        "src/changed.py",
        "src/consumer.py",
    ]

    file_path = next(
        path for path in first.paths if path.affected_entity.id == "file:client"
    )
    assert [step.relation for step in file_path.relationships] == [
        "declares_contract",
        "consumes",
    ]
    assert file_path.relationships[1].source.id == "file:client"
    assert file_path.relationships[1].target.id == "contract:orders"
    assert file_path.certainty is ImpactCertainty.INFERRED
    assert file_path.extractors == ("dotnet", "frontend_api")
    assert file_path.freshness_limitations == (
        "working tree differs from the index",
        "Index freshness is stale.",
    )


def test_changed_consumer_still_reaches_consumed_contract_forward(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = [
        _entity("file", "file:client", "src/client.py"),
        _contract("contract:orders", "Orders", "src/orders.py"),
        _edge(
            "file-consumer",
            "file",
            "file:client",
            "consumes",
            "contract",
            "contract:orders",
            extractor="frontend_api",
            path="src/client.py",
        ),
    ]
    _seed_graph(repository, facts)

    result = trace_impact_paths(
        repository,
        _file_change_seeds(repository, path="src/client.py", file_id="file:client"),
        _status(repository),
        max_hops=1,
    )

    assert [path.affected_entity.id for path in result.affected_contracts] == [
        "contract:orders"
    ]
    relationship = result.affected_contracts[0].relationships[0]
    assert relationship.dependency.id == "file:client"
    assert relationship.dependent.id == "contract:orders"
    assert relationship.source.id == "file:client"
    assert relationship.target.id == "contract:orders"


def test_evidence_linked_tests_retain_strength_freshness_and_are_deduplicated(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _entity("file", "file:test", "tests/test_changed.py"),
        _edge(
            "declaration",
            "file",
            "file:changed",
            "declares_symbol",
            "symbol",
            "symbol:changed",
            extractor="tree_sitter",
            path="src/changed.py",
        ),
        _edge(
            "test-import",
            "file",
            "file:test",
            "imports",
            "file",
            "file:changed",
            extractor="graphify",
            path="tests/test_changed.py",
        ),
    ]
    _seed_graph(repository, facts)

    result = trace_impact_paths(
        repository, _change_seeds(repository), _status(repository)
    )

    assert len(result.test_candidates) == 1
    candidate = result.test_candidates[0]
    assert candidate.affected_entity.id == "file:test"
    assert candidate.certainty is ImpactCertainty.CONFIRMED
    assert candidate.citations[0].path == "tests/test_changed.py"
    assert candidate.freshness_limitations == (
        "working tree differs from the index",
        "Index freshness is stale.",
    )
    assert result.inferred_test_candidates == ()
    assert result.category_coverage["tests"]["state"] == "observed"


def test_infers_separately_labeled_test_candidate_without_graph_edges(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    facts = [
        *_base_entities(),
        _entity("file", "file:inferred-test", "tests/test_changed.py"),
        _entity("file", "file:unrelated-test", "tests/test_other.py"),
    ]
    _seed_graph(repository, facts)

    result = trace_impact_paths(
        repository, _change_seeds(repository), _status(repository)
    )

    assert result.test_candidates == ()
    assert len(result.inferred_test_candidates) == 1
    inferred = result.inferred_test_candidates[0]
    assert inferred.changed_source.path == "src/changed.py"
    assert inferred.candidate.path == "tests/test_changed.py"
    assert "'test_' filename marker" in inferred.heuristic
    assert "relationships" not in result.to_dict()["inferred_test_candidates"][0]
    assert result.category_coverage["tests"]["state"] == "unavailable"


def test_category_coverage_is_unavailable_separately_from_supported_empty(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _seed_graph(repository, _base_entities())

    result = trace_impact_paths(
        repository,
        _change_seeds(repository),
        _status(repository, relationship_state="unknown"),
    )

    assert result.affected_contracts == ()
    assert result.affected_services == ()
    assert result.test_candidates == ()
    assert {
        name: value["state"] for name, value in result.category_coverage.items()
    } == {
        "contracts": "unavailable",
        "services": "unavailable",
        "tests": "unavailable",
    }
    assert {gap.code for gap in result.coverage_gaps} >= {
        "contracts_relationship_coverage_unavailable",
        "services_relationship_coverage_unavailable",
        "tests_relationship_coverage_unavailable",
    }


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("max_hops", 0, "max_hops must be between 1 and 5"),
        ("max_hops", 6, "max_hops must be between 1 and 5"),
        ("max_results", 0, "max_results must be between 1 and 1000"),
        ("max_results", 1001, "max_results must be between 1 and 1000"),
    ],
)
def test_rejects_out_of_range_bounds(
    tmp_path: Path, option: str, value: int, message: str
) -> None:
    arguments = {option: value}
    with pytest.raises(ValueError, match=message):
        trace_impact_paths(
            tmp_path,
            _change_seeds(tmp_path),
            _status(tmp_path),
            **arguments,
        )
