from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

import code_scout.pipeline.scan as scan_module
from code_scout.core.errors import ExtractionError
from code_scout.discovery import DiscoveredFile
from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.extractors import SymbolExtraction
from code_scout.extractors.graphify import (
    GraphifyExtraction,
    GraphifyWatchEvent,
)
from code_scout.extractors.lsp import (
    LSPDocumentSymbol,
    LSPRelationship,
    TypeScriptLSPExtraction,
    TypeScriptLSPRun,
)
from code_scout.harvest.core import _infer_language_hints
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.models.graph import Contract, Edge, Evidence, Service, Symbol
from code_scout.models.workspace import default_single_repository_workspace
from code_scout.pipeline.scan import (
    EXTRACTOR_PROFILES,
    ChangedFileReplacementResult,
    ScanCounts,
    ScanResult,
    replace_changed_file_active_facts,
    scan_workspace,
)
from code_scout.storage import (
    database_path,
    delete_active_facts_by_extractor_and_source_file,
    evidence_text_hash,
    get_snapshot_payload,
    list_active_facts,
    list_change_events,
    list_evidence_embeddings,
    list_raw_evidence_units,
    open_storage,
    upsert_active_facts,
)
from code_scout.workspace.config import WorkspaceConfig, create_workspace_config

FIXTURES = Path(__file__).parent / "fixtures"


def _without_revision_metadata(
    evidence: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Keep legacy metadata assertions focused on their pre-US-005 contract."""
    for item in evidence:
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            for key in ("source_revision_id", "content_sha256"):
                metadata.pop(key, None)
    return evidence


class _CountingEmbeddingProvider:
    model_id = "test-scan-provider"
    dimensions = 4

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_text(self, text: str) -> tuple[float, ...]:
        self.texts.append(text)
        return (float(len(text)), 1.0, 0.0, 0.0)


def test_scan_exports_are_documented_and_resolve_to_owner_modules() -> None:
    from code_scout import extractors, harvest
    from code_scout.pipeline import incremental

    assert scan_module.__all__ == (
        "ChangedFileReplacementResult",
        "EXTRACTOR_PROFILES",
        "ScanCounts",
        "ScanResult",
        "replace_changed_file_active_facts",
        "scan_workspace",
    )
    assert ChangedFileReplacementResult is harvest.ChangedFileReplacementResult
    assert EXTRACTOR_PROFILES is extractors.EXTRACTOR_PROFILES
    assert ScanCounts is harvest.ScanCounts
    assert ScanResult is harvest.ScanResult
    assert (
        replace_changed_file_active_facts
        is incremental.replace_changed_file_active_facts
    )
    assert scan_workspace is scan_module.scan_workspace


def test_extractor_profiles_define_expected_evidence_modes() -> None:
    assert EXTRACTOR_PROFILES["code-only"].extractors == (
        "graphify",
        "tree_sitter",
        "lsp",
    )
    assert EXTRACTOR_PROFILES["docs"].extractors == ("adr", "docs", "openapi")
    assert EXTRACTOR_PROFILES["agent"].extractors == (
        "graphify",
        "openapi",
        "adr",
    )
    assert EXTRACTOR_PROFILES["full"].extractors == (
        "graphify",
        "tree_sitter",
        "lsp",
        "adr",
        "openapi",
        "asyncapi",
        "infra",
        "docs",
    )


def test_tree_sitter_profiles_include_symbol_extraction_scan_selector() -> None:
    assert EXTRACTOR_PROFILES["code-only"].scan_extractors == (
        "graphify",
        "tree_sitter",
        "lsp",
        "symbol_extraction",
    )
    assert EXTRACTOR_PROFILES["full"].scan_extractors == (
        "graphify",
        "tree_sitter",
        "lsp",
        "adr",
        "openapi",
        "asyncapi",
        "infra",
        "docs",
        "symbol_extraction",
    )


def test_scan_module_does_not_own_extraction_assembly_helpers() -> None:
    moved_helper_names = {
        "_active_facts_from_extracted_graph",
        "_adr_decision_edges",
        "_concepts_with_inferred_evidence",
        "_edge_with_normalized_contract_ids",
        "_edges_with_normalized_contract_ids",
        "_filter_graphify_extraction_to_discovered_files",
        "_framework_hints_by_repository_id",
        "_graph_file",
        "_graph_repositories",
        "_graphify_config_for_scan",
        "_graphify_extraction_for_scan",
        "_infer_language_hints",
        "_infer_module_hints",
        "_merge_active_facts_by_id",
        "_symbol_extraction",
        "_symbols_with_inferred_evidence",
        "_suppress_tree_sitter_facts_covered_by_graphify",
    }

    assert moved_helper_names.isdisjoint(vars(scan_module))


@pytest.mark.parametrize(
    ("profile", "expected_paths"),
    [
        ("code-only", {"graphify", "tree_sitter", "lsp", "symbol_extraction"}),
        ("docs", {"adr", "docs", "openapi"}),
        ("agent", {"graphify", "openapi", "adr"}),
        (
            "full",
            {
                "graphify",
                "tree_sitter",
                "lsp",
                "adr",
                "openapi",
                "asyncapi",
                "infra",
                "docs",
                "symbol_extraction",
            },
        ),
    ],
)
def test_scan_profiles_apply_named_harvest_sets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    profile: str,
    expected_paths: set[str],
) -> None:
    create_workspace_config(tmp_path)
    harvested_paths: list[str] = []

    def fake_graphify(*_args: object, **_kwargs: object) -> GraphifyExtraction:
        harvested_paths.append("graphify")
        return GraphifyExtraction(services=(_profile_service("graphify"),))

    def fake_lsp(*_args: object, **_kwargs: object) -> TypeScriptLSPExtraction:
        harvested_paths.append("lsp")
        return TypeScriptLSPExtraction()

    def fake_adr(
        _discovered_files: tuple[DiscoveredFile, ...],
    ) -> tuple[ActiveFact, ...]:
        harvested_paths.append("adr")
        return (_profile_active_fact("adr_markdown"),)

    def fake_docs(
        _discovered_files: tuple[DiscoveredFile, ...],
    ) -> tuple[ActiveFact, ...]:
        harvested_paths.append("docs")
        return (_profile_active_fact("docs"),)

    def fake_openapi(
        *_args: object, **_kwargs: object
    ) -> tuple[tuple[ActiveFact, ...], tuple[object, ...]]:
        harvested_paths.append("openapi")
        return ((_profile_active_fact("openapi"),), ())

    def fake_asyncapi(
        *_args: object, **_kwargs: object
    ) -> tuple[tuple[ActiveFact, ...], tuple[object, ...]]:
        harvested_paths.append("asyncapi")
        return ((_profile_active_fact("asyncapi"),), ())

    def fake_infra(*_args: object, **_kwargs: object) -> tuple[ActiveFact, ...]:
        harvested_paths.append("infra")
        return (_profile_active_fact("infra"),)

    class FakeRegistry:
        def __init__(self, *, tree_sitter_enabled: bool, **_kwargs: object) -> None:
            if tree_sitter_enabled:
                harvested_paths.append("tree_sitter")

        def extract(
            self, _discovered_files: tuple[DiscoveredFile, ...]
        ) -> SymbolExtraction:
            harvested_paths.append("symbol_extraction")
            return SymbolExtraction(services=(_profile_service("tree_sitter"),))

    monkeypatch.setattr("code_scout.harvest.adapters.extract_graphify", fake_graphify)
    monkeypatch.setattr("code_scout.harvest.adapters.extract_typescript_lsp", fake_lsp)
    monkeypatch.setattr("code_scout.harvest.adapters.adr_active_facts", fake_adr)
    monkeypatch.setattr("code_scout.harvest.adapters.docs_active_facts", fake_docs)
    monkeypatch.setattr(
        "code_scout.harvest.adapters.openapi_active_facts", fake_openapi
    )
    monkeypatch.setattr(
        "code_scout.harvest.adapters.asyncapi_active_facts", fake_asyncapi
    )
    monkeypatch.setattr("code_scout.harvest.adapters.infra_active_facts", fake_infra)
    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)

    result = scan_workspace(
        str(tmp_path),
        profile=profile,
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    metadata_json = connection.execute(
        "SELECT metadata_json FROM snapshots WHERE id = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    connection.close()

    assert set(harvested_paths) == expected_paths
    assert result.extractor == profile
    assert json.loads(metadata_json) == {
        "extractor": profile,
        "extractor_count": len(expected_paths),
        "selection_type": "profile",
        "selection": profile,
        "selected_extractors": [
            name
            for name in EXTRACTOR_PROFILES[profile].scan_extractors
            if name in result.workspace.extractors.enabled
        ],
    }


def test_scan_loads_config_and_workspace_model(tmp_path) -> None:
    create_workspace_config(tmp_path)

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    assert result.workspace.id == "workspace"
    assert result.workspace.root_path == str(tmp_path)
    assert result.workspace.repositories[0].path == "."


def test_scan_initializes_storage_and_creates_snapshot_record(tmp_path) -> None:
    create_workspace_config(tmp_path)

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    workspace_rows = connection.execute(
        "SELECT id, name, root_path FROM workspaces"
    ).fetchall()
    repository_rows = connection.execute(
        "SELECT id, workspace_id, name, path FROM repositories"
    ).fetchall()
    snapshot_rows = connection.execute(
        "SELECT id, workspace_id, metadata_json, payload_sha256 FROM snapshots"
    ).fetchall()
    payload = get_snapshot_payload(connection, snapshot_rows[0]["payload_sha256"])
    connection.close()

    assert [dict(row) for row in workspace_rows] == [
        {"id": "workspace", "name": "Workspace", "root_path": str(tmp_path)}
    ]
    assert [dict(row) for row in repository_rows] == [
        {
            "id": "workspace",
            "workspace_id": "workspace",
            "name": "workspace",
            "path": ".",
        }
    ]
    assert len(snapshot_rows) == 1
    assert snapshot_rows[0]["id"] == result.snapshot_id
    assert snapshot_rows[0]["workspace_id"] == "workspace"
    assert snapshot_rows[0]["payload_sha256"] is not None
    assert payload is not None
    assert set(payload.to_dict()) == {
        "version",
        "active_facts",
        "resolved_entities",
        "resolved_edges",
        "raw_evidence",
        "revision_references",
        "repository_identities",
        "extractor_versions",
        "scan_scope",
        "coverage",
    }
    snapshot_metadata = json.loads(snapshot_rows[0]["metadata_json"])
    assert snapshot_metadata["extractor"] == ""
    assert snapshot_metadata["extractor_count"] == 8
    assert snapshot_metadata["selection_type"] == "all_enabled"
    assert snapshot_metadata["selection"] is None
    assert len(snapshot_metadata["selected_extractors"]) == 8


def test_full_rescan_removes_facts_and_evidence_for_deleted_files(tmp_path) -> None:
    create_workspace_config(tmp_path)
    stale_file = tmp_path / "stale.py"
    stale_file.write_text("def retired_symbol():\n    return True\n", encoding="utf-8")

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )
    with open_storage(tmp_path) as connection:
        assert any(
            fact.source_file == "stale.py" for fact in list_active_facts(connection)
        )
        assert any(
            unit.source_file == "stale.py"
            for unit in list_raw_evidence_units(connection)
        )

    stale_file.unlink()
    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 1, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        assert all(
            fact.source_file != "stale.py" for fact in list_active_facts(connection)
        )
        assert all(
            unit.source_file != "stale.py"
            for unit in list_raw_evidence_units(connection)
        )


def test_scan_records_single_extractor_path(tmp_path) -> None:
    create_workspace_config(tmp_path)

    result = scan_workspace(
        str(tmp_path),
        extractor="tree_sitter",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    metadata_json = connection.execute(
        "SELECT metadata_json FROM snapshots WHERE id = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    connection.close()

    assert result.extractor == "tree_sitter"
    assert json.loads(metadata_json)["extractor"] == "tree_sitter"


def test_scan_workspace_characterizes_docs_profile_persistence(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "README.md").write_text(
        "# Workspace\n\nDocuments the order workflow.\n",
        encoding="utf-8",
    )
    adr_file = tmp_path / "docs" / "adr" / "0001-use-orders-api.md"
    adr_file.parent.mkdir(parents=True)
    adr_file.write_text(
        """
# Use Orders API

Status: Accepted
Date: 2026-06-20

## Context
Orders need an HTTP boundary.

## Decision
Use the Orders API contract.

## Consequences
Clients call a stable endpoint.
""",
        encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    (tmp_path / "swagger.yaml").write_text("info: {}\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        profile="docs",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    snapshot_rows = connection.execute(
        "SELECT id, workspace_id, metadata_json FROM snapshots",
    ).fetchall()
    fact_rows = connection.execute(
        """
        SELECT extractor, kind, source_file
        FROM facts
        ORDER BY extractor, kind, source_file
        """,
    ).fetchall()
    graph_counts = {
        "files": connection.execute("SELECT count(*) FROM files").fetchone()[0],
        "contracts": connection.execute("SELECT count(*) FROM contracts").fetchone()[0],
        "concepts": connection.execute("SELECT count(*) FROM concepts").fetchone()[0],
        "decisions": connection.execute("SELECT count(*) FROM decisions").fetchone()[0],
        "edges": connection.execute("SELECT count(*) FROM edges").fetchone()[0],
    }
    contract_row = connection.execute(
        "SELECT method, path, operation_id FROM contracts",
    ).fetchone()
    connection.close()

    assert result.extractor == "docs"
    assert len(snapshot_rows) == 1
    assert snapshot_rows[0]["id"] == result.snapshot_id
    assert snapshot_rows[0]["workspace_id"] == "workspace"
    assert json.loads(snapshot_rows[0]["metadata_json"]) == {
        "extractor": "docs",
        "extractor_count": 3,
        "selection": "docs",
        "selection_type": "profile",
        "selected_extractors": ["adr", "docs", "openapi"],
    }
    assert {
        (row["extractor"], row["kind"], row["source_file"]) for row in fact_rows
    } >= {
        ("file_discovery", "file", "openapi.yaml"),
        ("openapi", "contract", "openapi.yaml"),
        ("adr_markdown", "decision", "docs/adr/0001-use-orders-api.md"),
        ("docs", "concept", "README.md"),
    }
    assert result.counts.files == graph_counts["files"] == 4
    assert result.counts.contracts == graph_counts["contracts"] == 1
    assert result.counts.decisions == graph_counts["decisions"] == 1
    assert result.counts.concepts == graph_counts["concepts"]
    assert result.counts.edges == graph_counts["edges"]
    assert dict(contract_row) == {
        "method": "GET",
        "path": "/orders",
        "operation_id": "listOrders",
    }
    assert [warning.path for warning in result.warnings] == ["swagger.yaml"]
    assert all(
        warning.message == "OpenAPI file is missing an `openapi` or `swagger` version."
        for warning in result.warnings
    )


def test_scan_lsp_extractor_runs_discovery_and_lsp_only(tmp_path) -> None:
    class RecordingLSPAdapter:
        def __init__(self) -> None:
            self.starts: list[tuple[str, tuple[str, ...]]] = []

        def start(
            self,
            *,
            repository_id: str,
            repository_root: str,
            command: tuple[str, ...],
        ) -> TypeScriptLSPRun:
            self.starts.append((repository_id, command))
            return TypeScriptLSPRun(
                repository_id=repository_id,
                repository_root=repository_root,
                mode="stdio",
                command=command,
            )

        def connect(
            self,
            *,
            repository_id: str,
            repository_root: str,
            address: str,
        ) -> TypeScriptLSPRun:
            raise AssertionError("connect should not be used")

        def collect(
            self,
            *,
            run: TypeScriptLSPRun,
            discovered_files: tuple[object, ...],
        ) -> TypeScriptLSPRun:
            return run

    create_workspace_config(tmp_path)
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"@nestjs/core":"^11.0.0"}}\n',
        encoding="utf-8",
    )
    controller_file = tmp_path / "src" / "users.controller.ts"
    controller_file.parent.mkdir()
    controller_file.write_text(
        """
import { Controller, Get } from '@nestjs/common';

@Controller('users')
export class UsersController {
  @Get()
  listUsers() {}
}
""",
        encoding="utf-8",
    )
    adapter = RecordingLSPAdapter()

    result = scan_workspace(
        str(tmp_path),
        extractor="lsp",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        lsp_adapter=adapter,
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    rows = {
        "files": connection.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "symbols": connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "services": connection.execute("SELECT COUNT(*) FROM services").fetchone()[0],
        "contracts": connection.execute(
            "SELECT COUNT(*) FROM contracts",
        ).fetchone()[0],
        "edges": connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
    }
    snapshot_metadata = connection.execute(
        "SELECT metadata_json FROM snapshots WHERE id = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    connection.close()

    assert adapter.starts == [("workspace", ("typescript-language-server", "--stdio"))]
    assert result.extractor == "lsp"
    assert result.counts.files == 2
    assert result.counts.package_files == 0
    assert result.counts.repositories_detected == 0
    assert result.counts.symbols == 0
    assert result.counts.contracts == 0
    assert result.counts.edges == 0
    assert rows == {
        "files": 2,
        "symbols": 0,
        "services": 0,
        "contracts": 0,
        "edges": 0,
    }
    assert json.loads(snapshot_metadata) == {
        "extractor": "lsp",
        "extractor_count": 1,
        "selection": "lsp",
        "selection_type": "extractor",
        "selected_extractors": ["lsp"],
    }


def test_scan_suppresses_lsp_broad_symbols_and_edges_without_existing_facts(
    tmp_path,
) -> None:
    class MockLSPAdapter:
        def start(
            self,
            *,
            repository_id: str,
            repository_root: str,
            command: tuple[str, ...],
        ) -> TypeScriptLSPRun:
            return TypeScriptLSPRun(
                repository_id=repository_id,
                repository_root=repository_root,
                mode="stdio",
                command=command,
            )

        def connect(
            self,
            *,
            repository_id: str,
            repository_root: str,
            address: str,
        ) -> TypeScriptLSPRun:
            raise AssertionError("connect should not be used")

        def collect(
            self,
            *,
            run: TypeScriptLSPRun,
            discovered_files: tuple[object, ...],
        ) -> TypeScriptLSPRun:
            assert {file.path for file in discovered_files} == {
                "src/app/api/users/route.ts",
                "src/app/api/users/users.service.ts",
            }
            return TypeScriptLSPRun(
                repository_id=run.repository_id,
                repository_root=run.repository_root,
                mode=run.mode,
                command=run.command,
                document_symbols=(
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/app/api/users/route.ts",
                        name="GET",
                        kind="function",
                        qualified_name="GET /api/users",
                        start_line=3,
                        end_line=5,
                        metadata={"framework": "nextjs"},
                    ),
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/app/api/users/users.service.ts",
                        name="listUsers",
                        kind="function",
                        qualified_name="listUsers",
                        start_line=1,
                        end_line=3,
                        metadata={"framework": "nextjs"},
                    ),
                ),
                relationships=(
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/app/api/users/route.ts",
                        source_line=4,
                        target_path="src/app/api/users/users.service.ts",
                        target_line=1,
                        kind="definition",
                        metadata={
                            "operation": "textDocument/definition",
                            "relationship": "route_handler_to_local_import",
                        },
                    ),
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/app/api/users/users.service.ts",
                        source_line=1,
                        target_path="src/app/api/users/route.ts",
                        target_line=4,
                        kind="reference",
                        metadata={"operation": "textDocument/references"},
                    ),
                ),
            )

    create_workspace_config(tmp_path)
    route_file = tmp_path / "src" / "app" / "api" / "users" / "route.ts"
    service_file = route_file.parent / "users.service.ts"
    route_file.parent.mkdir(parents=True)
    route_file.write_text(
        """
import { listUsers } from './users.service';
export async function GET() {
  return Response.json(await listUsers());
}
""",
        encoding="utf-8",
    )
    service_file.write_text(
        """
export async function listUsers() {
  return [];
}
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        extractor="lsp",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        lsp_adapter=MockLSPAdapter(),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    symbol_count = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    connection.close()

    assert result.counts.symbols == 0
    assert result.counts.edges == 0
    assert symbol_count == 0
    assert edge_count == 0


def test_scan_lsp_validation_boosts_confirmed_existing_code_edge(tmp_path) -> None:
    class MockLSPAdapter:
        def start(
            self,
            *,
            repository_id: str,
            repository_root: str,
            command: tuple[str, ...],
        ) -> TypeScriptLSPRun:
            return TypeScriptLSPRun(
                repository_id=repository_id,
                repository_root=repository_root,
                mode="stdio",
                command=command,
            )

        def connect(
            self,
            *,
            repository_id: str,
            repository_root: str,
            address: str,
        ) -> TypeScriptLSPRun:
            raise AssertionError("connect should not be used")

        def collect(
            self,
            *,
            run: TypeScriptLSPRun,
            discovered_files: tuple[object, ...],
        ) -> TypeScriptLSPRun:
            assert {file.path for file in discovered_files} == {
                "src/routes.ts",
                "src/users.ts",
            }
            return TypeScriptLSPRun(
                repository_id=run.repository_id,
                repository_root=run.repository_root,
                mode=run.mode,
                command=run.command,
                document_symbols=(
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/routes.ts",
                        name="GET",
                        kind="function",
                        qualified_name="GET",
                        start_line=3,
                        end_line=5,
                        confidence=0.92,
                    ),
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/users.ts",
                        name="listUsers",
                        kind="function",
                        qualified_name="listUsers",
                        start_line=1,
                        end_line=3,
                        confidence=0.91,
                    ),
                ),
                relationships=(
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/routes.ts",
                        source_line=4,
                        target_path="src/users.ts",
                        target_line=1,
                        kind="definition",
                        confidence=0.94,
                        metadata={"operation": "textDocument/definition"},
                    ),
                ),
            )

    create_workspace_config(tmp_path)
    route_file = tmp_path / "src" / "routes.ts"
    service_file = tmp_path / "src" / "users.ts"
    route_file.parent.mkdir()
    route_file.write_text(
        """
import { listUsers } from './users';
export async function GET() {
  return Response.json(await listUsers());
}
""",
        encoding="utf-8",
    )
    service_file.write_text(
        """
export async function listUsers() {
  return [];
}
""",
        encoding="utf-8",
    )
    route_symbol_id = "symbol:workspace:src/routes.ts:GET"
    users_symbol_id = "symbol:workspace:src/users.ts:listUsers"
    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                _symbol_active_fact(
                    id="fact:entity:tree-sitter:route",
                    source_file="src/routes.ts",
                    entity_id=route_symbol_id,
                    name="GET",
                    start_line=3,
                    end_line=5,
                    confidence=0.6,
                ),
                _symbol_active_fact(
                    id="fact:entity:tree-sitter:users",
                    source_file="src/users.ts",
                    entity_id=users_symbol_id,
                    name="listUsers",
                    start_line=1,
                    end_line=3,
                    confidence=0.6,
                ),
                _relation_active_fact(
                    id="fact:relation:tree-sitter:route-calls-users",
                    source_file="src/routes.ts",
                    source_id=route_symbol_id,
                    target_id=users_symbol_id,
                    relation="calls",
                    confidence=0.6,
                ),
            ),
        )
        connection.commit()

    scan_workspace(
        str(tmp_path),
        extractor="lsp",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        lsp_adapter=MockLSPAdapter(),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    edge_row = connection.execute(
        """
        SELECT relation_type, confidence, evidence_json
        FROM edges
        WHERE relation_type = 'calls'
        """
    ).fetchone()
    lsp_active_facts = connection.execute(
        """
        SELECT kind, source_file, evidence_json
        FROM facts
        WHERE extractor = 'lsp'
        ORDER BY kind
        """
    ).fetchall()
    connection.close()

    evidence = json.loads(edge_row["evidence_json"])
    lsp_evidence = next(item for item in evidence if item["extractor_name"] == "lsp")
    assert edge_row["confidence"] == 0.94
    assert lsp_evidence["metadata"]["validation_kind"] == "definition"
    assert lsp_evidence["metadata"]["validated_relation"] == "calls"
    assert [row["kind"] for row in lsp_active_facts] == ["calls"]
    assert all(
        json.loads(row["evidence_json"])[0]["metadata"].get("validated_fact_id")
        for row in lsp_active_facts
    )


def test_scan_graphify_extractor_invokes_configured_command(tmp_path) -> None:
    class RecordingGraphifyAdapter:
        def __init__(self) -> None:
            self.commands: list[tuple[str, tuple[str, ...]]] = []

        def run_command(
            self,
            *,
            workspace_root: str,
            command: tuple[str, ...],
        ) -> str:
            self.commands.append((workspace_root, command))
            return """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app.ts:graphify:App",
      "file_id": "file:workspace:src/app.ts",
      "name": "App",
      "kind": "function"
    }
  ]
}
"""

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    command:
      - graphify
      - --json
""",
    )
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")
    adapter = RecordingGraphifyAdapter()

    result = scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        graphify_adapter=adapter,
    )

    connection = sqlite3.connect(database_path(tmp_path))
    symbol_rows = connection.execute("SELECT name, kind FROM symbols").fetchall()
    snapshot_metadata = connection.execute(
        "SELECT metadata_json FROM snapshots WHERE id = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    connection.close()

    assert adapter.commands == [(str(tmp_path), ("graphify", "--json"))]
    assert result.extractor == "graphify"
    assert result.counts.symbols == 1
    assert symbol_rows == [("App", "function")]
    assert json.loads(snapshot_metadata) == {
        "extractor": "graphify",
        "extractor_count": 1,
        "selection": "graphify",
        "selection_type": "extractor",
        "selected_extractors": ["graphify"],
    }


def test_scan_graphify_watch_events_emit_active_facts(tmp_path) -> None:
    class WatchAdapter:
        def watch_events(self, *, workspace_root: str, watch_config: object):
            assert workspace_root == str(tmp_path)
            return (
                GraphifyWatchEvent(
                    graph_json="""
{
  "changed_graph": {
    "symbols": [
      {
        "id": "symbol:workspace:src/app.ts:graphify:App",
        "file_id": "file:workspace:src/app.ts",
        "name": "App",
        "kind": "function",
        "qualified_name": "App"
      }
    ]
  }
}
""",
                ),
            )

    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    watch:
      output_path: .code-scout/graphify-watch.jsonl
""",
    )
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
        graphify_watch_adapter=WatchAdapter(),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    fact_rows = connection.execute(
        """
        SELECT extractor, kind, source_file
        FROM facts
        WHERE extractor = 'graphify'
        ORDER BY kind
        """
    ).fetchall()
    symbol_rows = connection.execute("SELECT name, kind FROM symbols").fetchall()
    connection.close()

    assert result.warnings == ()
    assert result.counts.symbols == 1
    assert symbol_rows == [("App", "function")]
    assert ("graphify", "symbol", "src/app.ts") in fact_rows


