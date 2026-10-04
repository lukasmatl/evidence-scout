from __future__ import annotations

import json

import pytest

from code_scout.core.errors import StorageError
from code_scout.evidence.pack import EvidencePackLimits, compile_evidence_pack
from code_scout.models.graph import Edge, File, Repository, Snapshot, Symbol
from code_scout.storage import (
    RawEvidenceUnit,
    create_snapshot,
    evidence_text_hash,
    open_storage,
    upsert_edge,
    upsert_file,
    upsert_raw_evidence_units,
    upsert_repository,
    upsert_symbol,
    upsert_workspace,
)


class _Provider:
    model_id = "pack-test"
    dimensions = 2

    def embed_text(self, _text: str) -> tuple[float, ...]:
        return (1.0, 0.0)


def test_evidence_pack_ranks_provenance_and_builds_native_repository_map(
    tmp_path,
) -> None:
    first, second = _seed_scanned_workspace(tmp_path)

    pack = compile_evidence_pack(
        tmp_path,
        question="workflow",
        provider=_Provider(),
    )

    assert [item.id for item in pack.evidence] == [first.id, second.id]
    assert [item.rank for item in pack.evidence] == [1, 2]
    assert pack.evidence[0].provenance == "typescript"
    assert pack.evidence[0].confidence == 0.95
    assert pack.evidence[0].start_line == 1
    assert pack.evidence[0].related_symbols == ("symbol:checkout",)
    assert pack.evidence[0].related_files == ("file:payments",)
    assert pack.relationships[0].relation_type == "implemented_by"
    assert pack.relationships[0].confidence == 0.9
    assert pack.relationships[0].provenance == ("typescript",)
    symbol = next(item for item in pack.repository_map if item.kind == "symbol")
    assert symbol.name == "payments.checkout"
    assert symbol.signature == "def checkout(order_id: str) -> str:"
    assert symbol.source_provenance[0].extractor == "tree_sitter"
    assert (
        symbol.source_provenance[0].start_line,
        symbol.source_provenance[0].end_line,
    ) == (
        1,
        2,
    )
    assert "def checkout(order_id: str) -> str:" in pack.render()


def test_evidence_pack_limits_units_map_characters_and_per_file(tmp_path) -> None:
    _seed_scanned_workspace(tmp_path)
    limits = EvidencePackLimits(
        max_evidence_units=1,
        max_map_entries=1,
        max_characters=180,
        max_characters_per_file=12,
    )

    pack = compile_evidence_pack(
        tmp_path,
        question="workflow",
        provider=_Provider(),
        limits=limits,
    )

    assert len(pack.evidence) == 1
    assert pack.evidence[0].text == "Primary paym"
    assert len(pack.repository_map) == 1
    assert pack.repository_map[0].kind == "file"
    assert pack.character_count <= limits.max_characters
    assert json.loads(pack.context)["context_truncated"] is True
    assert pack.truncated is True


def test_evidence_pack_order_is_stable_across_insertion_order(tmp_path) -> None:
    _seed_scanned_workspace(tmp_path, reverse=True)

    first = compile_evidence_pack(
        tmp_path,
        question="workflow",
        provider=_Provider(),
    )
    second = compile_evidence_pack(
        tmp_path,
        question="workflow",
        provider=_Provider(),
    )

    assert first.to_dict() == second.to_dict()


def test_evidence_pack_missing_database_is_actionable_and_read_only(tmp_path) -> None:
    with pytest.raises(StorageError, match="has not been scanned") as error:
        compile_evidence_pack(
            tmp_path,
            question="workflow",
            provider=_Provider(),
        )

    assert "code-scout scan" in str(error.value.hint)
    assert not (tmp_path / ".code-scout").exists()


def test_evidence_pack_requires_a_completed_scan_snapshot(tmp_path) -> None:
    with open_storage(tmp_path):
        pass

    with pytest.raises(StorageError, match="no scan snapshot"):
        compile_evidence_pack(
            tmp_path,
            question="workflow",
            provider=_Provider(),
        )


