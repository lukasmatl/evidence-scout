"""Bounded, evidence-cited reverse-dependency traversal for changed code."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from code_scout.domain.normalize import NormalizedGraphFacts
from code_scout.evidence.assessment import (
    EvidenceAssessment,
    assess_legacy_evidence,
    combine_assessments,
    combine_path_assessments,
)
from code_scout.impact.mapping import (
    ChangedFileSeed,
    ChangedSymbolSeed,
    ChangeSeedResult,
    CoverageGap,
    load_resolved_graph_read_only,
)
from code_scout.models.graph import Edge, Evidence, File, Symbol
from code_scout.storage import database_path
from code_scout.workspace.status import WorkspaceStatusReport

_DEPENDENCY_RELATIONS = frozenset(
    {
        "calls",
        "depends_on",
        "imports",
        "references_project",
    }
)
_FORWARD_IMPACT_RELATIONS = frozenset(
    {
        "declares_contract",
        "injects",
    }
)
_CONSUMER_CONTRACT_RELATIONS = frozenset({"consumes"})
_SERVICE_CONTRACT_RELATIONS = frozenset({"exposes", "produces"})
_CONFIRMED_EXTRACTORS = frozenset({"dotnet", "graphify", "lsp"})
_STRENGTH = {
    "unavailable": 0,
    "ambiguous": 1,
    "inferred": 2,
    "confirmed": 3,
}


class ImpactCertainty(StrEnum):
    """Strength of the persisted evidence supporting an impact path."""

    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SourceCitation:
    repository_id: str
    path: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class ImpactEntity:
    type: str
    id: str
    name: str
    path: str | None
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class ImpactSeed:
    type: str
    id: str
    name: str
    path: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class ImpactRelationship:
    """One stored edge, ordered from changed dependency to its dependent."""

    id: str
    dependency: ImpactEntity
    dependent: ImpactEntity
    relation: str
    source: ImpactEntity
    target: ImpactEntity
    certainty: ImpactCertainty
    assessment: EvidenceAssessment
    labels: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    citations: tuple[SourceCitation, ...]
    extractors: tuple[str, ...]


@dataclass(frozen=True)
class ImpactPath:
    changed_seed: ImpactSeed
    affected_entity: ImpactEntity
    relationships: tuple[ImpactRelationship, ...]
    certainty: ImpactCertainty
    assessment: EvidenceAssessment
    labels: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    citations: tuple[SourceCitation, ...]
    extractors: tuple[str, ...]
    freshness: dict[str, Any]
    freshness_limitations: tuple[str, ...]


@dataclass(frozen=True)
class InferredTestCandidate:
    """An indexed test suggested by convention, without a fabricated graph path."""

    changed_source: ImpactSeed
    candidate: ImpactEntity
    heuristic: str
    freshness: dict[str, Any]
    freshness_limitations: tuple[str, ...]


@dataclass(frozen=True)
class ImpactPathResult:
    paths: tuple[ImpactPath, ...]
    affected_contracts: tuple[ImpactPath, ...]
    affected_services: tuple[ImpactPath, ...]
    test_candidates: tuple[ImpactPath, ...]
    inferred_test_candidates: tuple[InferredTestCandidate, ...]
    category_coverage: dict[str, dict[str, Any]]
    coverage_gaps: tuple[CoverageGap, ...]
    truncated: bool
    max_hops: int
    max_results: int
    freshness: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trace_impact_paths(
    workspace: str | Path,
    change_seeds: ChangeSeedResult,
    status: WorkspaceStatusReport,
    *,
    max_hops: int = 2,
    max_results: int = 100,
) -> ImpactPathResult:
    """Trace callers and module dependents using only persisted graph evidence."""
    _validate_bounds(max_hops, max_results)
    root = Path(workspace).expanduser().resolve()
    graph = load_resolved_graph_read_only(root, status)
    gaps = list(change_seeds.coverage_gaps)
    if graph is None:
        gaps.append(
            CoverageGap(
                code="relationship_index_unavailable",
                message=(
                    "The resolved index is unavailable; dependency impact was not "
                    "evaluated and safety cannot be inferred."
                ),
                repository_id=change_seeds.repository.id,
                path="",
            )
        )
        return _result(
            (),
            (),
            gaps,
            False,
            max_hops,
            max_results,
            change_seeds,
            _unavailable_category_coverage("The resolved index is unavailable."),
        )

    nodes = _node_index(graph)
    relationship_ids = _load_relationship_ids_read_only(root)
    adjacency = _impact_adjacency(graph.edges)
    seeds = _impact_seeds(change_seeds)
    paths: list[ImpactPath] = []
    truncated = False

    for seed in seeds:
        queue: deque[tuple[tuple[str, str], tuple[ImpactRelationship, ...]]] = deque(
            [((seed.type, seed.id), ())]
        )
        visited = {(seed.type, seed.id)}
        seed_found = False
        while queue:
            current, current_path = queue.popleft()
            if len(current_path) >= max_hops:
                continue
            for edge, dependent_key in adjacency.get(current, ()):
                if dependent_key in visited:
                    continue
                visited.add(dependent_key)
                relationship = _impact_relationship(
                    root,
                    change_seeds.repository.id,
                    edge,
                    nodes,
                    relationship_ids,
                    current,
                    dependent_key,
                )
                relationship_path = (*current_path, relationship)
                queue.append((dependent_key, relationship_path))
                seed_found = True
                if len(paths) >= max_results:
                    truncated = True
                    break
                paths.append(
                    _impact_path(
                        seed,
                        relationship.dependent,
                        relationship_path,
                        change_seeds.freshness,
                    )
                )
            if truncated:
                break
        if not seed_found:
            gaps.append(
                CoverageGap(
                    code="no_supported_impact_path",
                    message=(
                        "No supported caller or dependency path was observed for "
                        f"{seed.id}; this is not evidence that the change is safe."
                    ),
                    repository_id=change_seeds.repository.id,
                    path=seed.path,
                )
            )
        if truncated:
            break

    category_coverage = _category_coverage(status, graph, nodes)
    for category, coverage in category_coverage.items():
        if coverage["state"] != "unavailable":
            continue
        gaps.append(
            CoverageGap(
                code=f"{category}_relationship_coverage_unavailable",
                message=str(coverage["reason"]),
                repository_id=change_seeds.repository.id,
                path="",
            )
        )

    relationship_coverage = status.coverage.get("relationship_extraction", {})
    if relationship_coverage.get("state") != "observed":
        gaps.append(
            CoverageGap(
                code="relationship_coverage_unavailable",
                message=(
                    "Relationship extraction coverage is not observed; an empty or "
                    "partial impact result cannot establish safety."
                ),
                repository_id=change_seeds.repository.id,
                path="",
            )
        )
    elif graph.edges and not adjacency:
        gaps.append(
            CoverageGap(
                code="supported_relationships_unavailable",
                message=(
                    "Stored relationships do not include supported caller, import, "
                    "or dependency relations; impact remains unknown."
                ),
                repository_id=change_seeds.repository.id,
                path="",
            )
        )
    if not seeds:
        gaps.append(
            CoverageGap(
                code="change_seeds_unavailable",
                message=(
                    "No exact indexed symbol or file seeds are available for "
                    "dependency traversal; safety cannot be inferred."
                ),
                repository_id=change_seeds.repository.id,
                path="",
            )
        )
    categorized = _categorized_paths(paths, nodes)
    inferred_tests = _inferred_test_candidates(
        graph,
        seeds,
        categorized["test_candidates"],
        change_seeds.freshness,
        nodes,
    )
    return _result(
        paths,
        inferred_tests,
        gaps,
        truncated,
        max_hops,
        max_results,
        change_seeds,
        category_coverage,
    )


def _validate_bounds(max_hops: int, max_results: int) -> None:
    if (
        not isinstance(max_hops, int)
        or isinstance(max_hops, bool)
        or not 1 <= max_hops <= 5
    ):
        raise ValueError("max_hops must be between 1 and 5")
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= 1000
    ):
        raise ValueError("max_results must be between 1 and 1000")


def _impact_seeds(change_seeds: ChangeSeedResult) -> tuple[ImpactSeed, ...]:
    seeds = [_symbol_seed(seed) for seed in change_seeds.symbols]
    seeds.extend(
        _file_seed(seed)
        for seed in change_seeds.files
        if seed.file_id is not None and seed.indexed_path is not None
    )
    return tuple(
        sorted(
            {(_seed.type, _seed.id): _seed for _seed in seeds}.values(),
            key=lambda seed: (seed.path, seed.start_line or 0, seed.type, seed.id),
        )
    )


def _symbol_seed(seed: ChangedSymbolSeed) -> ImpactSeed:
    return ImpactSeed(
        type="symbol",
        id=seed.symbol_id,
        name=seed.qualified_name or seed.name,
        path=seed.path,
        start_line=seed.start_line,
        end_line=seed.end_line,
    )


def _file_seed(seed: ChangedFileSeed) -> ImpactSeed:
    assert seed.file_id is not None and seed.indexed_path is not None
    return ImpactSeed(
        type="file",
        id=seed.file_id,
        name=seed.indexed_path,
        path=seed.indexed_path,
        start_line=None,
        end_line=None,
    )


def _impact_adjacency(
    edges: tuple[Edge, ...],
) -> dict[tuple[str, str], tuple[tuple[Edge, tuple[str, str]], ...]]:
    pending: dict[tuple[str, str], list[tuple[Edge, tuple[str, str]]]] = {}
    for edge in edges:
        source = (edge.source_type, edge.source_id)
        target = (edge.target_type, edge.target_id)
        relation = _normalized_relation(edge.relation_type)
        if _is_dependency_relation(relation):
            pending.setdefault(target, []).append((edge, source))
        elif relation == "declares_symbol":
            pending.setdefault(target, []).append((edge, source))
        elif relation in _FORWARD_IMPACT_RELATIONS:
            pending.setdefault(source, []).append((edge, target))
        elif relation in _CONSUMER_CONTRACT_RELATIONS:
            # Extractors persist consumer -> contract. Keep the forward path so a
            # changed consumer can still reveal the contract it uses, and add the
            # reverse dependency path so a changed contract reaches its consumer.
            pending.setdefault(source, []).append((edge, target))
            pending.setdefault(target, []).append((edge, source))
        elif relation in _SERVICE_CONTRACT_RELATIONS:
            pending.setdefault(source, []).append((edge, target))
            pending.setdefault(target, []).append((edge, source))
    return {
        key: tuple(sorted(values, key=lambda item: (*_edge_sort_key(item[0]), item[1])))
        for key, values in pending.items()
    }


def _is_dependency_relation(relation: str) -> bool:
    normalized = _normalized_relation(relation)
    return normalized in _DEPENDENCY_RELATIONS or normalized.endswith("_calls")


def _normalized_relation(relation: str) -> str:
    return relation.casefold().replace("-", "_").replace(" ", "_")


def _node_index(graph: NormalizedGraphFacts) -> dict[tuple[str, str], object]:
    groups = (
        ("repository", graph.repositories),
        ("file", graph.files),
        ("symbol", graph.symbols),
        ("service", graph.services),
        ("contract", graph.contracts),
        ("concept", graph.concepts),
        ("decision", graph.decisions),
    )
    return {(kind, item.id): item for kind, items in groups for item in items}


def _impact_relationship(
    root: Path,
    repository_id: str,
    edge: Edge,
    nodes: dict[tuple[str, str], object],
    relationship_ids: dict[tuple[str, str, str, str, str], str],
    dependency_key: tuple[str, str],
    dependent_key: tuple[str, str],
) -> ImpactRelationship:
    relationship_id = relationship_ids.get(_edge_key(edge), _relationship_id(edge))
    citations = _citations(root, repository_id, edge.evidence)
    extractors = tuple(
        sorted({item.extractor_name for item in edge.evidence if item.extractor_name})
    )
    assessment = combine_assessments(
        assess_legacy_evidence(
            item.metadata,
            extractor_provenance=(item.extractor_name,),
        )
        for item in edge.evidence
    )
    return ImpactRelationship(
        id=relationship_id,
        dependency=_entity(*dependency_key, nodes),
        dependent=_entity(*dependent_key, nodes),
        relation=edge.relation_type,
        source=_entity(edge.source_type, edge.source_id, nodes),
        target=_entity(edge.target_type, edge.target_id, nodes),
        certainty=_edge_certainty(edge),
        assessment=assessment,
        labels=assessment.labels,
        evidence_ids=tuple(
            f"{relationship_id}:evidence:{index}"
            for index, _item in enumerate(
                sorted(edge.evidence, key=_evidence_sort_key), start=1
            )
        ),
        citations=citations,
        extractors=extractors,
    )


def _entity(
    kind: str,
    identifier: str,
    nodes: dict[tuple[str, str], object],
) -> ImpactEntity:
    node = nodes.get((kind, identifier))
    if isinstance(node, Symbol):
        file = nodes.get(("file", node.file_id))
        path = file.path if isinstance(file, File) else None
        return ImpactEntity(
            kind,
            identifier,
            node.qualified_name or node.name,
            path,
            node.start_line,
            node.end_line,
        )
    if isinstance(node, File):
        return ImpactEntity(kind, identifier, node.path, node.path, None, None)
    name = getattr(node, "name", None) or getattr(node, "title", None) or identifier
    evidence = getattr(node, "evidence", ())
    primary = min(evidence, key=_evidence_sort_key) if evidence else None
    return ImpactEntity(
        kind,
        identifier,
        str(name),
        primary.file_path if primary is not None else None,
        primary.start_line if primary is not None else None,
        primary.end_line if primary is not None else None,
    )


def _edge_certainty(edge: Edge) -> ImpactCertainty:
    if not edge.evidence:
        return ImpactCertainty.UNAVAILABLE
    if any(_has_conflict(item.metadata) for item in edge.evidence):
        return ImpactCertainty.AMBIGUOUS
    classes = tuple(
        _evidence_certainty(item, edge.confidence) for item in edge.evidence
    )
    return max(classes, key=lambda item: _STRENGTH[item.value])


def _evidence_certainty(
    evidence: Evidence, fallback_confidence: float
) -> ImpactCertainty:
    extractor = evidence.extractor_name.casefold().replace("-", "_")
    if not extractor:
        return ImpactCertainty.UNAVAILABLE
    if extractor in _CONFIRMED_EXTRACTORS:
        return ImpactCertainty.CONFIRMED
    confidence = evidence.confidence
    if confidence is None:
        confidence = fallback_confidence
    if extractor in {"heuristic", "lexical"} and confidence < 0.5:
        return ImpactCertainty.AMBIGUOUS
    return ImpactCertainty.INFERRED


def _has_conflict(metadata: dict[str, Any]) -> bool:
    return bool(
        metadata.get("resolver_conflicts")
        or metadata.get("conflict")
        or metadata.get("ambiguous")
        or metadata.get("unresolved")
    )


def _impact_path(
    seed: ImpactSeed,
    affected: ImpactEntity,
    relationships: tuple[ImpactRelationship, ...],
    freshness: dict[str, Any],
) -> ImpactPath:
    certainty = min(
        (item.certainty for item in relationships),
        key=lambda item: _STRENGTH[item.value],
    )
    assessment = combine_path_assessments(
        (item.assessment for item in relationships),
        freshness=str(freshness.get("state", "unknown")),
    )
    return ImpactPath(
        changed_seed=seed,
        affected_entity=affected,
        relationships=relationships,
        certainty=certainty,
        assessment=assessment,
        labels=assessment.labels,
        evidence_ids=tuple(
            dict.fromkeys(
                evidence_id
                for relationship in relationships
                for evidence_id in relationship.evidence_ids
            )
        ),
        citations=tuple(
            dict.fromkeys(
                citation
                for relationship in relationships
                for citation in relationship.citations
            )
        ),
        extractors=tuple(
            sorted(
                {
                    extractor
                    for relationship in relationships
                    for extractor in relationship.extractors
                }
            )
        ),
        freshness=dict(freshness),
        freshness_limitations=_freshness_limitations(freshness),
    )


def _categorized_paths(
    paths: list[ImpactPath] | tuple[ImpactPath, ...],
    _nodes: dict[tuple[str, str], object],
) -> dict[str, tuple[ImpactPath, ...]]:
    contracts = _dedupe_paths(
        path
        for path in paths
        if path.affected_entity.type == "contract" and _has_cited_path(path)
    )
    services = _dedupe_paths(
        path
        for path in paths
        if path.affected_entity.type == "service" and _has_cited_path(path)
    )
    tests = _dedupe_paths(
        path
        for path in paths
        if path.affected_entity.path is not None
        and _is_test_path(path.affected_entity.path)
        and _has_cited_path(path)
        and any(
            _is_dependency_relation(relationship.relation)
            for relationship in path.relationships
        )
    )
    return {
        "affected_contracts": contracts,
        "affected_services": services,
        "test_candidates": tests,
    }


def _has_cited_path(path: ImpactPath) -> bool:
    return bool(path.relationships) and all(
        relationship.citations for relationship in path.relationships
    )


def _dedupe_paths(paths: Any) -> tuple[ImpactPath, ...]:
    selected: dict[tuple[str, str], ImpactPath] = {}
    for path in paths:
        key = (path.affected_entity.type, path.affected_entity.id)
        current = selected.get(key)
        if current is None or _path_preference(path) < _path_preference(current):
            selected[key] = path
    return tuple(sorted(selected.values(), key=_path_sort_key))


def _path_preference(path: ImpactPath) -> tuple[Any, ...]:
    return (
        -_STRENGTH[path.certainty.value],
        len(path.relationships),
        path.changed_seed.path,
        path.changed_seed.id,
    )


def _path_sort_key(path: ImpactPath) -> tuple[Any, ...]:
    return (
        path.affected_entity.path or "",
        path.affected_entity.name,
        path.affected_entity.type,
        path.affected_entity.id,
        *_path_preference(path),
    )


def _test_file_for_entity(
    entity: ImpactEntity,
    nodes: dict[tuple[str, str], object],
) -> File | None:
    node = nodes.get((entity.type, entity.id))
    if isinstance(node, File):
        return node if _is_test_path(node.path) else None
    if isinstance(node, Symbol):
        file = nodes.get(("file", node.file_id))
        return file if isinstance(file, File) and _is_test_path(file.path) else None
    return None


def _is_test_path(path: str) -> bool:
    parsed = PurePosixPath(path.replace("\\", "/"))
    directories = {part.casefold() for part in parsed.parts[:-1]}
    name = parsed.name.casefold()
    stem = parsed.stem.casefold()
    return bool(
        directories & {"test", "tests", "__tests__", "spec", "specs"}
        or any(directory.endswith(".tests") for directory in directories)
        or name.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith(".test")
        or stem.endswith(".spec")
        or parsed.stem.endswith(("Test", "Tests"))
    )


def _inferred_test_candidates(
    graph: NormalizedGraphFacts,
    seeds: tuple[ImpactSeed, ...],
    evidence_linked: tuple[ImpactPath, ...],
    freshness: dict[str, Any],
    nodes: dict[tuple[str, str], object],
) -> tuple[InferredTestCandidate, ...]:
    linked_sources = {path.changed_seed.path for path in evidence_linked}
    linked_files = {
        file.id
        for path in evidence_linked
        for file in (_test_file_for_entity(path.affected_entity, nodes),)
        if file is not None
    }
    candidates: dict[tuple[str, str], InferredTestCandidate] = {}
    for seed in seeds:
        if (
            seed.type != "file"
            or seed.path in linked_sources
            or _is_test_path(seed.path)
        ):
            continue
        source_stem = PurePosixPath(seed.path).stem.casefold()
        for file in graph.files:
            marker = _matching_test_marker(file.path, source_stem)
            if marker is None or file.id in linked_files:
                continue
            candidate = InferredTestCandidate(
                changed_source=seed,
                candidate=_entity("file", file.id, nodes),
                heuristic=(
                    f"Indexed test path {file.path!r} matches changed source "
                    f"basename {source_stem!r} after removing {marker}."
                ),
                freshness=dict(freshness),
                freshness_limitations=_freshness_limitations(freshness),
            )
            candidates[(seed.path, file.id)] = candidate
    return tuple(
        candidates[key]
        for key in sorted(candidates, key=lambda item: (item[0], item[1]))
    )


def _matching_test_marker(path: str, source_stem: str) -> str | None:
    if not _is_test_path(path):
        return None
    stem = PurePosixPath(path.replace("\\", "/")).stem.casefold()
    conventions = (
        ("test_", stem.removeprefix("test_")),
        ("_test", stem.removesuffix("_test")),
        (".test", stem.removesuffix(".test")),
        (".spec", stem.removesuffix(".spec")),
        ("Test", stem.removesuffix("test")),
        ("Tests", stem.removesuffix("tests")),
    )
    for marker, normalized in conventions:
        if normalized != stem and normalized == source_stem:
            return f"the {marker!r} filename marker"
    if stem == source_stem:
        return "a test directory convention"
    return None


def _category_coverage(
    status: WorkspaceStatusReport,
    graph: NormalizedGraphFacts,
    nodes: dict[tuple[str, str], object],
) -> dict[str, dict[str, Any]]:
    relationship_state = status.coverage.get("relationship_extraction", {}).get("state")
    if relationship_state != "observed":
        reason = (
            "Relationship extraction coverage is not observed; this category "
            "cannot be distinguished from an empty supported result, and safety "
            "cannot be inferred."
        )
        return _unavailable_category_coverage(reason)

    test_edges = [
        edge
        for edge in graph.edges
        if _is_dependency_relation(edge.relation_type)
        and edge.evidence
        and _test_file_for_entity(
            _entity(edge.source_type, edge.source_id, nodes), nodes
        )
    ]
    return {
        "contracts": _coverage_entry(
            any(
                (edge.source_type == "contract" or edge.target_type == "contract")
                and edge.evidence
                for edge in graph.edges
            ),
            "No stored contract-bearing relationships establish contract coverage; "
            "safety cannot be inferred.",
        ),
        "services": _coverage_entry(
            any(
                (edge.source_type == "service" or edge.target_type == "service")
                and edge.evidence
                for edge in graph.edges
            ),
            "No stored service-bearing relationships establish service coverage; "
            "safety cannot be inferred.",
        ),
        "tests": _coverage_entry(
            bool(test_edges),
            "No indexed test file or symbol has a stored dependency relationship; "
            "safety cannot be inferred.",
        ),
    }


def _coverage_entry(observed: bool, unavailable_reason: str) -> dict[str, Any]:
    return (
        {"state": "observed", "reason": "Supported relationships are stored."}
        if observed
        else {"state": "unavailable", "reason": unavailable_reason}
    )


def _unavailable_category_coverage(reason: str) -> dict[str, dict[str, Any]]:
    return {
        category: {"state": "unavailable", "reason": reason}
        for category in ("contracts", "services", "tests")
    }


def _freshness_limitations(freshness: dict[str, Any]) -> tuple[str, ...]:
    limitations = []
    for item in freshness.get("coverage_reasons", ()):
        message = item.get("message") if isinstance(item, dict) else item
        if message:
            limitations.append(str(message))
    state = str(freshness.get("state", "unknown"))
    coverage = str(freshness.get("coverage", "unknown"))
    if state != "fresh":
        limitations.append(f"Index freshness is {state}.")
    if coverage != "complete":
        limitations.append(f"Freshness coverage is {coverage}.")
    return tuple(dict.fromkeys(limitations))


def _citations(
    root: Path,
    repository_id: str,
    evidence: tuple[Evidence, ...],
) -> tuple[SourceCitation, ...]:
    citations = []
    for item in sorted(evidence, key=_evidence_sort_key):
        path = _repository_relative_path(root, item.file_path)
        if path is None:
            continue
        citations.append(
            SourceCitation(
                repository_id=repository_id,
                path=path,
                start_line=item.start_line,
                end_line=item.end_line,
            )
        )
    return tuple(dict.fromkeys(citations))


def _repository_relative_path(root: Path, raw: str) -> str | None:
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve(strict=False).relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return None
    parsed = PurePosixPath(raw.replace("\\", "/"))
    if parsed.is_absolute() or ".." in parsed.parts:
        return None
    return parsed.as_posix()


def _relationship_id(edge: Edge) -> str:
    payload = json.dumps(
        (
            edge.source_type,
            edge.source_id,
            edge.relation_type,
            edge.target_type,
            edge.target_id,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "relationship:" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def _load_relationship_ids_read_only(
    workspace: Path,
) -> dict[tuple[str, str, str, str, str], str]:
    path = database_path(workspace)
    if not path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT id, source_type, source_id, target_type, target_id,
                       relation_type
                FROM edges
                ORDER BY source_type, source_id, relation_type, target_type,
                         target_id
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return {}
    return {
        (
            str(row["source_type"]),
            str(row["source_id"]),
            str(row["target_type"]),
            str(row["target_id"]),
            str(row["relation_type"]),
        ): f"relationship:{row['id']}"
        for row in rows
    }


def _edge_key(edge: Edge) -> tuple[str, str, str, str, str]:
    return (
        edge.source_type,
        edge.source_id,
        edge.target_type,
        edge.target_id,
        edge.relation_type,
    )


def _edge_sort_key(edge: Edge) -> tuple[Any, ...]:
    return (
        edge.source_type,
        edge.source_id,
        edge.relation_type,
        edge.target_type,
        edge.target_id,
        _relationship_id(edge),
    )


def _evidence_sort_key(evidence: Evidence) -> tuple[Any, ...]:
    return (
        evidence.file_path,
        evidence.start_line or 0,
        evidence.end_line or 0,
        evidence.extractor_name,
        -(evidence.confidence or 0.0),
        json.dumps(evidence.metadata, sort_keys=True, default=str),
    )


def _result(
    paths: list[ImpactPath] | tuple[ImpactPath, ...],
    inferred_test_candidates: tuple[InferredTestCandidate, ...],
    gaps: list[CoverageGap],
    truncated: bool,
    max_hops: int,
    max_results: int,
    change_seeds: ChangeSeedResult,
    category_coverage: dict[str, dict[str, Any]],
) -> ImpactPathResult:
    gap_values = {
        (gap.code, gap.repository_id, gap.path, gap.message): gap for gap in gaps
    }
    path_values = tuple(paths)
    categorized = _categorized_paths(path_values, {})
    return ImpactPathResult(
        paths=path_values,
        affected_contracts=categorized["affected_contracts"],
        affected_services=categorized["affected_services"],
        test_candidates=categorized["test_candidates"],
        inferred_test_candidates=inferred_test_candidates,
        category_coverage=category_coverage,
        coverage_gaps=tuple(gap_values[key] for key in sorted(gap_values)),
        truncated=truncated,
        max_hops=max_hops,
        max_results=max_results,
        freshness=dict(change_seeds.freshness),
    )