def test_scan_graphify_watch_falls_back_to_one_shot_when_not_configured(
    tmp_path,
) -> None:
    class RecordingGraphifyAdapter:
        def __init__(self) -> None:
            self.commands: list[tuple[str, tuple[str, ...]]] = []

        def run_command(
            self,
            *,
            workspace_root: str,
            command: tuple[str, ...],
        ) -> str:
            self.commands.append((workspace_root, command))
            return """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app.ts:graphify:App",
      "file_id": "file:workspace:src/app.ts",
      "name": "App",
      "kind": "function",
      "qualified_name": "App"
    }
  ]
}
"""

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    class FailingWatchAdapter:
        def watch_events(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("watch mode should not be used")

    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    command:
      - graphify
      - --json
""",
    )
    adapter = RecordingGraphifyAdapter()
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
        graphify_adapter=adapter,
        graphify_watch_adapter=FailingWatchAdapter(),
    )

    assert adapter.commands == [(str(tmp_path), ("graphify", "--json"))]
    assert result.counts.symbols == 1
    assert result.warnings == ()


def test_scan_graphify_watch_failure_warns_without_blocking_other_extractors(
    tmp_path,
) -> None:
    class FailingWatchAdapter:
        def watch_events(self, *_args: object, **_kwargs: object) -> object:
            raise ExtractionError(
                "Graphify watch failed.",
                hint="Restart Graphify watch.",
            )

    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: false
  lsp:
    enabled: false
  graphify:
    enabled: true
    watch:
      command:
        - graphify
        - watch
  adr:
    enabled: false
  openapi:
    enabled: true
  package_metadata:
    enabled: false
""",
        encoding="utf-8",
    )
    (tmp_path / "openapi.json").write_text(
        """
{
  "openapi": "3.0.3",
  "info": {"title": "Orders API", "version": "v1"},
  "paths": {
    "/orders": {
      "get": {"operationId": "listOrders"}
    }
  }
}
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
        graphify_watch_adapter=FailingWatchAdapter(),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    graphify_fact_count = connection.execute(
        "SELECT count(*) FROM facts WHERE extractor = 'graphify'"
    ).fetchone()[0]
    connection.close()

    assert contract_count == 1
    assert graphify_fact_count == 0
    assert result.warnings[0].path == "graphify watch"
    assert result.warnings[0].message == (
        "Graphify watch failed. Restart Graphify watch."
    )


def test_scan_default_includes_graphify_when_source_is_configured(tmp_path) -> None:
    class RecordingGraphifyAdapter:
        def __init__(self) -> None:
            self.commands: list[tuple[str, tuple[str, ...]]] = []

        def run_command(
            self,
            *,
            workspace_root: str,
            command: tuple[str, ...],
        ) -> str:
            self.commands.append((workspace_root, command))
            return """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app.ts:graphify:App",
      "file_id": "file:workspace:src/app.ts",
      "name": "App",
      "kind": "function",
      "qualified_name": "App"
    }
  ]
}
"""

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    command:
      - graphify
      - --json
""",
    )
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")
    adapter = RecordingGraphifyAdapter()

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        graphify_adapter=adapter,
    )

    connection = sqlite3.connect(database_path(tmp_path))
    symbol_rows = connection.execute("SELECT name, kind FROM symbols").fetchall()
    snapshot_metadata = connection.execute(
        "SELECT metadata_json FROM snapshots WHERE id = ?",
        (result.snapshot_id,),
    ).fetchone()[0]
    connection.close()

    assert adapter.commands == [(str(tmp_path), ("graphify", "--json"))]
    assert symbol_rows == [("App", "function")]
    parsed_metadata = json.loads(snapshot_metadata)
    assert parsed_metadata["extractor"] == ""
    assert parsed_metadata["extractor_count"] == 5
    assert parsed_metadata["selection_type"] == "all_enabled"
    assert parsed_metadata["selection"] is None
    assert len(parsed_metadata["selected_extractors"]) == 5


def test_scan_keeps_tree_sitter_fallback_symbols_for_graphify_uncovered_files(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs) -> None:
            pass

        def extract(self, _discovered_files: tuple[object, ...]) -> SymbolExtraction:
            symbol = _tree_sitter_symbol("src/fallback.ts", "Fallback")
            return SymbolExtraction(
                symbols=(symbol,),
                edges=(_declares_symbol_edge("src/fallback.ts", symbol),),
            )

    class GraphifyAdapter:
        def run_command(self, *, workspace_root: str, command: tuple[str, ...]) -> str:
            return """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/covered.ts:graphify:Covered",
      "file_id": "file:workspace:src/covered.ts",
      "name": "Covered",
      "kind": "function",
      "evidence": [{"file_path": "src/covered.ts"}]
    }
  ]
}
"""

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    _write_graphify_enabled_config(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "covered.ts").write_text(
        "export function Covered() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "fallback.ts").write_text(
        "export function Fallback() {}\n",
        encoding="utf-8",
    )

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        graphify_adapter=GraphifyAdapter(),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    assert ("tree_sitter", "symbol", "src/fallback.ts") in {
        (fact.extractor, fact.kind, fact.source_file) for fact in active_facts
    }