def test_evidence_pack_excludes_outside_and_symlink_evidence(tmp_path) -> None:
    first, _second = _seed_scanned_workspace(tmp_path)
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    escape = tmp_path / "escape.py"
    try:
        escape.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    outside_units = (
        _unit("evidence:a-outside", "../outside.py", "outside traversal"),
        _unit("evidence:b-symlink", "escape.py", "symlink escape"),
    )
    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, outside_units)
        for unit in outside_units:
            _insert_embedding(connection, unit, (1.0, 0.0))
        connection.commit()

    pack = compile_evidence_pack(
        tmp_path,
        question="workflow",
        provider=_Provider(),
        limits=EvidencePackLimits(max_evidence_units=10),
    )

    assert [item.id for item in pack.evidence] == [first.id, "evidence:z-secondary"]
    assert {item["evidence_unit_id"] for item in pack.needs_review} == {
        "evidence:a-outside",
        "evidence:b-symlink",
    }
    assert "outside traversal" not in pack.render()
    assert "symlink escape" not in pack.render()


def _seed_scanned_workspace(tmp_path, *, reverse: bool = False):
    source = tmp_path / "src" / "payments.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def checkout(order_id: str) -> str:\n    return order_id\n",
        encoding="utf-8",
    )
    other = tmp_path / "src" / "secondary.py"
    other.write_text("def secondary():\n    pass\n", encoding="utf-8")
    primary = _unit(
        "evidence:a-primary",
        "src/payments.py",
        "Primary payment implementation " * 4,
        confidence=0.95,
    )
    secondary = _unit(
        "evidence:z-secondary",
        "src/secondary.py",
        "Secondary payment helper",
        confidence=0.6,
    )
    units = (secondary, primary) if reverse else (primary, secondary)

    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection,
            id="workspace:test",
            name="test",
            root_path=str(tmp_path),
        )
        upsert_repository(
            connection,
            "workspace:test",
            Repository("repository:test", "test", "."),
        )
        upsert_file(
            connection,
            File("file:payments", "repository:test", "src/payments.py"),
        )
        upsert_symbol(
            connection,
            Symbol(
                "symbol:checkout",
                "file:payments",
                "checkout",
                "function",
                "payments.checkout",
                1,
                2,
            ),
        )
        upsert_edge(
            connection,
            Edge(
                "service",
                "service:payments",
                "symbol",
                "symbol:checkout",
                "implemented_by",
                0.9,
                evidence=(_graph_evidence(),),
            ),
        )
        upsert_edge(
            connection,
            Edge(
                "service",
                "service:payments",
                "file",
                "file:payments",
                "owns",
                1.0,
                evidence=(_graph_evidence(),),
            ),
        )
        symbol_unit = RawEvidenceUnit(
            id="evidence:symbol-checkout",
            owner_entity_type="symbol",
            owner_entity_id="symbol:checkout",
            source_file="src/payments.py",
            start_line=1,
            end_line=2,
            text="checkout declaration",
            provenance="tree_sitter",
            confidence=0.85,
        )
        upsert_raw_evidence_units(connection, (*units, symbol_unit))
        _insert_embedding(connection, primary, (1.0, 0.0))
        _insert_embedding(connection, secondary, (0.8, 0.6))
        create_snapshot(
            connection,
            Snapshot("snapshot:test", "workspace:test", "2026-07-16T00:00:00Z"),
        )
        connection.commit()
    return primary, secondary


def _unit(
    id: str,
    path: str,
    text: str,
    *,
    confidence: float = 0.8,
) -> RawEvidenceUnit:
    return RawEvidenceUnit(
        id=id,
        owner_entity_type="service",
        owner_entity_id="service:payments",
        source_file=path,
        start_line=1,
        end_line=2,
        text=text,
        provenance="typescript",
        confidence=confidence,
    )


