from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_scout.evidence.pack import compile_evidence_pack
from code_scout.evidence.tools import EvidenceTools
from code_scout.models.graph import File, Repository, Snapshot
from code_scout.storage import (
    RawEvidenceUnit,
    create_snapshot,
    evidence_text_hash,
    open_storage,
    upsert_file,
    upsert_raw_evidence_units,
    upsert_repository,
    upsert_workspace,
)
from code_scout.workspace.config import DEFAULT_CONFIG_PATH, load_workspace_config

_FIXTURE = Path(__file__).parent / "fixtures" / "dede" / "minimal-graph.json"


class _Provider:
    model_id = "dede-test"
    dimensions = 2

    def embed_text(self, _text: str) -> tuple[float, ...]:
        return (1.0, 0.0)


def test_configured_dede_graph_enriches_dotnet_verification_evidence(
    tmp_path: Path,
) -> None:
    _seed_scanned_workspace(tmp_path, source_suffix=".cs")
    _configure_dede(tmp_path, _FIXTURE.read_text(encoding="utf-8"))

    pack = compile_evidence_pack(
        tmp_path,
        question="orders blast radius",
        provider=_Provider(),
    )

    assert pack.evidence
    assert {edge.relation_type for edge in pack.relationships} >= {
        "IMPLEMENTS",
        "DI_RESOLVES_TO",
        "HTTP_CALLS",
        "READS_TABLE",
        "RESOLVES_TO_SERVICE",
    }
    di = next(edge for edge in pack.relationships if edge.id == "dede:di-1")
    assert di.certainty == "Certain"
    assert di.confidence == 1.0
    assert di.source_provenance[0].extractor == "dede"
    ambiguous = next(
        edge for edge in pack.relationships if edge.id == "dede:ambiguous-http"
    )
    assert ambiguous.certainty == "Ambiguous"
    assert ambiguous.unresolved is True
    assert any(item["id"] == "ambiguous-http" for item in pack.needs_review)
    assert pack.impact_paths[0].node_ids == (
        "endpoint:GET-orders",
        "method:OrdersService.Get",
        "table:orders",
    )
    assert pack.impact_paths[0].edge_ids == (
        "dede:http-1",
        "dede:lineage-1",
    )
    assert {warning["code"] for warning in pack.warnings} >= {
        "dede-missing-executable",
        "dede-invalid-source-location",
    }

    tools = EvidenceTools(tmp_path, pack)
    implementation = tools.find_implementations("IOrders")
    impact = tools.inspect_impact_paths(node_id="table:orders")
    traversal = tools.traverse_neighbors("endpoint:GET-orders")

    assert implementation["items"][0]["neighbor_id"] == ("method:OrdersService.Get")
    assert implementation["items"][0]["provenance"]["extractors"] == ["dede"]
    assert impact["items"][0]["certainty"] == "Inferred"
    classes = {item["id"]: item["confidence_class"] for item in traversal["items"]}
    assert classes["dede:ambiguous-http"] == "ambiguous"
    assert classes["dede:http-1"] == "inferred"
    assert any(item["relation"] == "READS_TABLE" for item in traversal["items"])


def test_dede_stale_graph_warns_but_keeps_base_and_enriched_evidence(
    tmp_path: Path,
) -> None:
    _seed_scanned_workspace(tmp_path, source_suffix=".cs")
    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    document["scanMetadata"]["completedAtUtc"] = "2000-01-01T00:00:00Z"
    _configure_dede(tmp_path, json.dumps(document), max_age_seconds=60)

    pack = compile_evidence_pack(
        tmp_path,
        question="orders",
        provider=_Provider(),
    )

    assert pack.evidence
    assert any(edge.id == "dede:http-1" for edge in pack.relationships)
    assert "dede-stale-graph" in {warning["code"] for warning in pack.warnings}