def test_scan_uses_conventional_graphify_json_as_primary_code_source(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def extract(self, _discovered_files: tuple[object, ...]) -> SymbolExtraction:
            symbol = _tree_sitter_symbol("src/app.ts", "App")
            return SymbolExtraction(
                symbols=(symbol,),
                edges=(_declares_symbol_edge("src/app.ts", symbol),),
            )

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    create_workspace_config(tmp_path)
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")
    graphify_file = tmp_path / ".code-scout" / "graphify-out" / "graph.json"
    graphify_file.parent.mkdir()
    graphify_file.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "label": "app.ts",
                        "file_type": "code",
                        "source_file": str(app_file),
                        "source_location": "L1",
                        "id": "tmp_src_app_ts",
                    },
                    {
                        "label": "App()",
                        "file_type": "code",
                        "source_file": str(app_file),
                        "source_location": "L1",
                        "id": "app_app",
                    },
                ],
                "links": [
                    {
                        "relation": "contains",
                        "source_file": str(app_file),
                        "source_location": "L1",
                        "source": "tmp_src_app_ts",
                        "target": "app_app",
                        "confidence_score": 1.0,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    assert result.counts.symbols == 1
    assert {
        (fact.extractor, fact.kind, fact.source_file)
        for fact in active_facts
        if fact.kind == "symbol"
    } == {
        ("graphify", "symbol", "src/app.ts"),
    }


def test_scan_runs_default_graphify_update_when_no_graph_source_exists(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def extract(self, _discovered_files: tuple[object, ...]) -> SymbolExtraction:
            return SymbolExtraction()

    def fake_run(
        command: tuple[str, ...],
        *,
        cwd: str,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> object:
        commands.append((command, cwd, check, capture_output, text))
        graphify_file = tmp_path / "graphify-out" / "graph.json"
        graphify_file.parent.mkdir()
        graphify_file.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "label": "app.ts",
                            "file_type": "code",
                            "source_file": str(app_file),
                            "source_location": "L1",
                            "id": "tmp_src_app_ts",
                        },
                        {
                            "label": "App()",
                            "file_type": "code",
                            "source_file": str(app_file),
                            "source_location": "L1",
                            "id": "app_app",
                        },
                    ],
                    "links": [
                        {
                            "relation": "contains",
                            "source_file": str(app_file),
                            "source_location": "L1",
                            "source": "tmp_src_app_ts",
                            "target": "app_app",
                            "confidence_score": 1.0,
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )
        return object()

    commands: list[tuple[tuple[str, ...], str, bool, bool, bool]] = []
    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    monkeypatch.setattr(
        "code_scout.harvest.core._default_graphify_command_available",
        lambda: True,
    )
    monkeypatch.setattr("code_scout.harvest.core.subprocess.run", fake_run)
    create_workspace_config(tmp_path)
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    assert commands == [(("graphify", "update", "."), str(tmp_path), True, True, True)]
    assert not (tmp_path / "graphify-out").exists()
    assert (tmp_path / ".code-scout" / "graphify-out" / "graph.json").is_file()
    assert result.counts.symbols == 1
    assert {
        (fact.extractor, fact.kind, fact.source_file)
        for fact in active_facts
        if fact.kind == "symbol"
    } == {
        ("graphify", "symbol", "src/app.ts"),
    }


def test_default_graphify_command_is_available_when_cli_is_on_path(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "code_scout.harvest.core.shutil.which",
        lambda command: "/usr/local/bin/graphify" if command == "graphify" else None,
    )

    assert scan_module.graphify_source_configured(
        default_single_repository_workspace(),
        WorkspaceConfig(),
    )


def test_scan_drops_graphify_symbols_for_undiscovered_files(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def extract(self, _discovered_files: tuple[object, ...]) -> SymbolExtraction:
            return SymbolExtraction()

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    create_workspace_config(tmp_path)
    app_file = tmp_path / "src" / "app.ts"
    generated_file = tmp_path / "src" / "obj" / "Debug" / "Generated.g.cs"
    app_file.parent.mkdir()
    generated_file.parent.mkdir(parents=True)
    app_file.write_text("export function App() {}\n", encoding="utf-8")
    generated_file.write_text("public class Generated {}\n", encoding="utf-8")
    graphify_file = tmp_path / ".code-scout" / "graphify-out" / "graph.json"
    graphify_file.parent.mkdir(parents=True)
    graphify_file.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "label": "App()",
                        "file_type": "code",
                        "source_file": str(app_file),
                        "source_location": "L1",
                        "id": "app_app",
                    },
                    {
                        "label": "Generated",
                        "file_type": "code",
                        "source_file": "src/obj/Debug/Generated.g.cs",
                        "source_location": "L1",
                        "id": "generated",
                    },
                ],
                "links": [
                    {
                        "relation": "uses",
                        "source_file": str(app_file),
                        "source_location": "L1",
                        "source": "app_app",
                        "target": "generated",
                        "confidence_score": 1.0,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    assert result.counts.symbols == 1
    assert {
        (fact.extractor, fact.kind, fact.source_file)
        for fact in active_facts
        if fact.kind == "symbol"
    } == {
        ("graphify", "symbol", "src/app.ts"),
    }


def test_scan_keeps_tree_sitter_additive_route_contract_when_graphify_covers_file(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs) -> None:
            pass

        def extract(self, _discovered_files: tuple[object, ...]) -> SymbolExtraction:
            symbol = _tree_sitter_symbol("src/app/api/users/route.ts", "POST")
            contract = _tree_sitter_route_contract("src/app/api/users/route.ts")
            service = _tree_sitter_route_service("src/app/api/users/route.ts")
            return SymbolExtraction(
                symbols=(symbol,),
                services=(service,),
                contracts=(contract,),
                edges=(
                    _declares_symbol_edge("src/app/api/users/route.ts", symbol),
                    _declares_contract_edge("src/app/api/users/route.ts", contract),
                    _produces_contract_edge(
                        "src/app/api/users/route.ts",
                        service,
                        contract,
                    ),
                ),
            )

    class GraphifyAdapter:
        def run_command(self, *, workspace_root: str, command: tuple[str, ...]) -> str:
            return """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app/api/users/route.ts:graphify:POST",
      "file_id": "file:workspace:src/app/api/users/route.ts",
      "name": "POST",
      "kind": "function",
      "evidence": [{"file_path": "src/app/api/users/route.ts"}]
    }
  ]
}
"""

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    _write_graphify_enabled_config(tmp_path)
    route_file = tmp_path / "src" / "app" / "api" / "users" / "route.ts"
    route_file.parent.mkdir(parents=True)
    route_file.write_text("export async function POST() {}\n", encoding="utf-8")

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        graphify_adapter=GraphifyAdapter(),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    tree_sitter_facts = [
        fact for fact in active_facts if fact.extractor == "tree_sitter"
    ]
    assert {(fact.kind, fact.source_file) for fact in tree_sitter_facts} == {
        ("contract", "src/app/api/users/route.ts"),
        ("service", "src/app/api/users/route.ts"),
        ("declares_contract", "src/app/api/users/route.ts"),
        ("produces", "src/app/api/users/route.ts"),
    }
    tree_sitter_facts_by_kind = {fact.kind: fact for fact in tree_sitter_facts}
    assert tree_sitter_facts_by_kind["contract"].properties["path"] == "/api/users"
    assert (
        tree_sitter_facts_by_kind["declares_contract"].identity["relation"]
        == "declares_contract"
    )
    assert tree_sitter_facts_by_kind["produces"].identity == {
        "subject": {"type": "service", "id": "service:workspace:web"},
        "relation": "produces",
        "object": {"type": "contract", "id": "contract:workspace:http:post:api-users"},
    }
    assert tree_sitter_facts_by_kind["produces"].evidence[0].metadata["route"] == (
        "/api/users"
    )


def test_scan_suppresses_tree_sitter_code_graph_duplicates_when_graphify_covers_file(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs) -> None:
            pass

        def extract(self, _discovered_files: tuple[object, ...]) -> SymbolExtraction:
            symbol = _tree_sitter_symbol("src/app.ts", "App")
            service = Service(
                id="service:workspace:app-service",
                name="AppService",
                repository_id="workspace",
                evidence=(
                    Evidence(
                        file_path="src/app.ts",
                        extractor_name="tree_sitter",
                        confidence=1.0,
                        metadata={"repository_id": "workspace"},
                    ),
                ),
            )
            return SymbolExtraction(
                symbols=(symbol,),
                services=(service,),
                edges=(
                    _declares_symbol_edge("src/app.ts", symbol),
                    Edge(
                        source_type="symbol",
                        source_id=symbol.id,
                        target_type="service",
                        target_id=service.id,
                        relation_type="calls",
                        confidence=1.0,
                        evidence=(
                            Evidence(
                                file_path="src/app.ts",
                                extractor_name="tree_sitter",
                                confidence=1.0,
                                metadata={"repository_id": "workspace"},
                            ),
                        ),
                    ),
                ),
            )

    class GraphifyAdapter:
        def run_command(self, *, workspace_root: str, command: tuple[str, ...]) -> str:
            return """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app.ts:graphify:App",
      "file_id": "file:workspace:src/app.ts",
      "name": "App",
      "kind": "function",
      "evidence": [{"file_path": "src/app.ts"}]
    }
  ],
  "services": [
    {
      "id": "service:workspace:app-service",
      "name": "AppService",
      "repository_id": "workspace",
      "evidence": [{"file_path": "src/app.ts"}]
    }
  ]
}
"""

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    _write_graphify_enabled_config(tmp_path)
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        graphify_adapter=GraphifyAdapter(),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    assert not [
        fact
        for fact in active_facts
        if fact.extractor == "tree_sitter"
        and fact.source_file == "src/app.ts"
        and fact.kind in {"relation", "service", "symbol"}
    ]


def test_scan_graphify_extractor_loads_configured_json_file(tmp_path) -> None:
    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
""",
    )
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")
    graphify_file = tmp_path / ".code-scout" / "graphify.json"
    graphify_file.write_text(
        """
{
  "concepts": [
    {"id": "concept:graphify", "name": "Graphify"}
  ],
  "edges": [
    {
      "source_type": "file",
      "source_id": "file:workspace:src/app.ts",
      "target_type": "concept",
      "target_id": "concept:graphify",
      "relation_type": "mentions",
      "confidence": 0.75,
      "evidence": [{"file_path": "src/app.ts"}]
    }
  ]
}
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    concept_rows = connection.execute("SELECT id, name FROM concepts").fetchall()
    edge_rows = connection.execute(
        "SELECT relation_type, confidence, evidence_json FROM edges"
    ).fetchall()
    connection.close()

    assert result.counts.concepts == 1
    assert result.counts.edges == 1
    assert concept_rows == [("concept:graphify", "Graphify")]
    assert edge_rows[0][0] == "mentions"
    assert edge_rows[0][1] == 0.75
    assert json.loads(edge_rows[0][2])[0]["extractor_name"] == "graphify"


def test_scan_suppresses_tree_sitter_symbol_facts_for_graphify_files(
    tmp_path,
    monkeypatch,
) -> None:
    class FakeRegistry:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def extract(self, discovered_files: tuple[object, ...]) -> SymbolExtraction:
            assert [file.path for file in discovered_files] == ["src/app.ts"]
            symbol = Symbol(
                id="symbol:workspace:src/app.ts:tree_sitter:app",
                file_id="file:workspace:src/app.ts",
                name="App",
                kind="function",
                qualified_name="App",
                start_line=1,
                end_line=1,
            )
            return SymbolExtraction(
                symbols=(symbol,),
                edges=(
                    Edge(
                        source_type="file",
                        source_id="file:workspace:src/app.ts",
                        target_type="symbol",
                        target_id=symbol.id,
                        relation_type="declares_symbol",
                        confidence=1.0,
                        evidence=(
                            Evidence(
                                file_path="src/app.ts",
                                extractor_name="tree_sitter",
                                confidence=1.0,
                                start_line=1,
                                end_line=1,
                            ),
                        ),
                    ),
                ),
            )

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: true
  lsp:
    enabled: false
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
  adr:
    enabled: false
  openapi:
    enabled: false
  package_metadata:
    enabled: false
  symbol_extraction:
    enabled: true
""",
        encoding="utf-8",
    )
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir()
    app_file.write_text("export function App() {}\n", encoding="utf-8")
    graphify_file = tmp_path / ".code-scout" / "graphify.json"
    graphify_file.write_text(
        """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app.ts:graphify:App",
      "file_id": "file:workspace:src/app.ts",
      "name": "App",
      "kind": "function",
      "qualified_name": "App",
      "start_line": 1,
      "end_line": 1
    }
  ]
}
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)

    assert result.counts.symbols == 1
    assert {
        (fact.extractor, fact.kind, fact.identity["entity_id"])
        for fact in active_facts
        if fact.kind in {"symbol", "relation"}
    } == {
        ("graphify", "symbol", "symbol:workspace:src/app.ts:graphify:App"),
    }


def test_scan_persists_normalized_graphify_graph_sections(tmp_path) -> None:
    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
""",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "users.controller.ts").write_text(
        "import { UsersService } from './users.service';\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "users.service.ts").write_text(
        "export class UsersService {}\n",
        encoding="utf-8",
    )
    graphify_file = tmp_path / ".code-scout" / "graphify.json"
    graphify_file.write_text(
        (FIXTURES / "graphify" / "representative.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    symbol_rows = connection.execute(
        "SELECT id, name FROM symbols ORDER BY id"
    ).fetchall()
    contract_rows = connection.execute(
        "SELECT id, evidence_json FROM contracts ORDER BY id"
    ).fetchall()
    edge_rows = connection.execute(
        """
        SELECT target_id, relation_type, confidence, evidence_json
        FROM edges
        ORDER BY relation_type, target_id
        """
    ).fetchall()
    connection.close()

    assert result.counts.symbols == 3
    assert {
        (row["id"], row["name"])
        for row in symbol_rows
        if row["name"] == "UsersController"
    } == {
        ("symbol:workspace:src/users.controller.ts:userscontroller", "UsersController")
    }
    assert contract_rows[0]["id"] == "contract:workspace:http:get:users-id"
    assert json.loads(contract_rows[0]["evidence_json"])[0]["extractor_name"] == (
        "graphify"
    )
    calls_edge = next(row for row in edge_rows if row["relation_type"] == "calls")
    assert (
        calls_edge["target_id"] == "symbol:workspace:src/users.service.ts:usersservice"
    )
    assert calls_edge["confidence"] == 0.81
    assert json.loads(calls_edge["evidence_json"])[0]["extractor_name"] == "graphify"


def test_scan_populates_raw_evidence_units_from_fixture_graph_evidence(
    tmp_path,
) -> None:
    _write_graphify_config(
        tmp_path,
        """
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
""",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "users.ts").write_text(
        "export class UsersService {}\n",
        encoding="utf-8",
    )
    adr_file = tmp_path / "docs" / "adr" / "0001-use-users-api.md"
    adr_file.parent.mkdir(parents=True)
    adr_file.write_text(
        "# Use Users API\n\nStatus: Accepted\n",
        encoding="utf-8",
    )
    graphify_file = tmp_path / ".code-scout" / "graphify.json"
    graphify_file.write_text(
        json.dumps(
            {
                "services": [
                    {
                        "id": "service:workspace:users-api",
                        "name": "Users API",
                        "repository_id": "workspace",
                        "evidence": [
                            {
                                "file_path": "src/users.ts",
                                "start_line": 1,
                                "end_line": 1,
                                "confidence": 0.91,
                                "metadata": {"symbol": "UsersService"},
                            },
                        ],
                    },
                ],
                "symbols": [
                    {
                        "id": "symbol:workspace:src/users.ts:UsersService",
                        "file_id": "file:workspace:src/users.ts",
                        "name": "UsersService",
                        "kind": "class",
                        "qualified_name": "UsersService",
                    },
                ],
                "contracts": [
                    {
                        "id": "contract:workspace:graphify:get:users",
                        "name": "GET /users",
                        "kind": "http",
                        "method": "GET",
                        "path": "/users",
                        "evidence": [
                            {
                                "file_path": "src/users.ts",
                                "start_line": 1,
                                "end_line": 1,
                                "confidence": 0.88,
                            },
                        ],
                    },
                ],
                "concepts": [
                    {
                        "id": "concept:workspace:users",
                        "name": "Users",
                        "description": "User account workflows.",
                    },
                ],
                "decisions": [
                    {
                        "id": "decision:workspace:docs/adr/0001-use-users-api.md",
                        "title": "Use Users API",
                        "status": "accepted",
                        "path": "docs/adr/0001-use-users-api.md",
                        "evidence": [
                            {
                                "file_path": "docs/adr/0001-use-users-api.md",
                                "start_line": 1,
                                "end_line": 3,
                                "confidence": 0.96,
                            },
                        ],
                    },
                ],
                "edges": [
                    {
                        "source_type": "file",
                        "source_id": "file:workspace:src/users.ts",
                        "target_type": "symbol",
                        "target_id": "symbol:workspace:src/users.ts:UsersService",
                        "relation_type": "declares_symbol",
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "file_path": "src/users.ts",
                                "start_line": 1,
                                "end_line": 1,
                                "confidence": 0.9,
                            },
                        ],
                    },
                    {
                        "source_type": "service",
                        "source_id": "service:workspace:users-api",
                        "target_type": "contract",
                        "target_id": "contract:workspace:graphify:get:users",
                        "relation_type": "serves",
                        "confidence": 0.84,
                        "evidence": [
                            {
                                "file_path": "src/users.ts",
                                "start_line": 1,
                                "end_line": 1,
                                "confidence": 0.84,
                            },
                        ],
                    },
                    {
                        "source_type": "decision",
                        "source_id": (
                            "decision:workspace:docs/adr/0001-use-users-api.md"
                        ),
                        "target_type": "concept",
                        "target_id": "concept:workspace:users",
                        "relation_type": "affects",
                        "confidence": 0.83,
                        "evidence": [
                            {
                                "file_path": "docs/adr/0001-use-users-api.md",
                                "start_line": 1,
                                "end_line": 3,
                                "confidence": 0.83,
                            },
                        ],
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    embedding_provider = _CountingEmbeddingProvider()
    scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        embedding_provider=embedding_provider,
    )
    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT owner_entity_type, owner_entity_id, source_file, text
        FROM raw_evidence_units
        ORDER BY owner_entity_type, owner_entity_id, source_file
        """,
    ).fetchall()
    embedding_rows = connection.execute(
        """
        SELECT evidence_unit_id, model_id, vector_json, dimensions, text_hash
        FROM evidence_embeddings
        ORDER BY evidence_unit_id
        """,
    ).fetchall()
    first_count = len(rows)
    connection.close()
    scan_workspace(
        str(tmp_path),
        extractor="graphify",
        created_at=datetime(2026, 6, 20, 10, 1, tzinfo=UTC),
        embedding_provider=embedding_provider,
    )
    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    repeated_count = connection.execute(
        "SELECT count(*) FROM raw_evidence_units",
    ).fetchone()[0]
    repeated_embedding_rows = connection.execute(
        """
        SELECT evidence_unit_id, model_id, vector_json, dimensions, text_hash
        FROM evidence_embeddings
        ORDER BY evidence_unit_id
        """,
    ).fetchall()
    connection.close()

    owner_sources = {(row["owner_entity_type"], row["source_file"]) for row in rows}
    assert owner_sources >= {
        ("file", "src/users.ts"),
        ("symbol", "src/users.ts"),
        ("service", "src/users.ts"),
        ("contract", "src/users.ts"),
        ("concept", "docs/adr/0001-use-users-api.md"),
        ("decision", "docs/adr/0001-use-users-api.md"),
        ("relationship", "src/users.ts"),
    }
    assert all(json.loads(row["text"])["kind"] for row in rows)
    assert all("summary" not in row["text"].lower() for row in rows)
    assert repeated_count == first_count
    assert len(embedding_rows) == first_count
    assert [tuple(row) for row in repeated_embedding_rows] == [
        tuple(row) for row in embedding_rows
    ]
    assert len(embedding_provider.texts) == first_count
    assert {row["model_id"] for row in embedding_rows} == {"test-scan-provider"}
    assert {row["dimensions"] for row in embedding_rows} == {4}
    assert all(len(row["text_hash"]) == 64 for row in embedding_rows)
    assert all(len(json.loads(row["vector_json"])) == 4 for row in embedding_rows)