def _insert_embedding(connection, unit: RawEvidenceUnit, vector) -> None:
    connection.execute(
        """
        INSERT INTO evidence_embeddings (
            evidence_unit_id, model_id, vector_json, dimensions, text_hash
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            unit.id,
            _Provider.model_id,
            json.dumps(vector),
            _Provider.dimensions,
            evidence_text_hash(unit.text),
        ),
    )


def _graph_evidence():
    from code_scout.models.graph import Evidence

    return Evidence(
        "src/payments.py",
        "typescript",
        confidence=0.9,
        start_line=1,
        end_line=2,
    )


@pytest.mark.parametrize("unresolved", [False, True])
def test_pack_preserves_provider_assessments_and_serialization(
    tmp_path, unresolved
) -> None:
    from dataclasses import asdict, replace

    from code_scout.domain.resolver import resolve_active_facts
    from code_scout.harvest.raw_evidence import raw_evidence_units_from_active_fact
    from code_scout.models.facts import ActiveFact, FactEvidence

    primary, _ = _seed_scanned_workspace(tmp_path)
    fact = ActiveFact(
        id="fact:derived",
        extractor="graphify",
        kind="implemented_by",
        source_file="src/payments.py",
        identity={
            "subject": {"type": "service", "id": "service:payments"},
            "relation": "implemented_by",
            "object": {"type": "symbol", "id": "symbol:checkout"},
        },
        properties={"validation": "corroborated", "unresolved": unresolved},
        evidence=(FactEvidence("src/payments.py", "graphify", 0.2, 1, 2),),
        confidence=0.2,
    )
    extracted = replace(
        fact,
        id="fact:extracted",
        extractor="typescript",
        properties={},
        confidence=1.0,
        evidence=(FactEvidence("missing.py", "typescript", 1.0),),
    )
    edge = resolve_active_facts((fact, extracted)).edges[0]
    raw = raw_evidence_units_from_active_fact(fact)[0]
    with open_storage(tmp_path) as connection:
        upsert_edge(connection, edge)
        upsert_raw_evidence_units(
            connection, (replace(primary, metadata=raw.metadata),)
        )
        connection.commit()

    pack = compile_evidence_pack(tmp_path, question="workflow", provider=_Provider())
    relation = next(
        r for r in pack.relationships if r.relation_type == "implemented_by"
    )
    assert relation.assessment["origin"] == "inferred"
    assert relation.assessment["validation"] == (
        "unresolved" if unresolved else "corroborated"
    )
    assert relation.confidence == edge.confidence
    assert relation.provenance == ("graphify", "typescript")
    assert len(relation.supporting_evidence) == 2  # Includes the unavailable source.
    assert len(relation.source_provenance) == 1
    assert {r["extractor_name"] for r in relation.supporting_evidence} == {
        "graphify",
        "typescript",
    }
    entry = next(e for e in pack.repository_map if e.id == relation.id)
    assert entry.assessment == relation.assessment
    assert pack.evidence[0].id == primary.id
    assert (
        pack.evidence[0].assessment["validation"] == relation.assessment["validation"]
    )
    symbol = next(e for e in pack.repository_map if e.kind == "symbol")
    assert symbol.assessment["origin"] == "extracted"
    assert all(
        "assessment" in item
        for key in ("evidence", "relationships", "repository_map")
        for item in pack.to_dict()[key]
    )
    before = json.dumps(pack.to_dict(), sort_keys=True)
    with open_storage(tmp_path) as connection:
        upsert_edge(connection, replace(edge, evidence=tuple(reversed(edge.evidence))))
        connection.commit()
    assert before == json.dumps(
        compile_evidence_pack(
            tmp_path, question="workflow", provider=_Provider()
        ).to_dict(),
        sort_keys=True,
    )
    assert asdict(edge)["evidence"][0]["assessment"]


def test_legacy_pack_without_provenance_stays_unknown() -> None:
    from dataclasses import asdict

    from code_scout.evidence.pack import PackedRelationship, RepositoryMapEntry

    relation = PackedRelationship("r", "symbol", "a", "calls", "symbol", "b", 1.0, ())
    entry = RepositoryMapEntry("file", "f", "app.py", "app.py")
    for item in (relation, entry):
        assessment = asdict(item)["assessment"]
        assert assessment["origin"] == "unknown"
        assert assessment["validation"] == "unknown"
        assert assessment["labels"] == ["unknown"]


def test_packed_raw_evidence_includes_resolved_owner_conflicts(tmp_path) -> None:
    from code_scout.models.graph import Evidence, Service
    from code_scout.storage import upsert_service

    primary, _ = _seed_scanned_workspace(tmp_path)
    with open_storage(tmp_path) as connection:
        upsert_service(
            connection,
            Service(
                "service:payments",
                "Payments",
                evidence=(
                    Evidence(
                        primary.source_file,
                        primary.provenance,
                        confidence=primary.confidence,
                        metadata={"resolver_conflicts": [{"field": "name"}]},
                    ),
                ),
            ),
        )
        connection.commit()
    pack = compile_evidence_pack(tmp_path, question="workflow", provider=_Provider())
    assert pack.evidence[0].id == primary.id
    assert pack.evidence[0].assessment["conflicted"] is True
    assert pack.evidence[0].confidence == primary.confidence