@pytest.mark.parametrize(
    ("graph", "warning"),
    [
        (None, "dede-missing-graph"),
        (
            '{"schemaVersion":"2","nodes":[],"edges":[],"scanMetadata":{}}',
            "dede-incompatible-schema",
        ),
    ],
)
def test_missing_or_incompatible_dede_graph_keeps_base_evidence(
    tmp_path: Path,
    graph: str | None,
    warning: str,
) -> None:
    _seed_scanned_workspace(tmp_path, source_suffix=".cs")
    _configure_dede(tmp_path, graph)

    pack = compile_evidence_pack(
        tmp_path,
        question="orders",
        provider=_Provider(),
    )

    assert pack.evidence
    assert not any(edge.id.startswith("dede:") for edge in pack.relationships)
    assert warning in {item["code"] for item in pack.warnings}


def test_dede_is_not_loaded_for_non_dotnet_scanned_workspace(tmp_path: Path) -> None:
    _seed_scanned_workspace(tmp_path, source_suffix=".py")
    _configure_dede(tmp_path, _FIXTURE.read_text(encoding="utf-8"))

    pack = compile_evidence_pack(
        tmp_path,
        question="orders",
        provider=_Provider(),
    )

    assert pack.evidence
    assert pack.warnings == ()
    assert not any(edge.id.startswith("dede:") for edge in pack.relationships)
    assert pack.impact_paths == ()


def test_workspace_config_parses_dede_without_making_it_an_extractor(
    tmp_path: Path,
) -> None:
    _configure_dede(tmp_path, None, max_age_seconds=120)

    config = load_workspace_config(tmp_path)

    assert config.enrichment.dede.enabled is True
    assert config.enrichment.dede.graph_path == ".code-scout/dede.json"
    assert config.enrichment.dede.executable == "definitely-missing-dede"
    assert config.enrichment.dede.max_age_seconds == 120


def _configure_dede(
    workspace: Path,
    graph: str | None,
    *,
    max_age_seconds: int = 86_400,
) -> None:
    state = workspace / ".code-scout"
    state.mkdir(exist_ok=True)
    (workspace / DEFAULT_CONFIG_PATH).write_text(
        f"""workspace:
  id: test
  name: Test
repositories:
  - id: test
    path: .
enrichment:
  dede:
    enabled: true
    graph_path: .code-scout/dede.json
    executable: definitely-missing-dede
    max_age_seconds: {max_age_seconds}
""",
        encoding="utf-8",
    )
    if graph is not None:
        (state / "dede.json").write_text(graph, encoding="utf-8")


def _seed_scanned_workspace(workspace: Path, *, source_suffix: str) -> None:
    relative = f"src/Orders{source_suffix}"
    source = workspace / relative
    source.parent.mkdir()
    source.write_text(
        "interface IOrders {}\n"
        "\n"
        "class OrdersService {\n"
        "  object Get() => db.Orders;\n"
        "}\n"
        "\n"
        'app.MapGet("/api/orders/{id}", () => service.Get());\n'
        'client.GetAsync("/api/orders/1");\n'
        "\n"
        "services.AddScoped<IOrders, OrdersService>();\n",
        encoding="utf-8",
    )
    unit = RawEvidenceUnit(
        id="evidence:orders",
        owner_entity_type="service",
        owner_entity_id="service:orders",
        source_file=relative,
        start_line=1,
        end_line=10,
        text="orders service blast radius",
        provenance="dotnet" if source_suffix == ".cs" else "tree_sitter",
        confidence=0.9,
    )
    with open_storage(workspace) as connection:
        upsert_workspace(
            connection,
            id="workspace:test",
            name="test",
            root_path=str(workspace),
        )
        upsert_repository(
            connection,
            "workspace:test",
            Repository("repository:test", "test", "."),
        )
        upsert_file(
            connection,
            File("file:orders", "repository:test", relative),
        )
        upsert_raw_evidence_units(connection, (unit,))
        connection.execute(
            """
            INSERT INTO evidence_embeddings (
                evidence_unit_id, model_id, vector_json, dimensions, text_hash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                unit.id,
                _Provider.model_id,
                json.dumps([1.0, 0.0]),
                _Provider.dimensions,
                evidence_text_hash(unit.text),
            ),
        )
        create_snapshot(
            connection,
            Snapshot("snapshot:test", "workspace:test", "2026-07-16T00:00:00Z"),
        )
        connection.commit()