def test_scan_persists_services_and_contracts_resolved_from_active_facts(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                ActiveFact(
                    id="fact:entity:docs:billing-service",
                    extractor="docs",
                    kind="service",
                    source_file="docs/services.md",
                    identity={
                        "entity_type": "service",
                        "entity_id": "service:billing",
                    },
                    properties={
                        "name": "Billing",
                        "repository_id": "workspace",
                        "description": "Owns billing workflows.",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/services.md",
                            extractor_name="docs",
                            confidence=0.82,
                            metadata={"heading": "Billing"},
                        ),
                    ),
                    confidence=0.82,
                ),
                ActiveFact(
                    id="fact:entity:frontend-api:list-payments",
                    extractor="frontend_api",
                    kind="contract",
                    source_file="apps/web/src/payments.ts",
                    identity={
                        "entity_type": "contract",
                        "entity_id": "contract:workspace:frontend_api:get:payments",
                    },
                    properties={
                        "name": "GET /payments",
                        "kind": "http",
                        "path": "/payments",
                        "method": "GET",
                        "response_dtos": ("PaymentListResponse",),
                    },
                    evidence=(
                        FactEvidence(
                            file_path="apps/web/src/payments.ts",
                            extractor_name="frontend_api",
                            confidence=0.74,
                            metadata={
                                "repository_id": "workspace",
                                "call_kind": "fetch",
                            },
                        ),
                    ),
                    confidence=0.74,
                ),
            ),
        )
        connection.commit()

    result = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    service_rows = connection.execute(
        """
        SELECT id, repository_id, name, description, evidence_json
        FROM services
        """
    ).fetchall()
    contract_rows = connection.execute(
        """
        SELECT id, name, kind, path, method, response_dtos_json, evidence_json
        FROM contracts
        """
    ).fetchall()
    connection.close()

    assert result.counts.services == 1
    assert result.counts.contracts == 1
    assert [dict(row) | {"evidence_json": "<json>"} for row in service_rows] == [
        {
            "id": "service:billing",
            "repository_id": "workspace",
            "name": "Billing",
            "description": "Owns billing workflows.",
            "evidence_json": "<json>",
        }
    ]
    assert json.loads(service_rows[0]["evidence_json"]) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"heading": "Billing"}, extractor_provenance=("docs",)
                    ).to_dict()
                )
            ),
            "file_path": "docs/services.md",
            "extractor_name": "docs",
            "confidence": 0.82,
            "start_line": None,
            "end_line": None,
            "metadata": {"heading": "Billing"},
        }
    ]
    assert [dict(row) | {"evidence_json": "<json>"} for row in contract_rows] == [
        {
            "id": "contract:workspace:http:get:payments",
            "name": "GET /payments",
            "kind": "http",
            "path": "/payments",
            "method": "GET",
            "response_dtos_json": '["PaymentListResponse"]',
            "evidence_json": "<json>",
        }
    ]
    assert json.loads(contract_rows[0]["evidence_json"])[0]["extractor_name"] == (
        "frontend_api"
    )


def test_scan_profile_resolves_current_graph_from_all_active_facts(tmp_path) -> None:
    create_workspace_config(tmp_path)
    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                ActiveFact(
                    id="fact:entity:graphify:existing-service",
                    extractor="graphify",
                    kind="service",
                    source_file="graphify.json",
                    identity={
                        "entity_type": "service",
                        "entity_id": "service:workspace:existing",
                    },
                    properties={
                        "name": "Existing",
                        "repository_id": "workspace",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="graphify.json",
                            extractor_name="graphify",
                            confidence=0.91,
                            metadata={"repository_id": "workspace"},
                        ),
                    ),
                    confidence=0.91,
                ),
            ),
        )
        connection.commit()

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "concept.md").write_text(
        """# Checkout

Checkout owns cart completion.
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        profile="docs",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    service_rows = connection.execute(
        "SELECT id, name FROM services ORDER BY id",
    ).fetchall()
    concept_rows = connection.execute(
        "SELECT id, name FROM concepts ORDER BY id",
    ).fetchall()
    snapshot_metadata = connection.execute(
        "SELECT metadata_json, payload_sha256 FROM snapshots WHERE id = ?",
        (result.snapshot_id,),
    ).fetchone()
    payload = get_snapshot_payload(connection, snapshot_metadata["payload_sha256"])
    active_facts = list_active_facts(connection)
    connection.close()

    assert result.extractor == "docs"
    assert [dict(row) for row in service_rows] == [
        {"id": "service:workspace:existing", "name": "Existing"},
    ]
    assert [dict(row) for row in concept_rows] == [
        {"id": "concept:workspace:checkout", "name": "Checkout"},
    ]
    assert json.loads(snapshot_metadata["metadata_json"]) == {
        "extractor": "docs",
        "extractor_count": 3,
        "selection": "docs",
        "selection_type": "profile",
        "selected_extractors": ["adr", "docs", "openapi"],
    }
    assert payload is not None
    assert payload.scan_scope == {
        "extractor": "docs",
        "selection": "docs",
        "selection_type": "profile",
        "selected_extractors": ["adr", "docs", "openapi"],
    }
    assert (
        "fact:entity:graphify:existing-service"
        in payload.coverage["inherited_active_fact_ids"]
    )
    assert "graphify.json" not in payload.coverage["refreshed_source_files"]
    assert payload.coverage["refreshed_source_files"] == [".", "docs/concept.md"]
    assert ("graphify", "service", "graphify.json") in {
        (fact.extractor, fact.kind, fact.source_file) for fact in active_facts
    }
    assert ("docs", "concept", "docs/concept.md") in {
        (fact.extractor, fact.kind, fact.source_file) for fact in active_facts
    }


def test_snapshots_retain_first_scan_bytes_and_revisions_after_rescan_deletion(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    source_file = tmp_path / "retained.py"
    source_file.write_text("def retained():\n    return 1\n", encoding="utf-8")

    first = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )
    with open_storage(tmp_path) as connection:
        first_row = connection.execute(
            "SELECT payload_sha256 FROM snapshots WHERE id = ?", (first.snapshot_id,)
        ).fetchone()
        assert first_row is not None
        first_bytes = (
            connection.execute(
                "SELECT payload_json FROM snapshot_payloads WHERE content_sha256 = ?",
                (first_row["payload_sha256"],),
            )
            .fetchone()[0]
            .encode("utf-8")
        )
        first_payload = get_snapshot_payload(connection, first_row["payload_sha256"])

    assert first_payload is not None
    revision_ids = {reference["id"] for reference in first_payload.revision_references}
    assert revision_ids

    source_file.unlink()
    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 21, 10, 1, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        assert all(
            fact.source_file != "retained.py" for fact in list_active_facts(connection)
        )
        assert all(
            connection.execute(
                "SELECT 1 FROM source_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            is not None
            for revision_id in revision_ids
        )
        first_bytes_after = (
            connection.execute(
                "SELECT payload_json FROM snapshot_payloads WHERE content_sha256 = ?",
                (first_row["payload_sha256"],),
            )
            .fetchone()[0]
            .encode("utf-8")
        )
        assert (
            get_snapshot_payload(connection, first_row["payload_sha256"])
            == first_payload
        )

    assert first_bytes_after == first_bytes


def test_explicit_package_rescan_replaces_selected_source_bucket_and_snapshot(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    package_json = tmp_path / "package.json"
    package_json.write_text('{"name": "version-a"}', encoding="utf-8")

    first = scan_workspace(
        str(tmp_path),
        extractor="package_metadata",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )
    with open_storage(tmp_path) as connection:
        first_row = connection.execute(
            "SELECT payload_sha256 FROM snapshots WHERE id = ?", (first.snapshot_id,)
        ).fetchone()
        assert first_row is not None
        first_payload = get_snapshot_payload(connection, first_row["payload_sha256"])

    package_json.write_text('{"name": "version-b"}', encoding="utf-8")
    second = scan_workspace(
        str(tmp_path),
        extractor="package_metadata",
        created_at=datetime(2026, 6, 21, 10, 1, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        active_facts = list_active_facts(connection)
        second_row = connection.execute(
            "SELECT payload_sha256 FROM snapshots WHERE id = ?", (second.snapshot_id,)
        ).fetchone()
        assert second_row is not None
        second_payload = get_snapshot_payload(connection, second_row["payload_sha256"])

    assert first_payload is not None
    assert second_payload is not None
    assert any(
        fact["properties"].get("name") == "version-a"
        for fact in first_payload.active_facts
    )
    assert all(fact.properties.get("name") != "version-a" for fact in active_facts)
    assert all(
        fact["properties"].get("name") != "version-a"
        for fact in second_payload.active_facts
    )
    assert any(fact.properties.get("name") == "version-b" for fact in active_facts)
    assert "package.json" in second_payload.coverage["refreshed_source_files"]


def test_explicit_rescan_replaces_successful_empty_result_but_keeps_failed_source(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    document = docs / "checkout.md"
    document.write_text("# Checkout\n\nCheckout owns payment.\n", encoding="utf-8")
    scan_workspace(str(tmp_path), extractor="docs")
    document.write_text("", encoding="utf-8")
    empty_result = scan_workspace(str(tmp_path), extractor="docs")
    with open_storage(tmp_path) as connection:
        empty_row = connection.execute(
            "SELECT payload_sha256 FROM snapshots WHERE id = ?",
            (empty_result.snapshot_id,),
        ).fetchone()
        assert empty_row is not None
        empty_payload = get_snapshot_payload(connection, empty_row["payload_sha256"])

    openapi = tmp_path / "openapi.yaml"
    openapi.write_text(
        """openapi: 3.0.0
info:
  title: Checkout
  version: 1
paths:
  /checkout:
    get:
      responses:
        '200':
          description: ok
""",
        encoding="utf-8",
    )
    scan_workspace(str(tmp_path), extractor="openapi")
    with open_storage(tmp_path) as connection:
        before_failed_rescan = tuple(
            fact.id
            for fact in list_active_facts(connection)
            if fact.extractor == "openapi"
        )
    openapi.write_text("openapi: [", encoding="utf-8")
    failed_result = scan_workspace(str(tmp_path), extractor="openapi")

    with open_storage(tmp_path) as connection:
        assert not any(
            fact.extractor == "docs" and fact.source_file == "docs/checkout.md"
            for fact in list_active_facts(connection)
        )
        assert (
            tuple(
                fact.id
                for fact in list_active_facts(connection)
                if fact.extractor == "openapi"
            )
            == before_failed_rescan
        )

    assert empty_payload is not None
    assert "docs/checkout.md" in empty_payload.coverage["refreshed_source_files"]
    assert failed_result.warnings


def test_snapshot_publication_failure_rolls_back_the_scan_transaction(
    tmp_path, monkeypatch
) -> None:
    create_workspace_config(tmp_path)
    source_file = tmp_path / "service.py"
    source_file.write_text("def before():\n    return 1\n", encoding="utf-8")
    scan_workspace(
        str(tmp_path),
        extractor="symbol_extraction",
        created_at=datetime(2026, 6, 21, 11, 0, tzinfo=UTC),
    )
    with open_storage(tmp_path) as connection:
        before_fact_ids = tuple(fact.id for fact in list_active_facts(connection))
        before_snapshot_count = connection.execute(
            "SELECT count(*) FROM snapshots"
        ).fetchone()[0]

    source_file.write_text("def after():\n    return 2\n", encoding="utf-8")

    def fail_snapshot_publication(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated snapshot publication failure")

    monkeypatch.setattr(scan_module, "create_snapshot", fail_snapshot_publication)
    with pytest.raises(RuntimeError, match="simulated snapshot publication failure"):
        scan_workspace(
            str(tmp_path),
            extractor="symbol_extraction",
            created_at=datetime(2026, 6, 21, 11, 1, tzinfo=UTC),
        )

    with open_storage(tmp_path) as connection:
        assert (
            tuple(fact.id for fact in list_active_facts(connection)) == before_fact_ids
        )
        assert (
            connection.execute("SELECT count(*) FROM snapshots").fetchone()[0]
            == before_snapshot_count
        )


def test_scan_persists_remaining_graph_types_resolved_from_active_facts(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                ActiveFact(
                    id="fact:entity:docs:repository",
                    extractor="docs",
                    kind="repository",
                    source_file="docs/repository.md",
                    identity={
                        "entity_type": "repository",
                        "entity_id": "workspace",
                    },
                    properties={
                        "name": "Workspace",
                        "path": ".",
                        "language_hints": ("Python",),
                        "framework_hints": ("pytest",),
                        "module_hints": ("tests",),
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/repository.md",
                            extractor_name="docs",
                            confidence=0.82,
                        ),
                    ),
                    confidence=0.82,
                ),
                ActiveFact(
                    id="fact:entity:docs:file",
                    extractor="docs",
                    kind="file",
                    source_file="docs/repository.md",
                    identity={
                        "entity_type": "file",
                        "entity_id": "file:workspace:src/app.py",
                    },
                    properties={
                        "repository_id": "workspace",
                        "path": "src/app.py",
                        "kind": "source",
                        "extension": ".py",
                        "language": "Python",
                        "content_hash": "abc123",
                        "modified_at": "2026-06-20T10:00:00Z",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/repository.md",
                            extractor_name="docs",
                            confidence=0.82,
                        ),
                    ),
                    confidence=0.82,
                ),
                ActiveFact(
                    id="fact:entity:lsp:symbol",
                    extractor="lsp",
                    kind="symbol",
                    source_file="src/app.py",
                    identity={
                        "entity_type": "symbol",
                        "entity_id": "symbol:workspace:src/app.py:lsp:App",
                    },
                    properties={
                        "file_id": "file:workspace:src/app.py",
                        "name": "App",
                        "kind": "function",
                        "qualified_name": "app.App",
                        "start_line": 1,
                        "end_line": 3,
                    },
                    evidence=(
                        FactEvidence(
                            file_path="src/app.py",
                            extractor_name="lsp",
                            confidence=0.85,
                        ),
                    ),
                    confidence=0.85,
                ),
                ActiveFact(
                    id="fact:entity:docs:concept",
                    extractor="docs",
                    kind="concept",
                    source_file="docs/concepts.md",
                    identity={
                        "entity_type": "concept",
                        "entity_id": "concept:workspace:app",
                    },
                    properties={
                        "name": "App",
                        "description": "Application entrypoint.",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/concepts.md",
                            extractor_name="docs",
                            confidence=0.82,
                        ),
                    ),
                    confidence=0.82,
                ),
                ActiveFact(
                    id="fact:entity:adr:decision",
                    extractor="adr_markdown",
                    kind="decision",
                    source_file="docs/adr/0001-app.md",
                    identity={
                        "entity_type": "decision",
                        "entity_id": "decision:workspace:docs/adr/0001-app.md",
                    },
                    properties={
                        "title": "Keep App Small",
                        "status": "accepted",
                        "adr_id": "1",
                        "path": "docs/adr/0001-app.md",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/adr/0001-app.md",
                            extractor_name="adr_markdown",
                            confidence=1.0,
                        ),
                    ),
                    confidence=1.0,
                ),
                ActiveFact(
                    id="fact:relation:docs:concept-decision",
                    extractor="docs",
                    kind="decided_by",
                    source_file="docs/concepts.md",
                    identity={
                        "subject": {
                            "type": "concept",
                            "id": "concept:workspace:app",
                        },
                        "relation": "decided_by",
                        "object": {
                            "type": "decision",
                            "id": "decision:workspace:docs/adr/0001-app.md",
                        },
                    },
                    properties={},
                    evidence=(
                        FactEvidence(
                            file_path="docs/concepts.md",
                            extractor_name="docs",
                            confidence=0.82,
                        ),
                    ),
                    confidence=0.82,
                ),
            ),
        )
        connection.commit()

    result = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    repository_row = connection.execute(
        "SELECT id, name, language_hints_json FROM repositories WHERE id = ?",
        ("workspace",),
    ).fetchone()
    file_row = connection.execute(
        "SELECT id, path, language, content_hash FROM files WHERE id = ?",
        ("file:workspace:src/app.py",),
    ).fetchone()
    symbol_row = connection.execute(
        "SELECT id, name, qualified_name FROM symbols"
    ).fetchone()
    concept_row = connection.execute(
        "SELECT id, name, description FROM concepts"
    ).fetchone()
    decision_row = connection.execute(
        "SELECT id, title, status FROM decisions"
    ).fetchone()
    edge_row = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type
        FROM edges
        """
    ).fetchone()
    connection.close()

    assert result.counts.files == 1
    assert result.counts.symbols == 1
    assert result.counts.concepts == 1
    assert result.counts.decisions == 1
    assert result.counts.edges == 1
    assert dict(repository_row) == {
        "id": "workspace",
        "name": "workspace",
        "language_hints_json": '["Python"]',
    }
    assert dict(file_row) == {
        "id": "file:workspace:src/app.py",
        "path": "src/app.py",
        "language": "Python",
        "content_hash": "abc123",
    }
    assert dict(symbol_row) == {
        "id": "symbol:workspace:src/app.py:app-app",
        "name": "App",
        "qualified_name": "app.App",
    }
    assert dict(concept_row) == {
        "id": "concept:workspace:app",
        "name": "App",
        "description": "Application entrypoint.",
    }
    assert dict(decision_row) == {
        "id": "decision:workspace:docs/adr/0001-app.md",
        "title": "Keep App Small",
        "status": "accepted",
    }
    assert dict(edge_row) == {
        "source_type": "concept",
        "source_id": "concept:workspace:app",
        "target_type": "decision",
        "target_id": "decision:workspace:docs/adr/0001-app.md",
        "relation_type": "decided_by",
    }


def test_scan_openapi_extractor_preserves_other_active_graph_facts(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    openapi_file = tmp_path / "openapi.yaml"
    openapi_file.write_text(
        """
openapi: 3.0.0
info:
  title: Payments API
  version: 1.0.0
paths:
  /payments:
    get:
      operationId: listPayments
      responses:
        '200':
          description: OK
""",
        encoding="utf-8",
    )
    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                ActiveFact(
                    id="fact:entity:graphify:billing-service",
                    extractor="graphify",
                    kind="service",
                    source_file=".code-scout/graphify.json",
                    identity={
                        "entity_type": "service",
                        "entity_id": "service:billing",
                    },
                    properties={
                        "name": "Billing",
                        "repository_id": "workspace",
                        "description": "Owns billing workflows.",
                    },
                    evidence=(
                        FactEvidence(
                            file_path=".code-scout/graphify.json",
                            extractor_name="graphify",
                            confidence=0.91,
                            metadata={"node": "Billing"},
                        ),
                    ),
                    confidence=0.91,
                ),
                ActiveFact(
                    id="fact:entity:adr:decision",
                    extractor="adr_markdown",
                    kind="decision",
                    source_file="docs/adr/0001-billing.md",
                    identity={
                        "entity_type": "decision",
                        "entity_id": "decision:workspace:docs/adr/0001-billing.md",
                    },
                    properties={
                        "title": "Keep Billing Separate",
                        "status": "accepted",
                        "adr_id": "1",
                        "path": "docs/adr/0001-billing.md",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/adr/0001-billing.md",
                            extractor_name="adr_markdown",
                            confidence=1.0,
                        ),
                    ),
                    confidence=1.0,
                ),
            ),
        )
        connection.commit()

    result = scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    service_row = connection.execute(
        "SELECT id, name, description, evidence_json FROM services WHERE id = ?",
        ("service:billing",),
    ).fetchone()
    decision_row = connection.execute(
        "SELECT id, title, status, evidence_json FROM decisions WHERE id = ?",
        ("decision:workspace:docs/adr/0001-billing.md",),
    ).fetchone()
    contract_rows = connection.execute(
        "SELECT id, method, path FROM contracts ORDER BY id",
    ).fetchall()
    active_facts = list_active_facts(connection)
    connection.close()

    assert result.extractor == "openapi"
    assert result.counts.services == 1
    assert result.counts.decisions == 1
    assert result.counts.contracts == 1
    assert dict(service_row) | {"evidence_json": "<json>"} == {
        "id": "service:billing",
        "name": "Billing",
        "description": "Owns billing workflows.",
        "evidence_json": "<json>",
    }
    assert json.loads(service_row["evidence_json"])[0]["extractor_name"] == "graphify"
    assert dict(decision_row) | {"evidence_json": "<json>"} == {
        "id": "decision:workspace:docs/adr/0001-billing.md",
        "title": "Keep Billing Separate",
        "status": "accepted",
        "evidence_json": "<json>",
    }
    assert json.loads(decision_row["evidence_json"])[0]["extractor_name"] == (
        "adr_markdown"
    )
    assert [dict(row) for row in contract_rows] == [
        {
            "id": "contract:workspace:http:get:payments",
            "method": "GET",
            "path": "/payments",
        },
    ]
    assert {
        ("openapi", "contract", "openapi.yaml"),
        ("graphify", "service", ".code-scout/graphify.json"),
        ("adr_markdown", "decision", "docs/adr/0001-billing.md"),
    } <= {(fact.extractor, fact.kind, fact.source_file) for fact in active_facts}


def test_scan_removes_current_graph_rows_after_last_active_fact_is_removed(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                ActiveFact(
                    id="fact:entity:docs:billing-service",
                    extractor="docs",
                    kind="service",
                    source_file="docs/architecture.md",
                    identity={
                        "entity_type": "service",
                        "entity_id": "service:billing",
                    },
                    properties={
                        "name": "Billing",
                        "repository_id": "workspace",
                    },
                    evidence=(
                        FactEvidence(
                            file_path="docs/architecture.md",
                            extractor_name="docs",
                            confidence=0.82,
                        ),
                    ),
                    confidence=0.82,
                ),
                ActiveFact(
                    id="fact:relation:docs:billing-depends-on-payments",
                    extractor="docs",
                    kind="depends_on",
                    source_file="docs/architecture.md",
                    identity={
                        "subject": {"type": "service", "id": "service:billing"},
                        "relation": "depends_on",
                        "object": {"type": "service", "id": "service:payments"},
                    },
                    properties={},
                    evidence=(
                        FactEvidence(
                            file_path="docs/architecture.md",
                            extractor_name="docs",
                            confidence=0.72,
                        ),
                    ),
                    confidence=0.72,
                ),
            ),
        )
        connection.commit()

    first_result = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    with open_storage(tmp_path) as connection:
        assert first_result.counts.services == 1
        assert first_result.counts.edges == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM services WHERE id = ?",
                ("service:billing",),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM edges").fetchone()[0] == 1
        delete_active_facts_by_extractor_and_source_file(
            connection,
            extractor="docs",
            source_file="docs/architecture.md",
        )
        connection.commit()

    second_result = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 1, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    service_count = connection.execute(
        "SELECT count(*) FROM services WHERE id = ?",
        ("service:billing",),
    ).fetchone()[0]
    edge_count = connection.execute("SELECT count(*) FROM edges").fetchone()[0]
    snapshot_count = connection.execute("SELECT count(*) FROM snapshots").fetchone()[0]
    connection.close()

    assert second_result.counts.services == 0
    assert second_result.counts.edges == 0
    assert service_count == 0
    assert edge_count == 0
    assert snapshot_count == 2


def test_replace_changed_file_active_facts_replaces_one_changed_file(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Payments API
  version: v2
paths:
  /payments:
    post:
      operationId: createPayment
""",
        encoding="utf-8",
    )

    result = replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    contract_rows = connection.execute(
        "SELECT id, method, path, operation_id FROM contracts ORDER BY id",
    ).fetchall()
    fact_rows = connection.execute(
        """
        SELECT extractor, source_file
        FROM facts
        WHERE source_file = ?
        ORDER BY extractor
        """,
        ("openapi.yaml",),
    ).fetchall()
    change_events = list_change_events(connection)
    connection.close()

    assert result.facts_removed > 0
    assert result.facts_added > 0
    assert result.nodes_updated > 0
    assert len(change_events) == 1
    assert change_events[0].type == "incremental_change"
    assert change_events[0].file == "openapi.yaml"
    assert change_events[0].extractors_run == ("openapi",)
    assert change_events[0].facts_added == result.facts_added
    assert change_events[0].facts_removed == result.facts_removed
    assert change_events[0].nodes_updated == result.nodes_updated
    assert change_events[0].outputs_rebuilt is False
    assert [dict(row) for row in contract_rows] == [
        {
            "id": "contract:workspace:http:post:payments",
            "method": "POST",
            "path": "/payments",
            "operation_id": "createPayment",
        },
    ]
    assert {tuple(row) for row in fact_rows} == {
        ("file_discovery", "openapi.yaml"),
        ("openapi", "openapi.yaml"),
    }


def test_replace_changed_file_active_facts_refreshes_raw_evidence_and_embeddings(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Payments API
  version: v2
paths:
  /payments:
    post:
      operationId: createPayment
""",
        encoding="utf-8",
    )

    replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    with open_storage(tmp_path) as connection:
        all_raw_units = list_raw_evidence_units(connection)
        raw_units = tuple(
            unit for unit in all_raw_units if unit.source_file == "openapi.yaml"
        )
        embeddings = list_evidence_embeddings(connection)

    raw_unit_ids = {unit.id for unit in raw_units}
    all_raw_unit_ids = {unit.id for unit in all_raw_units}
    embedded_unit_ids = {embedding.evidence_unit_id for embedding in embeddings}
    raw_text = "\n".join(unit.text for unit in raw_units)
    assert raw_units
    assert "Payments API" in raw_text
    assert "createPayment" in raw_text
    assert "Orders API" not in raw_text
    assert "listOrders" not in raw_text
    assert raw_unit_ids.issubset(embedded_unit_ids)
    assert embedded_unit_ids == all_raw_unit_ids
    assert {embedding.model_id for embedding in embeddings} == {
        "code-scout-local-hash-v1"
    }
    assert {embedding.dimensions for embedding in embeddings} == {128}
    assert {embedding.text_hash for embedding in embeddings} == {
        evidence_text_hash(unit.text) for unit in all_raw_units
    }


def test_replace_changed_file_active_facts_records_graph_change_event(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    package_file = tmp_path / "package.json"
    package_file.write_text(
        '{"name":"orders","dependencies":{"express":"latest"}}\n',
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="package_metadata",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    package_file.write_text(
        '{"name":"orders","dependencies":{"next":"latest"}}\n',
        encoding="utf-8",
    )

    replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("package.json",),
        selected_extractors=("package_metadata",),
    )

    with open_storage(tmp_path) as connection:
        change_events = list_change_events(connection)

    assert len(change_events) == 1
    assert change_events[0].metadata == {}
    assert change_events[0].outputs_rebuilt is False


def test_replace_changed_file_active_facts_preserves_unrelated_facts(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "openapi.yaml").write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    unrelated_fact = ActiveFact(
        id="fact:entity:docs:billing-service",
        extractor="docs",
        kind="service",
        source_file="docs/architecture.md",
        identity={"entity_type": "service", "entity_id": "service:billing"},
        properties={
            "name": "Billing",
            "repository_id": "workspace",
            "description": "Owns billing workflows.",
        },
        evidence=(
            FactEvidence(
                file_path="docs/architecture.md",
                extractor_name="docs",
                confidence=0.82,
            ),
        ),
        confidence=0.82,
    )
    with open_storage(tmp_path) as connection:
        upsert_active_facts(connection, (unrelated_fact,))
        connection.commit()

    replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    service_row = connection.execute(
        "SELECT id, name, description FROM services WHERE id = ?",
        ("service:billing",),
    ).fetchone()
    active_facts = list_active_facts(connection)
    connection.close()

    assert dict(service_row) == {
        "id": "service:billing",
        "name": "Billing",
        "description": "Owns billing workflows.",
    }
    assert unrelated_fact in active_facts


def test_replace_changed_file_active_facts_resolves_full_active_fact_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import code_scout.pipeline.incremental as incremental_module

    create_workspace_config(tmp_path)
    (tmp_path / "openapi.yaml").write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    unrelated_fact = ActiveFact(
        id="fact:entity:docs:billing-service",
        extractor="docs",
        kind="service",
        source_file="docs/architecture.md",
        identity={"entity_type": "service", "entity_id": "service:billing"},
        properties={"name": "Billing", "repository_id": "workspace"},
        evidence=(
            FactEvidence(
                file_path="docs/architecture.md",
                extractor_name="docs",
                confidence=0.82,
            ),
        ),
        confidence=0.82,
    )
    with open_storage(tmp_path) as connection:
        upsert_active_facts(connection, (unrelated_fact,))
        connection.commit()

    real_resolve = incremental_module.resolve_active_facts
    resolver_inputs: list[tuple[ActiveFact, ...]] = []

    def spy_resolve(facts: tuple[ActiveFact, ...]):
        resolver_inputs.append(facts)
        return real_resolve(facts)

    monkeypatch.setattr(incremental_module, "resolve_active_facts", spy_resolve)

    replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    assert any(
        unrelated_fact in facts and any(fact.extractor == "openapi" for fact in facts)
        for facts in resolver_inputs
    )


def test_incremental_openapi_and_adr_harvest_matches_full_scan_graph(
    tmp_path,
) -> None:
    full_workspace = tmp_path / "full"
    incremental_workspace = tmp_path / "incremental"
    for workspace_path in (full_workspace, incremental_workspace):
        workspace_path.mkdir()
        create_workspace_config(workspace_path)
        (workspace_path / "openapi.yaml").write_text(
            """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
      tags:
        - Orders
""",
            encoding="utf-8",
        )
        adr_dir = workspace_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        (adr_dir / "0001-use-orders-api.md").write_text(
            """# ADR 0001: Use Orders API

Date: 2026-06-20
Status: Accepted

## Context

Orders need a stable contract.

## Decision

Use OpenAPI as the source of truth.
""",
            encoding="utf-8",
        )

    scan_workspace(str(full_workspace), extractor="openapi")
    scan_workspace(str(full_workspace), extractor="adr")
    replace_changed_file_active_facts(
        str(incremental_workspace),
        changed_file_paths=(
            "openapi.yaml",
            "docs/adr/0001-use-orders-api.md",
        ),
        selected_extractors=("openapi", "adr"),
    )

    assert _current_graph_state(incremental_workspace) == _current_graph_state(
        full_workspace
    )


def test_incremental_symbol_harvest_matches_full_scan_graph(
    tmp_path,
) -> None:
    full_workspace = tmp_path / "full"
    incremental_workspace = tmp_path / "incremental"
    source_fixture = FIXTURES / "typescript-monorepo"
    for workspace_path in (full_workspace, incremental_workspace):
        shutil.copytree(source_fixture, workspace_path)
        _write_config(
            workspace_path,
            """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  graphify:
    enabled: false
  lsp:
    enabled: false
  tree_sitter:
    enabled: true
  symbol_extraction:
    enabled: true
  package_metadata:
    enabled: false
  openapi:
    enabled: false
  adr:
    enabled: false
  asyncapi:
    enabled: false
  infra:
    enabled: false
  docs:
    enabled: false
""",
        )

    changed_paths = tuple(
        sorted(
            path.relative_to(source_fixture).as_posix()
            for path in source_fixture.rglob("*")
            if path.is_file()
        )
    )

    scan_workspace(str(full_workspace), extractor="tree_sitter")
    replace_changed_file_active_facts(
        str(incremental_workspace),
        changed_file_paths=changed_paths,
        selected_extractors=("tree_sitter", "symbol_extraction"),
    )

    assert _current_graph_state(incremental_workspace) == _current_graph_state(
        full_workspace
    )


def test_incremental_graphify_harvest_matches_full_scan_graph(
    tmp_path,
) -> None:
    full_workspace = tmp_path / "full"
    incremental_workspace = tmp_path / "incremental"
    for workspace_path in (full_workspace, incremental_workspace):
        (workspace_path / "src").mkdir(parents=True)
        (workspace_path / "src" / "users.controller.ts").write_text(
            "export class UsersController {}\n",
            encoding="utf-8",
        )
        (workspace_path / "src" / "users.service.ts").write_text(
            "export class UsersService {}\n",
            encoding="utf-8",
        )
        graphify_file = workspace_path / ".code-scout" / "graphify.json"
        graphify_file.parent.mkdir()
        graphify_file.write_text(
            (FIXTURES / "graphify" / "representative.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        _write_config(
            workspace_path,
            """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
  tree_sitter:
    enabled: false
  symbol_extraction:
    enabled: false
  lsp:
    enabled: false
  package_metadata:
    enabled: false
  openapi:
    enabled: false
  adr:
    enabled: false
  asyncapi:
    enabled: false
  infra:
    enabled: false
  docs:
    enabled: false
""",
        )

    scan_workspace(str(full_workspace), extractor="graphify")
    replace_changed_file_active_facts(
        str(incremental_workspace),
        changed_file_paths=("src/users.controller.ts", "src/users.service.ts"),
        selected_extractors=("graphify",),
    )

    assert _current_graph_state(incremental_workspace) == _current_graph_state(
        full_workspace
    )


def test_replace_changed_file_active_facts_deletes_explicit_deleted_file(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )
    unrelated_fact = ActiveFact(
        id="fact:entity:docs:billing-service",
        extractor="docs",
        kind="service",
        source_file="docs/architecture.md",
        identity={"entity_type": "service", "entity_id": "service:billing"},
        properties={"name": "Billing", "repository_id": "workspace"},
        evidence=(
            FactEvidence(
                file_path="docs/architecture.md",
                extractor_name="docs",
                confidence=0.82,
            ),
        ),
        confidence=0.82,
    )
    with open_storage(tmp_path) as connection:
        upsert_active_facts(connection, (unrelated_fact,))
        connection.commit()
    spec_file.unlink()

    result = replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=(),
        deleted_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    deleted_source_fact_count = connection.execute(
        "SELECT count(*) FROM facts WHERE source_file = ?",
        ("openapi.yaml",),
    ).fetchone()[0]
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    file_count = connection.execute(
        "SELECT count(*) FROM files WHERE path = ?",
        ("openapi.yaml",),
    ).fetchone()[0]
    service_row = connection.execute(
        "SELECT id, name FROM services WHERE id = ?",
        ("service:billing",),
    ).fetchone()
    change_events = list_change_events(connection)
    connection.close()

    assert result.warnings == ()
    assert result.facts_removed > 0
    assert result.facts_added >= 0
    assert result.nodes_updated > 0
    assert len(change_events) == 1
    assert change_events[0].type == "incremental_delete"
    assert change_events[0].file == "openapi.yaml"
    assert change_events[0].facts_added == result.facts_added
    assert change_events[0].facts_removed == result.facts_removed
    assert change_events[0].nodes_updated == result.nodes_updated
    assert change_events[0].outputs_rebuilt is False
    assert deleted_source_fact_count == 0
    assert contract_count == 0
    assert file_count == 0
    assert dict(service_row) == {"id": "service:billing", "name": "Billing"}


def test_replace_changed_file_active_facts_deletes_raw_evidence_and_embeddings(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )
    with open_storage(tmp_path) as connection:
        deleted_source_unit_ids = {
            unit.id
            for unit in list_raw_evidence_units(connection)
            if unit.source_file == "openapi.yaml"
        }
        assert deleted_source_unit_ids
        assert any(
            embedding.evidence_unit_id in deleted_source_unit_ids
            for embedding in list_evidence_embeddings(connection)
        )
    spec_file.unlink()

    replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=(),
        deleted_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    with open_storage(tmp_path) as connection:
        remaining_units = list_raw_evidence_units(connection)
        remaining_embeddings = list_evidence_embeddings(connection)

    assert all(unit.source_file != "openapi.yaml" for unit in remaining_units)
    assert deleted_source_unit_ids.isdisjoint(
        embedding.evidence_unit_id for embedding in remaining_embeddings
    )


def test_replace_changed_file_active_facts_warns_for_temporarily_unreadable_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import code_scout.pipeline.incremental as incremental_module

    create_workspace_config(tmp_path)
    source_file = tmp_path / "openapi.yaml"
    source_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    def unreadable_discovery(_workspace: object):
        raise PermissionError(13, "Permission denied", str(source_file))

    monkeypatch.setattr(
        incremental_module,
        "discover_workspace_files",
        unreadable_discovery,
    )

    result = replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    source_fact_count = connection.execute(
        "SELECT count(*) FROM facts WHERE source_file = ?",
        ("openapi.yaml",),
    ).fetchone()[0]
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    connection.close()

    assert result.facts_added == 0
    assert result.facts_removed == 0
    assert result.nodes_updated == 0
    assert result.warnings[0].path == "openapi.yaml"
    assert result.warnings[0].message == (
        "Changed file could not be read for incremental scanning."
    )
    assert source_fact_count > 0
    assert contract_count == 1


def test_replace_changed_file_active_facts_warns_for_ambiguous_missing_changed_file(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    source_file = tmp_path / "openapi.yaml"
    source_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        extractor="openapi",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )
    source_file.unlink()

    result = replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("openapi.yaml",),
        selected_extractors=("openapi",),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    source_fact_count = connection.execute(
        "SELECT count(*) FROM facts WHERE source_file = ?",
        ("openapi.yaml",),
    ).fetchone()[0]
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    connection.close()

    assert result.facts_added == 0
    assert result.facts_removed == 0
    assert result.nodes_updated == 0
    assert result.warnings[0].path == "openapi.yaml"
    assert result.warnings[0].message == (
        "Changed file was not available for incremental scanning."
    )
    assert source_fact_count > 0
    assert contract_count == 1


def test_replace_changed_file_active_facts_characterizes_mixed_batch(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
""",
        encoding="utf-8",
    )
    package_file = tmp_path / "package.json"
    package_file.write_text(
        '{"name":"orders","dependencies":{"express":"latest"}}\n',
        encoding="utf-8",
    )
    scan_workspace(
        str(tmp_path),
        profile="docs",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )
    scan_workspace(
        str(tmp_path),
        extractor="package_metadata",
        created_at=datetime(2026, 6, 21, 10, 1, tzinfo=UTC),
    )

    spec_file.unlink()
    package_file.write_text(
        '{"name":"orders","dependencies":{"next":"latest"}}\n',
        encoding="utf-8",
    )

    result = replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("package.json", "missing-openapi.yaml"),
        deleted_file_paths=("openapi.yaml",),
        selected_extractors=("package_metadata", "openapi"),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    source_fact_counts = {
        row["source_file"]: row["count"]
        for row in connection.execute(
            """
            SELECT source_file, count(*) AS count
            FROM facts
            WHERE source_file IN (
                'package.json',
                'openapi.yaml',
                'missing-openapi.yaml'
            )
            GROUP BY source_file
            """,
        ).fetchall()
    }
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    repository_row = connection.execute(
        "SELECT framework_hints_json FROM repositories WHERE id = 'workspace'",
    ).fetchone()
    change_events = list_change_events(connection)
    connection.close()

    assert result.facts_added > 0
    assert result.facts_removed > 0
    assert result.nodes_updated > 0
    assert result.change_event_id == change_events[0].id
    assert [warning.path for warning in result.warnings] == ["missing-openapi.yaml"]
    assert result.warnings[0].message == (
        "Changed file was not available for incremental scanning."
    )
    assert source_fact_counts["package.json"] > 0
    assert "openapi.yaml" not in source_fact_counts
    assert "missing-openapi.yaml" not in source_fact_counts
    assert contract_count == 0
    assert "Next.js" in json.loads(repository_row["framework_hints_json"])
    assert len(change_events) == 1
    assert change_events[0].type == "incremental_mixed"
    assert change_events[0].file == "package.json,missing-openapi.yaml,openapi.yaml"
    assert change_events[0].extractors_run == ("package_metadata", "openapi")
    assert change_events[0].facts_added == result.facts_added
    assert change_events[0].facts_removed == result.facts_removed
    assert change_events[0].nodes_updated == result.nodes_updated
    assert change_events[0].outputs_rebuilt is False
    assert change_events[0].metadata == {}


def test_scan_graphify_extractor_requires_configured_source(tmp_path) -> None:
    create_workspace_config(tmp_path)

    with pytest.raises(ExtractionError) as error:
        scan_workspace(
            str(tmp_path),
            extractor="graphify",
            created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        )

    assert "Graphify source is unavailable" in error.value.message
    assert "extractors.graphify.command" in (error.value.hint or "")


def test_scan_summary_prints_zero_counts_before_extractors_are_wired(tmp_path) -> None:
    create_workspace_config(tmp_path)

    summary = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    ).summary()

    assert "Scan complete." in summary
    assert "Workspace: Workspace (workspace)" in summary
    assert "Extractor: adr" in summary
    assert "Files analyzed: 0" in summary
    assert "Package files analyzed: 0" in summary
    assert "Repositories detected: 0" in summary
    assert "Symbols: 0" in summary
    assert "Services detected: 0" in summary
    assert "Contracts detected: 0" in summary
    assert "ADRs detected: 0" in summary
    assert "Concepts detected: 0" in summary
    assert "Edges created: 0" in summary


def test_scan_default_skips_unconfigured_graphify_without_warning(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "export function App() {}\n",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    assert result.warnings == ()
    assert "Warnings: 0" in result.summary()


def test_scan_persists_discovered_file_metadata(tmp_path) -> None:
    create_workspace_config(tmp_path)
    app_file = tmp_path / "src" / "app.ts"
    app_file.parent.mkdir(parents=True)
    app_file.write_text("export const app = true;\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    file_rows = connection.execute(
        """
        SELECT id, repository_id, path, kind, extension, language, content_hash,
               modified_at
        FROM files
        """
    ).fetchall()
    connection.close()

    assert result.counts.files == 1
    assert [dict(row) for row in file_rows] == [
        {
            "id": "file:workspace:src/app.ts",
            "repository_id": "workspace",
            "path": "src/app.ts",
            "kind": "source",
            "extension": ".ts",
            "language": "TypeScript",
            "content_hash": sha256(
                b"export const app = true;\n",
            ).hexdigest(),
            "modified_at": file_rows[0]["modified_at"],
        }
    ]
    assert file_rows[0]["modified_at"].endswith("Z")


def test_infer_language_hints_returns_single_dominant_language(tmp_path) -> None:
    discovered_files = tuple(
        _discovered_file(tmp_path, f"src/module{index}.py", language="Python")
        for index in range(10)
    )

    assert _infer_language_hints(discovered_files, "workspace") == ("Python",)


def test_infer_language_hints_returns_multiple_qualifying_languages(tmp_path) -> None:
    discovered_files = (
        *(
            _discovered_file(tmp_path, f"src/module{index}.py", language="Python")
            for index in range(10)
        ),
        *(
            _discovered_file(tmp_path, f"web/page{index}.ts", language="TypeScript")
            for index in range(6)
        ),
        *(
            _discovered_file(tmp_path, f"scripts/tool{index}.go", language="Go")
            for index in range(3)
        ),
        _discovered_file(tmp_path, "README.md", language="Markdown"),
        _discovered_file(
            tmp_path,
            "other/app.rb",
            repository_id="other",
            language="Ruby",
        ),
        _discovered_file(tmp_path, "assets/logo", language=None),
    )

    assert _infer_language_hints(discovered_files, "workspace") == (
        "Python",
        "TypeScript",
        "Go",
        "Markdown",
    )


def test_infer_language_hints_excludes_languages_below_threshold(tmp_path) -> None:
    discovered_files = (
        *(
            _discovered_file(tmp_path, f"src/module{index}.py", language="Python")
            for index in range(96)
        ),
        *(
            _discovered_file(tmp_path, f"web/page{index}.ts", language="TypeScript")
            for index in range(4)
        ),
    )

    assert _infer_language_hints(discovered_files, "workspace") == ("Python",)


def test_scan_merges_inferred_language_hints_with_package_hints(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "package.json").write_text(
        '{"devDependencies":{"typescript":"^5.0.0"}}\n',
        encoding="utf-8",
    )
    python_file = tmp_path / "src" / "app.py"
    python_file.parent.mkdir()
    python_file.write_text("print('hello')\n", encoding="utf-8")

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    language_hints_json = connection.execute(
        "SELECT language_hints_json FROM repositories WHERE id = ?",
        ("workspace",),
    ).fetchone()[0]
    connection.close()

    assert json.loads(language_hints_json) == ["JSON", "Python", "TypeScript"]


def test_scan_persists_symbol_extraction_registry_symbols_and_edges(
    tmp_path,
    monkeypatch,
) -> None:
    registry_calls: list[dict[str, object]] = []

    class FakeRegistry:
        def __init__(
            self,
            *,
            tree_sitter_enabled: bool,
            heuristic_enabled: bool,
            dotnet_enabled: bool,
            byond_enabled: bool,
            tree_sitter_extractor: object,
            heuristic_extractor: object,
            dotnet_extractor: object,
            byond_extractor: object,
        ) -> None:
            registry_calls.append(
                {
                    "tree_sitter_enabled": tree_sitter_enabled,
                    "heuristic_enabled": heuristic_enabled,
                    "dotnet_enabled": dotnet_enabled,
                    "byond_enabled": byond_enabled,
                    "tree_sitter_extractor": tree_sitter_extractor,
                    "heuristic_extractor": heuristic_extractor,
                    "dotnet_extractor": dotnet_extractor,
                    "byond_extractor": byond_extractor,
                },
            )

        def extract(self, discovered_files: tuple[object, ...]) -> SymbolExtraction:
            assert [file.path for file in discovered_files] == ["src/tool.ps1"]
            symbol = Symbol(
                id="symbol:workspace:src/tool.ps1:powershell:invoke-tool",
                file_id="file:workspace:src/tool.ps1",
                name="Invoke-Tool",
                kind="function",
                qualified_name="Invoke-Tool",
                start_line=1,
                end_line=1,
            )
            return SymbolExtraction(
                symbols=(symbol,),
                edges=(
                    Edge(
                        source_type="file",
                        source_id="file:workspace:src/tool.ps1",
                        target_type="symbol",
                        target_id=symbol.id,
                        relation_type="declares_symbol",
                        confidence=0.6,
                        evidence=(
                            Evidence(
                                file_path="src/tool.ps1",
                                extractor_name="fake_registry",
                                confidence=0.6,
                                start_line=1,
                                end_line=1,
                            ),
                        ),
                    ),
                ),
            )

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    create_workspace_config(tmp_path)
    script_file = tmp_path / "src" / "tool.ps1"
    script_file.parent.mkdir()
    script_file.write_text("function Invoke-Tool {}\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    symbol_rows = connection.execute(
        "SELECT id, name, kind, qualified_name FROM symbols"
    ).fetchall()
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               confidence, evidence_json
        FROM edges
        """
    ).fetchall()
    connection.close()

    assert result.counts.symbols == 1
    assert result.counts.edges == 1
    assert registry_calls[0]["tree_sitter_enabled"] is True
    assert registry_calls[0]["heuristic_enabled"] is True
    assert registry_calls[0]["dotnet_enabled"] is True
    assert registry_calls[0]["byond_enabled"] is True
    assert [dict(row) for row in symbol_rows] == [
        {
            "id": "symbol:workspace:src/tool.ps1:powershell:invoke-tool",
            "name": "Invoke-Tool",
            "kind": "function",
            "qualified_name": "Invoke-Tool",
        }
    ]
    assert [dict(row) | {"evidence_json": "<json>"} for row in edge_rows] == [
        {
            "source_type": "file",
            "source_id": "file:workspace:src/tool.ps1",
            "target_type": "symbol",
            "target_id": "symbol:workspace:src/tool.ps1:powershell:invoke-tool",
            "relation_type": "declares_symbol",
            "confidence": 0.6,
            "evidence_json": "<json>",
        }
    ]
    assert json.loads(edge_rows[0]["evidence_json"])[0]["extractor_name"] == (
        "fake_registry"
    )


def test_scan_disables_registry_tree_sitter_when_tree_sitter_not_selected(
    tmp_path,
    monkeypatch,
) -> None:
    registry_tree_sitter_flags: list[bool] = []

    class FakeRegistry:
        def __init__(
            self,
            *,
            tree_sitter_enabled: bool,
            heuristic_enabled: bool,
            dotnet_enabled: bool,
            byond_enabled: bool,
            tree_sitter_extractor: object,
            heuristic_extractor: object,
            dotnet_extractor: object,
            byond_extractor: object,
        ) -> None:
            registry_tree_sitter_flags.append(tree_sitter_enabled)

        def extract(self, discovered_files: tuple[object, ...]) -> SymbolExtraction:
            return SymbolExtraction()

    monkeypatch.setattr("code_scout.harvest.adapters.ExtractorRegistry", FakeRegistry)
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: false
  lsp:
    enabled: false
  graphify:
    enabled: false
  adr:
    enabled: false
  openapi:
    enabled: false
  package_metadata:
    enabled: false
  symbol_extraction:
    enabled: true
""",
        encoding="utf-8",
    )
    script_file = tmp_path / "src" / "tool.ps1"
    script_file.parent.mkdir()
    script_file.write_text("function Invoke-Tool {}\n", encoding="utf-8")

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    assert registry_tree_sitter_flags == [False]


def test_scan_persists_adr_files_as_decision_candidates_with_evidence(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "README.md").write_text("# Notes\n", encoding="utf-8")
    adr_file = tmp_path / "docs" / "adr" / "0001-use-sqlite.md"
    adr_file.parent.mkdir(parents=True)
    adr_file.write_text(
        """# 1. Use SQLite

Date: 2026-06-20

## Status

Accepted

## Context

code-scout needs a local durable graph store.

## Decision

Use SQLite through the stdlib sqlite3 module.

## Consequences

Scans can persist graph facts without network access.
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    file_rows = connection.execute(
        "SELECT id, path, kind FROM files ORDER BY path"
    ).fetchall()
    decision_rows = connection.execute(
        """
        SELECT id, title, status, adr_id, path, decided_at, context, decision,
               consequences, evidence_json
        FROM decisions
        """
    ).fetchall()
    concept_rows = connection.execute(
        "SELECT id, name, description FROM concepts"
    ).fetchall()
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               confidence, evidence_json
        FROM edges
        ORDER BY relation_type, source_id
        """
    ).fetchall()
    active_facts = list_active_facts(connection)
    connection.close()

    assert result.counts.files == 2
    assert result.counts.concepts == 1
    assert result.counts.decisions == 1
    assert result.counts.edges == 3
    assert [dict(row) for row in file_rows] == [
        {
            "id": "file:workspace:README.md",
            "path": "README.md",
            "kind": "documentation",
        },
        {
            "id": "file:workspace:docs/adr/0001-use-sqlite.md",
            "path": "docs/adr/0001-use-sqlite.md",
            "kind": "decision",
        },
    ]
    assert [dict(row) for row in decision_rows] == [
        {
            "id": "decision:workspace:docs/adr/0001-use-sqlite.md",
            "title": "Use SQLite",
            "status": "accepted",
            "adr_id": "1",
            "path": "docs/adr/0001-use-sqlite.md",
            "decided_at": "2026-06-20",
            "context": "code-scout needs a local durable graph store.",
            "decision": "Use SQLite through the stdlib sqlite3 module.",
            "consequences": "Scans can persist graph facts without network access.",
            "evidence_json": decision_rows[0]["evidence_json"],
        }
    ]
    assert _without_revision_metadata(
        json.loads(decision_rows[0]["evidence_json"])
    ) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"field": "title", "repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 1,
            "end_line": 1,
            "metadata": {"field": "title", "repository_id": "workspace"},
        },
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"field": "status", "repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 7,
            "end_line": 7,
            "metadata": {"field": "status", "repository_id": "workspace"},
        },
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"field": "decided_at", "repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 3,
            "end_line": 3,
            "metadata": {"field": "decided_at", "repository_id": "workspace"},
        },
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"field": "context", "repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 11,
            "end_line": 11,
            "metadata": {"field": "context", "repository_id": "workspace"},
        },
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"field": "decision", "repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 15,
            "end_line": 15,
            "metadata": {"field": "decision", "repository_id": "workspace"},
        },
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"field": "consequences", "repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 19,
            "end_line": 19,
            "metadata": {"field": "consequences", "repository_id": "workspace"},
        },
    ]
    assert [dict(row) for row in concept_rows] == [
        {
            "id": "concept:workspace:use-sqlite",
            "name": "Use SQLite",
            "description": None,
        }
    ]
    assert dict(edge_rows[0]) | {"evidence_json": "<json>"} == {
        "source_type": "decision",
        "source_id": "decision:workspace:docs/adr/0001-use-sqlite.md",
        "target_type": "concept",
        "target_id": "concept:workspace:use-sqlite",
        "relation_type": "affects",
        "confidence": 0.85,
        "evidence_json": "<json>",
    }
    assert _without_revision_metadata(json.loads(edge_rows[0]["evidence_json"])) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {
                            "source": "title",
                            "concept_slug": "use-sqlite",
                            "repository_id": "workspace",
                        },
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 0.85,
            "start_line": 1,
            "end_line": 1,
            "metadata": {
                "source": "title",
                "concept_slug": "use-sqlite",
                "repository_id": "workspace",
            },
        }
    ]
    assert dict(edge_rows[1]) | {"evidence_json": "<json>"} == {
        "source_type": "concept",
        "source_id": "concept:workspace:use-sqlite",
        "target_type": "decision",
        "target_id": "decision:workspace:docs/adr/0001-use-sqlite.md",
        "relation_type": "decided_by",
        "confidence": 0.85,
        "evidence_json": "<json>",
    }
    assert _without_revision_metadata(json.loads(edge_rows[1]["evidence_json"])) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {
                            "source": "title",
                            "concept_slug": "use-sqlite",
                            "repository_id": "workspace",
                        },
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 0.85,
            "start_line": 1,
            "end_line": 1,
            "metadata": {
                "source": "title",
                "concept_slug": "use-sqlite",
                "repository_id": "workspace",
            },
        }
    ]
    assert json.loads(edge_rows[1]["evidence_json"])[0]["confidence"] == 0.85
    assert dict(edge_rows[2]) | {"evidence_json": "<json>"} == {
        "source_type": "file",
        "source_id": "file:workspace:docs/adr/0001-use-sqlite.md",
        "target_type": "decision",
        "target_id": "decision:workspace:docs/adr/0001-use-sqlite.md",
        "relation_type": "documents_decision",
        "confidence": 1.0,
        "evidence_json": "<json>",
    }
    assert _without_revision_metadata(json.loads(edge_rows[2]["evidence_json"])) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-use-sqlite.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 1,
            "end_line": 19,
            "metadata": {"repository_id": "workspace"},
        }
    ]
    assert result.counts == result.counts.__class__(
        files=2,
        package_files=0,
        repositories_detected=0,
        concepts=1,
        decisions=1,
        edges=3,
    )
    assert {
        ("adr_markdown", "decision", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "affects", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "decided_by", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "documents_decision", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "concept", "docs/adr/0001-use-sqlite.md"),
        ("file_discovery", "file", "README.md"),
        ("file_discovery", "file", "docs/adr/0001-use-sqlite.md"),
        ("workspace_config", "repository", "."),
    } <= {(fact.extractor, fact.kind, fact.source_file) for fact in active_facts}
    assert {
        (fact.extractor, fact.kind, fact.source_file)
        for fact in active_facts
        if fact.extractor == "adr_markdown"
    } == {
        ("adr_markdown", "decision", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "concept", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "affects", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "decided_by", "docs/adr/0001-use-sqlite.md"),
        ("adr_markdown", "documents_decision", "docs/adr/0001-use-sqlite.md"),
    }
    decision_fact = next(fact for fact in active_facts if fact.kind == "decision")
    assert decision_fact.identity == {
        "entity_type": "decision",
        "entity_id": "decision:workspace:docs/adr/0001-use-sqlite.md",
    }
    assert decision_fact.properties["title"] == "Use SQLite"
    assert len(decision_fact.evidence) == 6


def test_scan_persists_claude_md_through_adr_decision_edges(tmp_path) -> None:
    create_workspace_config(tmp_path)
    claude_file = tmp_path / "CLAUDE.md"
    claude_file.write_text(
        """# Use active architecture guidance

Status: Accepted
Decision: Export repository guidance as active decisions.
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        extractor="adr",
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    file_rows = connection.execute("SELECT path, kind FROM files").fetchall()
    decision_rows = connection.execute(
        "SELECT id, title, status, path FROM decisions"
    ).fetchall()
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               confidence, evidence_json
        FROM edges
        WHERE relation_type = 'documents_decision'
        """
    ).fetchall()
    connection.close()

    assert result.counts.decisions == 1
    assert [dict(row) for row in file_rows] == [
        {"path": "CLAUDE.md", "kind": "decision"}
    ]
    assert [dict(row) for row in decision_rows] == [
        {
            "id": "decision:workspace:CLAUDE.md",
            "title": "Use active architecture guidance",
            "status": "accepted",
            "path": "CLAUDE.md",
        }
    ]
    assert len(edge_rows) == 1
    assert dict(edge_rows[0]) | {"evidence_json": "<json>"} == {
        "source_type": "file",
        "source_id": "file:workspace:CLAUDE.md",
        "target_type": "decision",
        "target_id": "decision:workspace:CLAUDE.md",
        "relation_type": "documents_decision",
        "confidence": 1.0,
        "evidence_json": "<json>",
    }
    assert _without_revision_metadata(json.loads(edge_rows[0]["evidence_json"])) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {"repository_id": "workspace"},
                        extractor_provenance=("adr_markdown",),
                    ).to_dict()
                )
            ),
            "file_path": "CLAUDE.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 1,
            "end_line": 4,
            "metadata": {"repository_id": "workspace"},
        }
    ]


def test_scan_persists_docs_markdown_concepts_and_links(tmp_path) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: false
  lsp:
    enabled: false
  graphify:
    enabled: false
  adr:
    enabled: false
  docs:
    enabled: true
  openapi:
    enabled: false
  package_metadata:
    enabled: false
  symbol_extraction:
    enabled: false
""",
        encoding="utf-8",
    )
    source_file = tmp_path / "src" / "payments.ts"
    source_file.parent.mkdir()
    source_file.write_text("export function processPayments() {}\n", encoding="utf-8")
    docs_file = tmp_path / "docs" / "payments.md"
    docs_file.parent.mkdir()
    docs_file.write_text(
        """# Payments

Payments are implemented in [payments.ts](../src/payments.ts).
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        extractor="docs",
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    concept_rows = connection.execute("SELECT id, name FROM concepts").fetchall()
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               confidence, evidence_json
        FROM edges
        WHERE relation_type IN ('documents', 'documents_concept')
        ORDER BY relation_type, source_type
        """
    ).fetchall()
    active_facts = list_active_facts(connection)
    connection.close()

    assert result.counts.concepts == 1
    assert result.counts.edges == 2
    assert [(row["id"], row["name"]) for row in concept_rows] == [
        ("concept:workspace:payments", "Payments"),
    ]
    concept_fact = next(
        fact
        for fact in active_facts
        if fact.extractor == "docs" and fact.kind == "concept"
    )
    assert concept_fact.evidence[0].confidence == 0.85
    assert [dict(row) | {"evidence_json": "<json>"} for row in edge_rows] == [
        {
            "source_type": "concept",
            "source_id": "concept:workspace:payments",
            "target_type": "file",
            "target_id": "file:workspace:src/payments.ts",
            "relation_type": "documents",
            "confidence": 0.85,
            "evidence_json": "<json>",
        },
        {
            "source_type": "file",
            "source_id": "file:workspace:docs/payments.md",
            "target_type": "concept",
            "target_id": "concept:workspace:payments",
            "relation_type": "documents_concept",
            "confidence": 0.85,
            "evidence_json": "<json>",
        },
    ]
    assert {
        (fact.extractor, fact.kind, fact.source_file)
        for fact in active_facts
        if fact.extractor == "docs"
    } == {
        ("docs", "concept", "docs/payments.md"),
        ("docs", "documents", "docs/payments.md"),
        ("docs", "documents_concept", "docs/payments.md"),
    }


def test_scan_discovers_configured_adr_paths(tmp_path) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
paths:
  adr:
    - ./records/decisions
""",
        encoding="utf-8",
    )
    adr_file = tmp_path / "records" / "decisions" / "0002-use-adr-path.md"
    adr_file.parent.mkdir(parents=True)
    adr_file.write_text("# Use configured ADR path\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    decision_paths = [
        row[0] for row in connection.execute("SELECT path FROM decisions").fetchall()
    ]
    connection.close()

    assert result.counts.decisions == 1
    assert decision_paths == ["records/decisions/0002-use-adr-path.md"]


def test_scan_updates_changed_file_hash_and_modified_timestamp(tmp_path) -> None:
    create_workspace_config(tmp_path)
    app_file = tmp_path / "src" / "app.ts"
    first_modified_at = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    second_modified_at = datetime(2026, 6, 20, 10, 1, tzinfo=UTC)
    app_file.parent.mkdir(parents=True)
    app_file.write_text("first\n", encoding="utf-8")
    os.utime(app_file, (first_modified_at.timestamp(), first_modified_at.timestamp()))

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )
    app_file.write_text("second\n", encoding="utf-8")
    os.utime(
        app_file,
        (second_modified_at.timestamp(), second_modified_at.timestamp()),
    )
    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 1, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    file_rows = connection.execute(
        "SELECT id, content_hash, modified_at FROM files"
    ).fetchall()
    connection.close()

    assert [dict(row) for row in file_rows] == [
        {
            "id": "file:workspace:src/app.ts",
            "content_hash": sha256(b"second\n").hexdigest(),
            "modified_at": "2026-06-20T10:01:00Z",
        }
    ]
    assert len(file_rows) == 1


def test_scan_persists_package_json_repository_fact_with_evidence(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "package.json").write_text(
        (
            '{"name":"@acme/web","private":true,"scripts":{"dev":"vite"},'
            '"dependencies":{"react":"^19.0.0"},'
            '"devDependencies":{"vitest":"^3.0.0"}}\n'
        ),
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               confidence, evidence_json
        FROM edges
        """
    ).fetchall()
    active_facts = list_active_facts(connection)
    connection.close()

    assert result.counts.package_files == 1
    assert result.counts.repositories_detected == 1
    assert result.counts.edges == 1
    assert len(edge_rows) == 1
    assert dict(edge_rows[0]) | {"evidence_json": "<json>"} == {
        "source_type": "repository",
        "source_id": "workspace",
        "target_type": "file",
        "target_id": "file:workspace:package.json",
        "relation_type": "declares_package_metadata",
        "confidence": 1.0,
        "evidence_json": "<json>",
    }
    assert _without_revision_metadata(json.loads(edge_rows[0]["evidence_json"])) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {
                            "name": "@acme/web",
                            "scripts": {"dev": "vite"},
                            "dependencies": {"react": "^19.0.0"},
                            "dev_dependencies": {"vitest": "^3.0.0"},
                            "workspaces": [],
                            "private": True,
                            "repository_id": "workspace",
                            "language_hints": [],
                            "framework_hints": [],
                        },
                        extractor_provenance=("package_json",),
                    ).to_dict()
                )
            ),
            "file_path": "package.json",
            "extractor_name": "package_json",
            "confidence": 1.0,
            "start_line": 1,
            "end_line": 1,
            "metadata": {
                "name": "@acme/web",
                "scripts": {"dev": "vite"},
                "dependencies": {"react": "^19.0.0"},
                "dev_dependencies": {"vitest": "^3.0.0"},
                "workspaces": [],
                "private": True,
                "repository_id": "workspace",
                "language_hints": [],
                "framework_hints": [],
            },
        }
    ]
    package_fact = next(fact for fact in active_facts if fact.kind == "package")
    assert package_fact.identity == {
        "entity_type": "package",
        "entity_id": "package:workspace:acme-web",
    }
    assert package_fact.properties["repository_id"] == "workspace"
    assert package_fact.properties["path"] == "package.json"
    assert package_fact.evidence[0].metadata["repository_id"] == "workspace"
    assert package_fact.evidence[0].metadata["name"] == "@acme/web"


def test_scan_persists_docker_compose_runtime_facts(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        """
services:
  api:
    image: ghcr.io/acme/api:latest
    depends_on:
      - db
      - queue
  db:
    image: postgres:16
  queue:
    image: rabbitmq:3
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    service_rows = connection.execute(
        "SELECT id, name, repository_id FROM services ORDER BY id",
    ).fetchall()
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type
        FROM edges
        WHERE relation_type = 'depends_on'
        ORDER BY target_id
        """,
    ).fetchall()
    active_facts = list_active_facts(connection)
    connection.close()

    assert result.counts.services == 3
    assert [dict(row) for row in service_rows] == [
        {"id": "service:workspace:api", "name": "api", "repository_id": "workspace"},
        {"id": "service:workspace:db", "name": "db", "repository_id": "workspace"},
        {
            "id": "service:workspace:queue",
            "name": "queue",
            "repository_id": "workspace",
        },
    ]
    assert [dict(row) for row in edge_rows] == [
        {
            "source_type": "service",
            "source_id": "service:workspace:api",
            "target_type": "database",
            "target_id": "runtime-resource:workspace:db",
            "relation_type": "depends_on",
        },
        {
            "source_type": "service",
            "source_id": "service:workspace:api",
            "target_type": "queue",
            "target_id": "runtime-resource:workspace:queue",
            "relation_type": "depends_on",
        },
    ]
    assert {
        (fact.kind, fact.identity["entity_id"])
        for fact in active_facts
        if fact.kind in {"database", "queue"}
    } == {
        ("database", "runtime-resource:workspace:db"),
        ("queue", "runtime-resource:workspace:queue"),
    }


def test_scan_persists_dockerfile_infra_facts(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "Dockerfile.prod").write_text(
        """
FROM node:22-alpine AS runtime
EXPOSE 3000
""",
        encoding="utf-8",
    )

    scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    active_facts = list_active_facts(connection)
    connection.close()

    assert {
        (fact.kind, fact.identity["entity_id"])
        for fact in active_facts
        if fact.kind in {"runtime_image", "deploy_target"}
    } == {
        ("runtime_image", "runtime-image:workspace:node-22-alpine"),
        ("deploy_target", "deploy-target:workspace:dockerfile-prod:3000-tcp"),
    }
    assert {
        (
            fact.kind,
            fact.identity["subject"]["type"],
            fact.identity["subject"]["id"],
            fact.identity["object"]["type"],
            fact.identity["object"]["id"],
        )
        for fact in active_facts
        if fact.kind in {"declares_runtime_image", "defines_deploy_target"}
    } == {
        (
            "declares_runtime_image",
            "file",
            "file:workspace:Dockerfile.prod",
            "runtime_image",
            "runtime-image:workspace:node-22-alpine",
        ),
        (
            "defines_deploy_target",
            "repository",
            "workspace",
            "deploy_target",
            "deploy-target:workspace:dockerfile-prod:3000-tcp",
        ),
    }


def test_scan_updates_repository_hints_from_package_facts(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "package.json").write_text(
        (
            '{"dependencies":{"next":"^15.0.0","@nestjs/core":"^11.0.0"},'
            '"devDependencies":{"typescript":"^5.8.0","turbo":"^2.0.0"}}\n'
        ),
        encoding="utf-8",
    )
    (tmp_path / "nx.json").write_text('{"targetDefaults":{}}\n', encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    repository_rows = connection.execute(
        """
        SELECT language_hints_json, framework_hints_json
        FROM repositories
        """
    ).fetchall()
    connection.close()

    assert result.counts.package_files == 2
    assert result.counts.repositories_detected == 1
    assert [json.loads(row["language_hints_json"]) for row in repository_rows] == [
        ["JSON", "TypeScript"]
    ]
    assert [json.loads(row["framework_hints_json"]) for row in repository_rows] == [
        ["NestJS", "Next.js", "nx", "turbo"]
    ]


def test_scan_persists_openapi_contracts_from_json_and_yaml(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "api.json").write_text(
        (
            '{"openapi":"3.1.0","info":{"title":"Payments API",'
            '"version":"2026-06-20"},"paths":{"/payments":{"post":{'
            '"operationId":"createPayment","tags":["Payments"],'
            '"requestBody":{"content":{"application/json":{"schema":'
            '{"$ref":"#/components/schemas/CreatePaymentRequest"}}}},'
            '"responses":{"201":{"description":"Created","content":'
            '{"application/json":{"schema":'
            '{"$ref":"#/components/schemas/PaymentResponse"}}}}}}}}}\n'
        ),
        encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
      tags:
        - Orders
      summary: List orders
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    contract_rows = connection.execute(
        """
        SELECT id, name, kind, path, version, method, producer_hint,
               operation_id, request_dtos_json, response_dtos_json, evidence_json
        FROM contracts
        ORDER BY id
        """
    ).fetchall()
    concept_rows = connection.execute(
        "SELECT id, name, description FROM concepts ORDER BY id"
    ).fetchall()
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               confidence, evidence_json
        FROM edges
        ORDER BY relation_type, source_id, target_id
        """
    ).fetchall()
    connection.close()

    assert result.counts.contracts == 2
    assert result.counts.concepts == 4
    assert result.counts.edges == 8
    assert result.warnings == ()
    assert [dict(row) | {"evidence_json": "<json>"} for row in contract_rows] == [
        {
            "id": "contract:workspace:http:get:orders",
            "name": "listOrders",
            "kind": "http",
            "path": "/orders",
            "version": "v1",
            "method": "GET",
            "producer_hint": "Orders API",
            "operation_id": "listOrders",
            "request_dtos_json": "[]",
            "response_dtos_json": "[]",
            "evidence_json": "<json>",
        },
        {
            "id": "contract:workspace:http:post:payments",
            "name": "createPayment",
            "kind": "http",
            "path": "/payments",
            "version": "2026-06-20",
            "method": "POST",
            "producer_hint": "Payments API",
            "operation_id": "createPayment",
            "request_dtos_json": '["CreatePaymentRequest"]',
            "response_dtos_json": '["PaymentResponse"]',
            "evidence_json": "<json>",
        },
    ]
    assert [dict(row) for row in concept_rows] == [
        {
            "id": "concept:workspace:createpayment",
            "name": "createPayment",
            "description": None,
        },
        {
            "id": "concept:workspace:listorders",
            "name": "listOrders",
            "description": None,
        },
        {
            "id": "concept:workspace:orders",
            "name": "Orders",
            "description": None,
        },
        {
            "id": "concept:workspace:payments",
            "name": "Payments",
            "description": None,
        },
    ]
    assert [dict(row) | {"evidence_json": "<json>"} for row in edge_rows] == [
        {
            "source_type": "file",
            "source_id": "file:workspace:api.json",
            "target_type": "contract",
            "target_id": "contract:workspace:http:post:payments",
            "relation_type": "declares_contract",
            "confidence": 1.0,
            "evidence_json": "<json>",
        },
        {
            "source_type": "file",
            "source_id": "file:workspace:openapi.yaml",
            "target_type": "contract",
            "target_id": "contract:workspace:http:get:orders",
            "relation_type": "declares_contract",
            "confidence": 1.0,
            "evidence_json": "<json>",
        },
        {
            "source_type": "concept",
            "source_id": "concept:workspace:createpayment",
            "target_type": "contract",
            "target_id": "contract:workspace:http:post:payments",
            "relation_type": "describes_contract",
            "confidence": 0.85,
            "evidence_json": "<json>",
        },
        {
            "source_type": "concept",
            "source_id": "concept:workspace:listorders",
            "target_type": "contract",
            "target_id": "contract:workspace:http:get:orders",
            "relation_type": "describes_contract",
            "confidence": 0.85,
            "evidence_json": "<json>",
        },
        {
            "source_type": "concept",
            "source_id": "concept:workspace:orders",
            "target_type": "contract",
            "target_id": "contract:workspace:http:get:orders",
            "relation_type": "describes_contract",
            "confidence": 0.85,
            "evidence_json": "<json>",
        },
        {
            "source_type": "concept",
            "source_id": "concept:workspace:payments",
            "target_type": "contract",
            "target_id": "contract:workspace:http:post:payments",
            "relation_type": "describes_contract",
            "confidence": 0.85,
            "evidence_json": "<json>",
        },
        {
            "source_type": "service",
            "source_id": "service:workspace:orders-api",
            "target_type": "contract",
            "target_id": "contract:workspace:http:get:orders",
            "relation_type": "produces",
            "confidence": 1.0,
            "evidence_json": "<json>",
        },
        {
            "source_type": "service",
            "source_id": "service:workspace:payments-api",
            "target_type": "contract",
            "target_id": "contract:workspace:http:post:payments",
            "relation_type": "produces",
            "confidence": 1.0,
            "evidence_json": "<json>",
        },
    ]
    assert _without_revision_metadata(json.loads(edge_rows[1]["evidence_json"])) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {
                            "repository_id": "workspace",
                            "title": "Orders API",
                            "version": "v1",
                            "spec_version": "3.0.3",
                            "path_count": 1,
                            "method": "GET",
                            "path": "/orders",
                            "operation_id": "listOrders",
                            "tags": ["Orders"],
                            "request_dtos": [],
                            "response_dtos": [],
                        },
                        extractor_provenance=("openapi",),
                    ).to_dict()
                )
            ),
            "file_path": "openapi.yaml",
            "extractor_name": "openapi",
            "confidence": 1.0,
            "start_line": 1,
            "end_line": 12,
            "metadata": {
                "repository_id": "workspace",
                "title": "Orders API",
                "version": "v1",
                "spec_version": "3.0.3",
                "path_count": 1,
                "method": "GET",
                "path": "/orders",
                "operation_id": "listOrders",
                "tags": ["Orders"],
                "request_dtos": [],
                "response_dtos": [],
            },
        }
    ]
    assert json.loads(contract_rows[0]["evidence_json"])[0]["confidence"] == 1.0


def test_scan_detects_configured_openapi_paths(tmp_path) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
paths:
  openapi:
    - contracts/public-api.yml
""",
        encoding="utf-8",
    )
    spec_file = tmp_path / "contracts" / "public-api.yml"
    spec_file.parent.mkdir(parents=True)
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Public API
  version: v2
paths:
  /public:
    get:
      operationId: getPublic
      summary: Public endpoint
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    contract_paths = [
        row[0] for row in connection.execute("SELECT path FROM contracts").fetchall()
    ]
    connection.close()

    assert result.counts.contracts == 1
    assert contract_paths == ["/public"]


def test_scan_detects_configured_asyncapi_paths(tmp_path) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
paths:
  asyncapi:
    - contracts/events.yml
""",
        encoding="utf-8",
    )
    spec_file = tmp_path / "contracts" / "events.yml"
    spec_file.parent.mkdir(parents=True)
    spec_file.write_text(
        """
asyncapi: 2.6.0
info:
  title: Events API
  version: v1
channels:
  orders.created:
    publish:
      operationId: publishOrderCreated
      message:
        payload:
          title: OrderCreated
          type: object
""",
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    contract_rows = connection.execute(
        "SELECT id, kind, path, method, operation_id FROM contracts"
    ).fetchall()
    fact_rows = connection.execute(
        "SELECT extractor, kind, source_file FROM facts ORDER BY kind"
    ).fetchall()
    connection.close()

    assert result.counts.contracts == 1
    assert [dict(row) for row in contract_rows] == [
        {
            "id": (
                "contract:workspace:contracts/events.yml:event:publish:orders-created"
            ),
            "kind": "event",
            "path": "orders.created",
            "method": "PUBLISH",
            "operation_id": "publishOrderCreated",
        },
    ]
    assert ("asyncapi", "schema", "contracts/events.yml") in [
        tuple(row) for row in fact_rows
    ]


def test_scan_rejects_non_openapi_json_and_yaml_files(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "settings.json").write_text('{"name":"app"}\n', encoding="utf-8")
    (tmp_path / "config.yaml").write_text("name: app\n", encoding="utf-8")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    edge_count = connection.execute("SELECT count(*) FROM edges").fetchone()[0]
    connection.close()

    assert result.counts.files == 2
    assert result.counts.contracts == 0
    assert result.warnings == ()
    assert contract_count == 0
    assert edge_count == 0


def test_scan_reports_invalid_openapi_warning_without_failing(tmp_path) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "openapi.json").write_text(
        '{"openapi":"3.0.0","info":{"title":"Broken API"},"paths":{}}\n',
        encoding="utf-8",
    )

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    contract_count = connection.execute("SELECT count(*) FROM contracts").fetchone()[0]
    connection.close()

    assert result.counts.files == 1
    assert result.counts.contracts == 0
    assert contract_count == 0
    assert result.warnings[0].path == "openapi.json"
    assert result.warnings[0].message == (
        "OpenAPI file is missing a non-empty `info.version` string."
    )


def _write_graphify_config(tmp_path, graphify_config: str) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        f"""
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: false
  lsp:
    enabled: false
{graphify_config}
  adr:
    enabled: false
  openapi:
    enabled: false
  package_metadata:
    enabled: false
""",
        encoding="utf-8",
    )


def _write_config(tmp_path: Path, text: str) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


def _current_graph_state(workspace_path: Path) -> dict[str, list[dict[str, object]]]:
    with sqlite3.connect(database_path(workspace_path)) as connection:
        connection.row_factory = sqlite3.Row
        return {
            "repositories": _rows(
                connection,
                """
                SELECT id, name, path, remote_url, default_branch,
                       language_hints_json, framework_hints_json,
                       module_hints_json
                FROM repositories
                ORDER BY id
                """,
            ),
            "files": _rows(
                connection,
                """
                SELECT id, repository_id, path, kind, extension, language,
                       content_hash
                FROM files
                ORDER BY id
                """,
            ),
            "symbols": _rows(
                connection,
                """
                SELECT id, file_id, name, kind, qualified_name, start_line, end_line
                FROM symbols
                ORDER BY id
                """,
            ),
            "services": _rows(
                connection,
                """
                SELECT id, repository_id, name, description, aliases_json,
                       evidence_json
                FROM services
                ORDER BY id
                """,
            ),
            "contracts": _rows(
                connection,
                """
                SELECT id, name, kind, path, version, method, producer_hint,
                       operation_id, request_dtos_json, response_dtos_json,
                       evidence_json
                FROM contracts
                ORDER BY id
                """,
            ),
            "concepts": _rows(
                connection,
                """
                SELECT id, name, description
                FROM concepts
                ORDER BY id
                """,
            ),
            "decisions": _rows(
                connection,
                """
                SELECT id, title, status, adr_id, path, decided_at, context,
                       decision, consequences, evidence_json
                FROM decisions
                ORDER BY id
                """,
            ),
            "edges": _rows(
                connection,
                """
                SELECT source_type, source_id, target_type, target_id,
                       relation_type, confidence, evidence_json
                FROM edges
                ORDER BY source_type, source_id, target_type, target_id,
                         relation_type
                """,
            ),
        }


def _rows(connection: sqlite3.Connection, query: str) -> list[dict[str, object]]:
    return [dict(row) for row in connection.execute(query).fetchall()]


def _write_graphify_enabled_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: true
  lsp:
    enabled: false
  graphify:
    enabled: true
    command:
      - graphify
      - --json
  adr:
    enabled: false
  openapi:
    enabled: false
  package_metadata:
    enabled: false
  symbol_extraction:
    enabled: true
""",
        encoding="utf-8",
    )


def _profile_service(extractor: str) -> Service:
    return Service(
        id=f"service:workspace:{extractor}",
        name=extractor,
        repository_id="workspace",
        evidence=(
            Evidence(
                file_path=f"{extractor}.fixture",
                extractor_name=extractor,
                confidence=0.9,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )


def _profile_active_fact(extractor: str) -> ActiveFact:
    return ActiveFact(
        id=f"fact:entity:{extractor}:profile-marker",
        extractor=extractor,
        kind="service",
        source_file=f"{extractor}.fixture",
        identity={
            "entity_type": "service",
            "entity_id": f"service:workspace:{extractor}",
        },
        properties={
            "name": extractor,
            "repository_id": "workspace",
        },
        evidence=(
            FactEvidence(
                file_path=f"{extractor}.fixture",
                extractor_name=extractor,
                confidence=0.9,
                metadata={"repository_id": "workspace"},
            ),
        ),
        confidence=0.9,
    )


def _tree_sitter_symbol(file_path: str, name: str) -> Symbol:
    return Symbol(
        id=f"symbol:workspace:{file_path}:tree_sitter:{name.lower()}",
        file_id=f"file:workspace:{file_path}",
        name=name,
        kind="function",
        qualified_name=name,
        start_line=1,
        end_line=1,
    )


def _declares_symbol_edge(file_path: str, symbol: Symbol) -> Edge:
    return Edge(
        source_type="file",
        source_id=f"file:workspace:{file_path}",
        target_type="symbol",
        target_id=symbol.id,
        relation_type="declares_symbol",
        confidence=1.0,
        evidence=(
            Evidence(
                file_path=file_path,
                extractor_name="tree_sitter",
                confidence=1.0,
                start_line=1,
                end_line=1,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )


def _tree_sitter_route_contract(file_path: str) -> Contract:
    return Contract(
        id="contract:workspace:tree_sitter:post:api-users",
        name="POST /api/users",
        kind="http",
        path="/api/users",
        method="POST",
        evidence=(
            Evidence(
                file_path=file_path,
                extractor_name="tree_sitter",
                confidence=1.0,
                start_line=1,
                end_line=1,
                metadata={
                    "repository_id": "workspace",
                    "framework": "nextjs",
                    "route": "/api/users",
                    "method": "POST",
                },
            ),
        ),
    )


def _tree_sitter_route_service(file_path: str) -> Service:
    return Service(
        id="service:workspace:web",
        name="web",
        repository_id="workspace",
        description="Next.js API route service",
        evidence=(
            Evidence(
                file_path=file_path,
                extractor_name="tree_sitter",
                confidence=1.0,
                start_line=1,
                end_line=1,
                metadata={
                    "repository_id": "workspace",
                    "framework": "nextjs",
                    "service_name": "web",
                },
            ),
        ),
    )


def _declares_contract_edge(file_path: str, contract: Contract) -> Edge:
    return Edge(
        source_type="file",
        source_id=f"file:workspace:{file_path}",
        target_type="contract",
        target_id=contract.id,
        relation_type="declares_contract",
        confidence=1.0,
        evidence=(
            Evidence(
                file_path=file_path,
                extractor_name="tree_sitter",
                confidence=1.0,
                start_line=1,
                end_line=1,
                metadata={"repository_id": "workspace", "route": contract.path},
            ),
        ),
    )


def _produces_contract_edge(
    file_path: str,
    service: Service,
    contract: Contract,
) -> Edge:
    return Edge(
        source_type="service",
        source_id=service.id,
        target_type="contract",
        target_id=contract.id,
        relation_type="produces",
        confidence=1.0,
        evidence=(
            Evidence(
                file_path=file_path,
                extractor_name="tree_sitter",
                confidence=1.0,
                start_line=1,
                end_line=1,
                metadata={
                    "repository_id": "workspace",
                    "framework": "nextjs",
                    "service_id": service.id,
                    "service_name": service.name,
                    "route": contract.path,
                    "method": contract.method,
                },
            ),
        ),
    )


def _symbol_active_fact(
    *,
    id: str,
    source_file: str,
    entity_id: str,
    name: str,
    start_line: int,
    end_line: int,
    confidence: float,
) -> ActiveFact:
    return ActiveFact(
        id=id,
        extractor="tree_sitter",
        kind="symbol",
        source_file=source_file,
        identity={"entity_type": "symbol", "entity_id": entity_id},
        properties={
            "file_id": f"file:workspace:{source_file}",
            "name": name,
            "kind": "function",
            "qualified_name": name,
            "start_line": start_line,
            "end_line": end_line,
        },
        evidence=(
            FactEvidence(
                file_path=source_file,
                extractor_name="tree_sitter",
                confidence=confidence,
                start_line=start_line,
                end_line=end_line,
                metadata={"repository_id": "workspace"},
            ),
        ),
        confidence=confidence,
    )


def _relation_active_fact(
    *,
    id: str,
    source_file: str,
    source_id: str,
    target_id: str,
    relation: str,
    confidence: float,
) -> ActiveFact:
    return ActiveFact(
        id=id,
        extractor="tree_sitter",
        kind=relation,
        source_file=source_file,
        identity={
            "subject": {"type": "symbol", "id": source_id},
            "relation": relation,
            "object": {"type": "symbol", "id": target_id},
        },
        properties={},
        evidence=(
            FactEvidence(
                file_path=source_file,
                extractor_name="tree_sitter",
                confidence=confidence,
                metadata={"repository_id": "workspace"},
            ),
        ),
        confidence=confidence,
    )


def _discovered_file(
    tmp_path: Path,
    relative_path: str,
    *,
    language: str | None,
    repository_id: str = "workspace",
) -> DiscoveredFile:
    path = tmp_path / relative_path
    return DiscoveredFile(
        repository_id=repository_id,
        path=relative_path,
        absolute_path=str(path),
        kind="source",
        extension=path.suffix,
        content_hash="hash",
        modified_at="2026-06-20T10:00:00Z",
        language=language,
    )
