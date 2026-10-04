from __future__ import annotations

import json
import sqlite3

import pytest

from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    Evidence,
    File,
    Repository,
    Service,
    Snapshot,
    Symbol,
)
from code_scout.storage import (
    DEFAULT_DATABASE_PATH,
    SCHEMA_VERSION,
    ActiveFactReplacementCounts,
    ChangeEvent,
    RawEvidenceUnit,
    RawEvidenceUnitReplacementCounts,
    SnapshotPayload,
    SourceRevision,
    canonical_snapshot_payload,
    create_snapshot,
    database_path,
    delete_active_facts_by_extractor_and_source_file,
    delete_raw_evidence_units_by_scan_scope,
    delete_stale_evidence_embeddings,
    deterministic_change_event_id,
    deterministic_raw_evidence_unit_id,
    deterministic_source_revision_id,
    evidence_text_hash,
    get_snapshot_payload,
    get_source_blob,
    get_source_revision,
    graph_summary_counts,
    initialize_schema,
    list_active_facts,
    list_active_facts_by_extractor_and_source_file,
    list_change_events,
    list_evidence_embeddings,
    list_raw_evidence_units,
    list_raw_evidence_units_by_scan_scope,
    list_snapshots,
    list_source_revisions,
    open_storage,
    record_change_event,
    refresh_evidence_embeddings,
    replace_active_facts_by_extractor_and_source_file,
    replace_raw_evidence_units_by_scan_scope,
    snapshot_payload_sha256,
    source_content_sha256,
    store_snapshot_payload,
    store_source_blob,
    store_source_revision,
    upsert_active_facts,
    upsert_concept,
    upsert_contract,
    upsert_decision,
    upsert_edge,
    upsert_file,
    upsert_raw_evidence_units,
    upsert_repository,
    upsert_service,
    upsert_symbol,
    upsert_workspace,
)

EXPECTED_TABLES = {
    "workspaces",
    "repositories",
    "files",
    "symbols",
    "services",
    "contracts",
    "concepts",
    "decisions",
    "edges",
    "facts",
    "raw_evidence_units",
    "evidence_embeddings",
    "change_events",
    "snapshots",
    "snapshot_payloads",
    "source_blobs",
    "source_revisions",
}


def _active_fact(
    id: str,
    *,
    extractor: str = "graphify",
    kind: str = "service",
    source_file: str = "apps/api/src/orders.ts",
) -> ActiveFact:
    return ActiveFact(
        id=id,
        extractor=extractor,
        kind=kind,
        source_file=source_file,
        identity={"entity_type": kind, "entity_id": id},
        properties={"name": id.rsplit(":", maxsplit=1)[-1]},
        evidence=(),
        confidence=0.9,
    )


def _raw_evidence_unit(
    id: str,
    *,
    owner_entity_type: str = "service",
    owner_entity_id: str = "service:orders",
    source_file: str = "apps/api/src/orders.ts",
    provenance: str = "graphify",
) -> RawEvidenceUnit:
    return RawEvidenceUnit(
        id=id,
        owner_entity_type=owner_entity_type,
        owner_entity_id=owner_entity_id,
        source_file=source_file,
        start_line=12,
        end_line=18,
        text="OrdersService handles order commands.",
        provenance=provenance,
        confidence=0.91,
        metadata={"symbol": "OrdersService"},
    )


class _CountingEmbeddingProvider:
    model_id = "test-provider"
    dimensions = 3

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_text(self, text: str) -> tuple[float, ...]:
        self.texts.append(text)
        return (float(len(text)), 1.0, 0.0)


def test_open_storage_reports_contention_and_closes_failed_connection(
    tmp_path, monkeypatch
) -> None:
    from code_scout.core.errors import StorageError
    from code_scout.storage import schema

    connection = open_storage(tmp_path)
    connection.close()
    blocker = sqlite3.connect(database_path(tmp_path))
    blocker.execute("BEGIN EXCLUSIVE")
    connect = sqlite3.connect
    attempted = []

    def short_timeout_connect(*args, **kwargs):
        kwargs["timeout"] = 0.01
        connection = connect(*args, **kwargs)
        attempted.append(connection)
        return connection

    monkeypatch.setattr(schema.sqlite3, "connect", short_timeout_connect)
    try:
        with pytest.raises(StorageError, match="busy") as failure:
            open_storage(tmp_path)
        assert "Wait" in failure.value.hint
        assert "scan" in failure.value.hint
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            attempted[0].execute("SELECT 1")
    finally:
        blocker.rollback()
        blocker.close()

    reopened = open_storage(tmp_path)
    assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()


def test_open_storage_creates_workspace_database_path(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        assert database_path(tmp_path) == tmp_path / DEFAULT_DATABASE_PATH
        assert database_path(tmp_path).is_file()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_initialize_schema_creates_required_tables(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        table_names = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert table_names == EXPECTED_TABLES
    assert user_version == SCHEMA_VERSION


def test_schema_includes_graph_relationship_columns(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        columns_by_table = {
            table_name: {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            for table_name in EXPECTED_TABLES
        }

    assert {
        "id",
        "workspace_id",
        "name",
        "path",
        "remote_url",
        "default_branch",
        "language_hints_json",
        "framework_hints_json",
        "module_hints_json",
    } <= columns_by_table["repositories"]
    assert {
        "id",
        "repository_id",
        "path",
        "kind",
        "extension",
        "language",
        "content_hash",
        "modified_at",
    } <= columns_by_table["files"]
    assert {"id", "file_id", "name", "kind", "qualified_name"} <= (
        columns_by_table["symbols"]
    )
    assert {
        "id",
        "repository_id",
        "name",
        "description",
        "aliases_json",
        "evidence_json",
    } <= columns_by_table["services"]
    assert {
        "adr_id",
        "context",
        "decision",
        "consequences",
        "evidence_json",
    } <= columns_by_table["decisions"]
    assert {
        "method",
        "producer_hint",
        "operation_id",
        "request_dtos_json",
        "response_dtos_json",
        "evidence_json",
    } <= columns_by_table["contracts"]
    assert {
        "source_type",
        "source_id",
        "target_type",
        "target_id",
        "relation_type",
        "confidence",
        "evidence_json",
    } <= columns_by_table["edges"]
    assert {
        "id",
        "extractor",
        "kind",
        "source_file",
        "identity_json",
        "properties_json",
        "evidence_json",
        "confidence",
        "conflict_metadata_json",
        "extractor_version",
        "created_at",
        "updated_at",
    } <= columns_by_table["facts"]
    assert {
        "id",
        "owner_entity_type",
        "owner_entity_id",
        "source_file",
        "start_line",
        "end_line",
        "text",
        "provenance",
        "confidence",
        "metadata_json",
        "created_at",
        "updated_at",
    } <= columns_by_table["raw_evidence_units"]
    assert {
        "evidence_unit_id",
        "model_id",
        "vector_json",
        "dimensions",
        "text_hash",
        "created_at",
        "updated_at",
    } <= columns_by_table["evidence_embeddings"]
    assert {
        "id",
        "type",
        "file",
        "extractors_run_json",
        "facts_added",
        "facts_removed",
        "nodes_updated",
        "outputs_rebuilt",
        "metadata_json",
        "timestamp",
    } <= columns_by_table["change_events"]
    assert {"id", "workspace_id", "created_at", "metadata_json", "payload_sha256"} <= (
        columns_by_table["snapshots"]
    )
    assert {"content_sha256", "format_version", "payload_json"} <= (
        columns_by_table["snapshot_payloads"]
    )


def test_schema_initialization_is_idempotent(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "idempotent.db")
    connection.row_factory = sqlite3.Row

    initialize_schema(connection)
    initialize_schema(connection)

    connection.execute(
        """
        INSERT INTO workspaces (id, name, root_path)
        VALUES ('workspace', 'Workspace', '.')
        """
    )
    connection.commit()

    initialize_schema(connection)

    rows = connection.execute("SELECT id, name FROM workspaces").fetchall()
    table_count = connection.execute(
        """
        SELECT count(*)
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'workspaces'
        """
    ).fetchone()[0]
    connection.close()

    assert [dict(row) for row in rows] == [{"id": "workspace", "name": "Workspace"}]
    assert table_count == 1


def test_schema_upgrade_preserves_legacy_storage_without_source_bytes(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "legacy.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE facts (
            id TEXT PRIMARY KEY,
            extractor TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_file TEXT NOT NULL,
            confidence REAL NOT NULL
        );
        CREATE TABLE raw_evidence_units (
            id TEXT PRIMARY KEY,
            owner_entity_type TEXT NOT NULL,
            owner_entity_id TEXT NOT NULL,
            source_file TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            text TEXT NOT NULL,
            provenance TEXT NOT NULL,
            confidence REAL NOT NULL
        );
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO facts VALUES ('fact:legacy', 'legacy', 'service', 'old.py', 0.5);
        INSERT INTO raw_evidence_units VALUES (
            'evidence:legacy', 'service', 'service:legacy', 'old.py', 1, 1,
            'legacy', 'legacy', 0.5
        );
        INSERT INTO snapshots VALUES (
            'snapshot:legacy', 'workspace', '2026-01-01', '{}'
        );
        """
    )

    initialize_schema(connection)

    assert connection.execute("SELECT id FROM facts").fetchone()[0] == "fact:legacy"
    assert (
        connection.execute("SELECT id FROM raw_evidence_units").fetchone()[0]
        == "evidence:legacy"
    )
    assert (
        connection.execute("SELECT id FROM snapshots").fetchone()[0]
        == "snapshot:legacy"
    )
    assert connection.execute("SELECT count(*) FROM source_blobs").fetchone()[0] == 0
    assert (
        connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] == 0
    )
    connection.close()


def test_source_revisions_deduplicate_bytes_and_qualify_logical_identity(
    tmp_path,
) -> None:
    source_bytes = b"export const answer = 42;\r\n"
    digest = source_content_sha256(source_bytes)

    with open_storage(tmp_path) as connection:
        first = store_source_revision(
            connection,
            repository_id="repo:one",
            path="src/answer.ts",
            source_bytes=source_bytes,
        )
        second = store_source_revision(
            connection,
            repository_id="repo:two",
            path="src/answer.ts",
            source_bytes=source_bytes,
        )

        assert first.content_sha256 == second.content_sha256 == digest
        assert first.id != second.id
        assert get_source_blob(connection, digest).bytes == source_bytes
        assert list_source_revisions(connection) == (first, second)
        assert (
            connection.execute("SELECT count(*) FROM source_blobs").fetchone()[0] == 1
        )


def test_source_revisions_are_byte_integral_and_immutable(tmp_path) -> None:
    source_bytes = "const café = '✓';\n".encode()
    digest = source_content_sha256(source_bytes)

    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="./src/example.ts",
            source_bytes=source_bytes,
            content_sha256=digest,
        )

        assert revision == SourceRevision(
            id=deterministic_source_revision_id(
                repository_id="repo:api",
                path="src/example.ts",
                content_sha256=digest,
            ),
            repository_id="repo:api",
            path="src/example.ts",
            content_sha256=digest,
        )
        assert get_source_revision(connection, revision.id) == revision
        assert get_source_blob(connection, digest).bytes == source_bytes
        with pytest.raises(ValueError, match="do not match"):
            store_source_blob(connection, b"changed", content_sha256=digest)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE source_blobs SET bytes = ? WHERE content_sha256 = ?",
                (b"changed", digest),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM source_revisions WHERE id = ?",
                (revision.id,),
            )


def test_source_revision_write_rolls_back_blob_when_revision_cannot_be_stored(
    tmp_path,
) -> None:
    original = b"original"
    replacement = b"replacement"
    original_digest = source_content_sha256(original)
    replacement_digest = source_content_sha256(replacement)

    with open_storage(tmp_path) as connection:
        original_revision = store_source_revision(
            connection,
            repository_id="repo:api",
            path="src/example.ts",
            source_bytes=original,
        )
        conflicting_id = deterministic_source_revision_id(
            repository_id="repo:api",
            path="src/example.ts",
            content_sha256=replacement_digest,
        )
        connection.execute(
            """
            INSERT INTO source_revisions (id, repository_id, path, content_sha256)
            VALUES (?, ?, ?, ?)
            """,
            (conflicting_id, "repo:other", "src/other.ts", original_digest),
        )

        with pytest.raises(ValueError, match="Existing source revision"):
            store_source_revision(
                connection,
                repository_id="repo:api",
                path="src/example.ts",
                source_bytes=replacement,
            )

        assert get_source_blob(connection, replacement_digest) is None
        assert (
            get_source_revision(connection, original_revision.id) == original_revision
        )


def test_facts_table_stores_latest_active_facts(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(facts)").fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(facts)").fetchall()
        }

        connection.execute(
            """
            INSERT INTO facts (
                id,
                extractor,
                kind,
                source_file,
                confidence
            )
            VALUES (
                'fact:entity:graphify:orders',
                'graphify',
                'service',
                'apps/api/src/orders.ts',
                0.92
            )
            """
        )
        row = connection.execute(
            """
            SELECT id, extractor, kind, source_file, identity_json,
                   properties_json, evidence_json, confidence,
                   conflict_metadata_json, extractor_version
            FROM facts
            """
        ).fetchone()

    assert columns["id"]["pk"] == 1
    assert columns["extractor"]["notnull"] == 1
    assert columns["kind"]["notnull"] == 1
    assert columns["source_file"]["notnull"] == 1
    assert columns["identity_json"]["dflt_value"] == "'{}'"
    assert columns["properties_json"]["dflt_value"] == "'{}'"
    assert columns["evidence_json"]["dflt_value"] == "'[]'"
    assert columns["conflict_metadata_json"]["dflt_value"] == "'{}'"
    assert columns["extractor_version"]["notnull"] == 0
    assert "idx_facts_extractor_source_file" in indexes
    assert dict(row) == {
        "id": "fact:entity:graphify:orders",
        "extractor": "graphify",
        "kind": "service",
        "source_file": "apps/api/src/orders.ts",
        "identity_json": "{}",
        "properties_json": "{}",
        "evidence_json": "[]",
        "confidence": 0.92,
        "conflict_metadata_json": "{}",
        "extractor_version": None,
    }


def test_raw_evidence_units_table_stores_queryable_scanned_evidence(
    tmp_path,
) -> None:
    with open_storage(tmp_path) as connection:
        columns = {
            row["name"]: row
            for row in connection.execute(
                "PRAGMA table_info(raw_evidence_units)",
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(raw_evidence_units)",
            ).fetchall()
        }

        connection.execute(
            """
            INSERT INTO raw_evidence_units (
                id,
                owner_entity_type,
                owner_entity_id,
                source_file,
                text,
                provenance,
                confidence
            )
            VALUES (
                'evidence-unit:graphify:orders',
                'service',
                'service:orders',
                'apps/api/src/orders.ts',
                'OrdersService handles order commands.',
                'graphify',
                0.91
            )
            """
        )
        row = connection.execute(
            """
            SELECT id, owner_entity_type, owner_entity_id, source_file,
                   start_line, end_line, text, provenance, confidence,
                   metadata_json
            FROM raw_evidence_units
            """
        ).fetchone()

    assert columns["id"]["pk"] == 1
    assert columns["owner_entity_type"]["notnull"] == 1
    assert columns["owner_entity_id"]["notnull"] == 1
    assert columns["source_file"]["notnull"] == 1
    assert columns["start_line"]["notnull"] == 0
    assert columns["end_line"]["notnull"] == 0
    assert columns["text"]["notnull"] == 1
    assert columns["provenance"]["notnull"] == 1
    assert columns["confidence"]["notnull"] == 1
    assert columns["metadata_json"]["dflt_value"] == "'{}'"
    assert "idx_raw_evidence_units_scan_scope" in indexes
    assert "idx_raw_evidence_units_owner" in indexes
    assert dict(row) == {
        "id": "evidence-unit:graphify:orders",
        "owner_entity_type": "service",
        "owner_entity_id": "service:orders",
        "source_file": "apps/api/src/orders.ts",
        "start_line": None,
        "end_line": None,
        "text": "OrdersService handles order commands.",
        "provenance": "graphify",
        "confidence": 0.91,
        "metadata_json": "{}",
    }


def test_evidence_embeddings_table_stores_model_vectors_and_text_hash(
    tmp_path,
) -> None:
    with open_storage(tmp_path) as connection:
        columns = {
            row["name"]: row
            for row in connection.execute(
                "PRAGMA table_info(evidence_embeddings)",
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(evidence_embeddings)",
            ).fetchall()
        }

        connection.execute(
            """
            INSERT INTO evidence_embeddings (
                evidence_unit_id,
                model_id,
                vector_json,
                dimensions,
                text_hash
            )
            VALUES (
                'evidence-unit:graphify:orders',
                'code-scout-local-hash-v1',
                '[0.25,0.75]',
                2,
                'hash'
            )
            """
        )
        row = connection.execute(
            """
            SELECT evidence_unit_id, model_id, vector_json, dimensions, text_hash
            FROM evidence_embeddings
            """
        ).fetchone()

    assert columns["evidence_unit_id"]["pk"] == 1
    assert columns["model_id"]["pk"] == 2
    assert columns["vector_json"]["notnull"] == 1
    assert columns["dimensions"]["notnull"] == 1
    assert columns["text_hash"]["notnull"] == 1
    assert "idx_evidence_embeddings_model" in indexes
    assert dict(row) == {
        "evidence_unit_id": "evidence-unit:graphify:orders",
        "model_id": "code-scout-local-hash-v1",
        "vector_json": "[0.25,0.75]",
        "dimensions": 2,
        "text_hash": "hash",
    }


def test_record_change_event_persists_typed_event(tmp_path) -> None:
    timestamp = "2026-06-21T12:00:00Z"
    event = ChangeEvent(
        id=deterministic_change_event_id(
            type="incremental_change",
            file="openapi.yaml",
            extractors_run=("openapi",),
            facts_added=4,
            facts_removed=3,
            nodes_updated=2,
            outputs_rebuilt=True,
            timestamp=timestamp,
            metadata={
                "outputs_planned": [
                    {"id": "docs:repositories:workspace", "kind": "doc"}
                ],
            },
        ),
        type="incremental_change",
        file="openapi.yaml",
        extractors_run=("openapi",),
        facts_added=4,
        facts_removed=3,
        nodes_updated=2,
        outputs_rebuilt=True,
        timestamp=timestamp,
        metadata={
            "outputs_planned": [{"id": "docs:repositories:workspace", "kind": "doc"}],
        },
    )

    with open_storage(tmp_path) as connection:
        assert record_change_event(connection, event) == event.id
        events = list_change_events(connection)

    assert events == (event,)
    assert events[0].to_dict() == {
        "id": event.id,
        "type": "incremental_change",
        "file": "openapi.yaml",
        "extractors_run": ("openapi",),
        "facts_added": 4,
        "facts_removed": 3,
        "nodes_updated": 2,
        "outputs_rebuilt": True,
        "timestamp": timestamp,
        "metadata": {
            "outputs_planned": [{"id": "docs:repositories:workspace", "kind": "doc"}],
        },
    }


def test_list_change_events_orders_by_timestamp_then_id(tmp_path) -> None:
    newest = ChangeEvent(
        id="change-event:20260621T120000Z:b",
        type="incremental_change",
        file="src/new.py",
        extractors_run=("tree_sitter",),
        facts_added=1,
        facts_removed=0,
        nodes_updated=1,
        outputs_rebuilt=True,
        timestamp="2026-06-21T12:00:00Z",
    )
    oldest = ChangeEvent(
        id="change-event:20260621T115900Z:a",
        type="incremental_delete",
        file="src/old.py",
        extractors_run=("tree_sitter",),
        facts_added=0,
        facts_removed=2,
        nodes_updated=1,
        outputs_rebuilt=True,
        timestamp="2026-06-21T11:59:00Z",
    )
    same_time_first = ChangeEvent(
        id="change-event:20260621T120000Z:a",
        type="incremental_change",
        file="src/a.py",
        extractors_run=("tree_sitter",),
        facts_added=1,
        facts_removed=1,
        nodes_updated=1,
        outputs_rebuilt=True,
        timestamp="2026-06-21T12:00:00Z",
    )

    with open_storage(tmp_path) as connection:
        record_change_event(connection, newest)
        record_change_event(connection, oldest)
        record_change_event(connection, same_time_first)
        events = list_change_events(connection)

    assert events == (oldest, same_time_first, newest)


def test_schema_initialization_adds_facts_without_replacing_graph_data(
    tmp_path,
) -> None:
    connection = sqlite3.connect(tmp_path / "existing.db")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL,
            schema_version TEXT NOT NULL DEFAULT '1',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE services (
            id TEXT PRIMARY KEY,
            repository_id TEXT,
            name TEXT NOT NULL,
            description TEXT,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        INSERT INTO workspaces (id, name, root_path)
        VALUES ('workspace', 'Workspace', '.');

        INSERT INTO services (id, name)
        VALUES ('service:cli', 'CLI');
        """
    )

    initialize_schema(connection)

    workspace_rows = connection.execute("SELECT id, name FROM workspaces").fetchall()
    service_rows = connection.execute("SELECT id, name FROM services").fetchall()
    facts_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(facts)")
    }
    facts_count = connection.execute("SELECT count(*) FROM facts").fetchone()[0]
    connection.close()

    assert [dict(row) for row in workspace_rows] == [
        {"id": "workspace", "name": "Workspace"}
    ]
    assert [dict(row) for row in service_rows] == [{"id": "service:cli", "name": "CLI"}]
    assert {
        "id",
        "extractor",
        "kind",
        "source_file",
        "identity_json",
        "properties_json",
        "evidence_json",
        "confidence",
        "conflict_metadata_json",
        "extractor_version",
        "created_at",
        "updated_at",
    } <= facts_columns
    assert facts_count == 0


def test_upsert_active_facts_inserts_and_lists_facts(tmp_path) -> None:
    service_fact = ActiveFact(
        id="fact:entity:graphify:orders",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/orders.ts",
        identity={
            "entity_type": "service",
            "entity_id": "service:orders",
        },
        properties={
            "name": "Orders",
            "repository_id": "repo:api",
        },
        evidence=(
            FactEvidence(
                file_path="apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.97,
                start_line=12,
                end_line=24,
                metadata={
                    "symbol": "OrdersService",
                    "tags": ["service", "orders"],
                },
            ),
        ),
        confidence=0.95,
        conflict_metadata={"strategy": "latest"},
        extractor_version="1.2.0",
    )
    contract_fact = ActiveFact(
        id="fact:entity:openapi:list-orders",
        extractor="openapi",
        kind="contract",
        source_file="contracts/openapi.yaml",
        identity={
            "entity_type": "contract",
            "entity_id": "contract:list-orders",
        },
        properties={
            "method": "GET",
            "path": "/orders",
        },
        evidence=(
            FactEvidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
                metadata={"operation_id": "listOrders"},
            ),
        ),
        confidence=1.0,
    )

    with open_storage(tmp_path) as connection:
        fact_ids = upsert_active_facts(connection, [service_fact, contract_fact])
        facts = list_active_facts(connection)

    assert fact_ids == (service_fact.id, contract_fact.id)
    assert facts == (service_fact, contract_fact)
    assert facts[0].evidence[0].confidence == 0.97
    assert facts[0].evidence[0].metadata == {
        "symbol": "OrdersService",
        "tags": ["service", "orders"],
    }
    assert facts[0].confidence == 0.95
    assert facts[0].conflict_metadata == {"strategy": "latest"}


def test_upsert_active_facts_updates_existing_fact(tmp_path) -> None:
    fact = ActiveFact(
        id="fact:entity:graphify:orders",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/orders.ts",
        identity={"entity_type": "service", "entity_id": "service:orders"},
        properties={"name": "Orders"},
        evidence=(
            FactEvidence(
                file_path="apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.72,
            ),
        ),
        confidence=0.72,
        extractor_version="1.0.0",
    )
    updated_fact = ActiveFact(
        id=fact.id,
        extractor=fact.extractor,
        kind=fact.kind,
        source_file=fact.source_file,
        identity=fact.identity,
        properties={"name": "Orders API", "owner": "platform"},
        evidence=(
            FactEvidence(
                file_path="apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.93,
                start_line=4,
                metadata={"symbol": "OrdersApi"},
            ),
        ),
        confidence=0.93,
        conflict_metadata={"supersedes": "scan-1"},
        extractor_version="1.1.0",
    )

    with open_storage(tmp_path) as connection:
        assert upsert_active_facts(connection, (fact,)) == (fact.id,)
        assert upsert_active_facts(connection, (updated_fact,)) == (fact.id,)
        facts = list_active_facts(connection)

    assert facts == (updated_fact,)


def test_list_active_facts_by_extractor_and_source_file_filters_rows(tmp_path) -> None:
    graphify_orders = ActiveFact(
        id="fact:entity:graphify:orders",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/orders.ts",
        identity={"entity_type": "service", "entity_id": "service:orders"},
        properties={"name": "Orders"},
        evidence=(),
        confidence=0.91,
    )
    graphify_users = ActiveFact(
        id="fact:entity:graphify:users",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/users.ts",
        identity={"entity_type": "service", "entity_id": "service:users"},
        properties={"name": "Users"},
        evidence=(),
        confidence=0.9,
    )
    openapi_orders = ActiveFact(
        id="fact:entity:openapi:orders",
        extractor="openapi",
        kind="contract",
        source_file="apps/api/src/orders.ts",
        identity={"entity_type": "contract", "entity_id": "contract:orders"},
        properties={"path": "/orders"},
        evidence=(),
        confidence=1.0,
    )

    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (graphify_users, graphify_orders, openapi_orders),
        )
        facts = list_active_facts_by_extractor_and_source_file(
            connection,
            extractor="graphify",
            source_file="apps/api/src/orders.ts",
        )

    assert facts == (graphify_orders,)


def test_delete_active_facts_by_extractor_and_source_file_only_removes_scope(
    tmp_path,
) -> None:
    graphify_orders = _active_fact("fact:entity:graphify:orders")
    graphify_users = _active_fact(
        "fact:entity:graphify:users",
        source_file="apps/api/src/users.ts",
    )
    openapi_orders = _active_fact(
        "fact:entity:openapi:orders",
        extractor="openapi",
        kind="contract",
    )

    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (graphify_orders, graphify_users, openapi_orders),
        )
        removed = delete_active_facts_by_extractor_and_source_file(
            connection,
            extractor="graphify",
            source_file="apps/api/src/orders.ts",
        )
        facts = list_active_facts(connection)

    assert removed == 1
    assert facts == (graphify_users, openapi_orders)


def test_replace_active_facts_by_extractor_and_source_file_preserves_unrelated_facts(
    tmp_path,
) -> None:
    stale_orders_service = _active_fact("fact:entity:graphify:orders-service")
    stale_orders_contract = _active_fact(
        "fact:entity:graphify:orders-contract",
        kind="contract",
    )
    graphify_users = _active_fact(
        "fact:entity:graphify:users",
        source_file="apps/api/src/users.ts",
    )
    openapi_orders = _active_fact(
        "fact:entity:openapi:orders",
        extractor="openapi",
        kind="contract",
    )
    fresh_orders_service = _active_fact(
        "fact:entity:graphify:orders-service:v2",
        kind="service",
    )

    with open_storage(tmp_path) as connection:
        upsert_active_facts(
            connection,
            (
                stale_orders_service,
                stale_orders_contract,
                graphify_users,
                openapi_orders,
            ),
        )
        counts = replace_active_facts_by_extractor_and_source_file(
            connection,
            extractor="graphify",
            source_file="apps/api/src/orders.ts",
            facts=(fresh_orders_service,),
        )
        facts = list_active_facts(connection)

    assert counts == ActiveFactReplacementCounts(removed=2, inserted=1)
    assert facts == (fresh_orders_service, graphify_users, openapi_orders)


def test_replace_active_facts_by_extractor_and_source_file_rolls_back_on_insert_error(
    tmp_path,
) -> None:
    stale_orders_service = _active_fact("fact:entity:graphify:orders-service")
    invalid_replacement = ActiveFact(
        id="fact:entity:graphify:orders-invalid",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/orders.ts",
        identity={"entity_type": "service", "entity_id": object()},
        properties={},
        evidence=(),
        confidence=0.9,
    )

    with open_storage(tmp_path) as connection:
        upsert_active_facts(connection, (stale_orders_service,))
        with pytest.raises(TypeError):
            replace_active_facts_by_extractor_and_source_file(
                connection,
                extractor="graphify",
                source_file="apps/api/src/orders.ts",
                facts=(invalid_replacement,),
            )
        facts = list_active_facts(connection)

    assert facts == (stale_orders_service,)


def test_deterministic_raw_evidence_unit_id_is_stable_for_unchanged_input() -> None:
    first_id = deterministic_raw_evidence_unit_id(
        owner_entity_type="service",
        owner_entity_id="service:orders",
        source_file="./apps/api/src/orders.ts",
        start_line=12,
        end_line=18,
        text="OrdersService handles order commands.",
        provenance="graphify",
    )
    repeated_id = deterministic_raw_evidence_unit_id(
        owner_entity_type="service",
        owner_entity_id="service:orders",
        source_file="apps/api/src/orders.ts",
        start_line=12,
        end_line=18,
        text="OrdersService handles order commands.",
        provenance="graphify",
    )
    changed_text_id = deterministic_raw_evidence_unit_id(
        owner_entity_type="service",
        owner_entity_id="service:orders",
        source_file="apps/api/src/orders.ts",
        start_line=12,
        end_line=18,
        text="OrdersService handles order queries.",
        provenance="graphify",
    )

    assert first_id == repeated_id
    assert first_id != changed_text_id
    assert first_id.startswith(
        "evidence-unit:graphify:apps-api-src-orders-ts:service:service-orders:",
    )


def test_upsert_raw_evidence_units_inserts_and_lists_units(tmp_path) -> None:
    service_unit = RawEvidenceUnit(
        id=deterministic_raw_evidence_unit_id(
            owner_entity_type="service",
            owner_entity_id="service:orders",
            source_file="apps/api/src/orders.ts",
            start_line=12,
            end_line=18,
            text="OrdersService handles order commands.",
            provenance="graphify",
        ),
        owner_entity_type="service",
        owner_entity_id="service:orders",
        source_file="apps/api/src/orders.ts",
        start_line=12,
        end_line=18,
        text="OrdersService handles order commands.",
        provenance="graphify",
        confidence=0.91,
        metadata={"symbol": "OrdersService", "tags": ["orders", "service"]},
    )
    contract_unit = RawEvidenceUnit(
        id=deterministic_raw_evidence_unit_id(
            owner_entity_type="contract",
            owner_entity_id="contract:list-orders",
            source_file="contracts/openapi.yaml",
            text="GET /orders returns an order list.",
            provenance="openapi",
        ),
        owner_entity_type="contract",
        owner_entity_id="contract:list-orders",
        source_file="contracts/openapi.yaml",
        text="GET /orders returns an order list.",
        provenance="openapi",
        confidence=1.0,
        metadata={"operation_id": "listOrders"},
    )

    with open_storage(tmp_path) as connection:
        unit_ids = upsert_raw_evidence_units(
            connection,
            (service_unit, contract_unit),
        )
        units = list_raw_evidence_units(connection)

    assert unit_ids == (service_unit.id, contract_unit.id)
    assert units == tuple(
        sorted((service_unit, contract_unit), key=lambda unit: unit.id),
    )
    assert units[0].metadata


def test_upsert_raw_evidence_units_updates_existing_unit(tmp_path) -> None:
    unit = _raw_evidence_unit("evidence-unit:graphify:orders")
    updated_unit = RawEvidenceUnit(
        id=unit.id,
        owner_entity_type=unit.owner_entity_type,
        owner_entity_id=unit.owner_entity_id,
        source_file=unit.source_file,
        start_line=10,
        end_line=20,
        text="OrdersService handles order commands and queries.",
        provenance=unit.provenance,
        confidence=0.95,
        metadata={"symbol": "OrdersService", "revision": 2},
    )

    with open_storage(tmp_path) as connection:
        assert upsert_raw_evidence_units(connection, (unit,)) == (unit.id,)
        assert upsert_raw_evidence_units(connection, (updated_unit,)) == (unit.id,)
        units = list_raw_evidence_units(connection)

    assert units == (updated_unit,)


def test_list_raw_evidence_units_by_scan_scope_filters_rows(tmp_path) -> None:
    graphify_orders = _raw_evidence_unit("evidence-unit:graphify:orders")
    graphify_users = _raw_evidence_unit(
        "evidence-unit:graphify:users",
        owner_entity_id="service:users",
        source_file="apps/api/src/users.ts",
    )
    openapi_orders = _raw_evidence_unit(
        "evidence-unit:openapi:orders",
        owner_entity_type="contract",
        owner_entity_id="contract:list-orders",
        provenance="openapi",
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(
            connection,
            (graphify_users, graphify_orders, openapi_orders),
        )
        units = list_raw_evidence_units_by_scan_scope(
            connection,
            provenance="graphify",
            source_file="apps/api/src/orders.ts",
        )

    assert units == (graphify_orders,)


def test_delete_raw_evidence_units_by_scan_scope_only_removes_scope(
    tmp_path,
) -> None:
    graphify_orders = _raw_evidence_unit("evidence-unit:graphify:orders")
    graphify_users = _raw_evidence_unit(
        "evidence-unit:graphify:users",
        source_file="apps/api/src/users.ts",
    )
    openapi_orders = _raw_evidence_unit(
        "evidence-unit:openapi:orders",
        provenance="openapi",
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(
            connection,
            (graphify_orders, graphify_users, openapi_orders),
        )
        removed = delete_raw_evidence_units_by_scan_scope(
            connection,
            provenance="graphify",
            source_file="apps/api/src/orders.ts",
        )
        units = list_raw_evidence_units(connection)

    assert removed == 1
    assert units == (graphify_users, openapi_orders)


def test_replace_raw_evidence_units_by_scan_scope_preserves_unrelated_units(
    tmp_path,
) -> None:
    stale_orders_service = _raw_evidence_unit("evidence-unit:graphify:orders-service")
    stale_orders_symbol = _raw_evidence_unit(
        "evidence-unit:graphify:orders-symbol",
        owner_entity_type="symbol",
        owner_entity_id="symbol:OrdersService",
    )
    graphify_users = _raw_evidence_unit(
        "evidence-unit:graphify:users",
        source_file="apps/api/src/users.ts",
    )
    openapi_orders = _raw_evidence_unit(
        "evidence-unit:openapi:orders",
        provenance="openapi",
    )
    fresh_orders_service = _raw_evidence_unit(
        "evidence-unit:graphify:orders-service:v2",
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(
            connection,
            (
                stale_orders_service,
                stale_orders_symbol,
                graphify_users,
                openapi_orders,
            ),
        )
        counts = replace_raw_evidence_units_by_scan_scope(
            connection,
            provenance="graphify",
            source_file="apps/api/src/orders.ts",
            evidence_units=(fresh_orders_service,),
        )
        units = list_raw_evidence_units(connection)

    assert counts == RawEvidenceUnitReplacementCounts(removed=2, inserted=1)
    assert units == tuple(
        sorted(
            (fresh_orders_service, graphify_users, openapi_orders),
            key=lambda unit: unit.id,
        ),
    )


def test_replace_raw_evidence_units_by_scan_scope_rejects_mismatched_scope(
    tmp_path,
) -> None:
    users_unit = _raw_evidence_unit(
        "evidence-unit:graphify:users",
        source_file="apps/api/src/users.ts",
    )

    with open_storage(tmp_path) as connection:
        with pytest.raises(ValueError):
            replace_raw_evidence_units_by_scan_scope(
                connection,
                provenance="graphify",
                source_file="apps/api/src/orders.ts",
                evidence_units=(users_unit,),
            )


def test_replace_raw_evidence_units_by_scan_scope_rolls_back_on_insert_error(
    tmp_path,
) -> None:
    stale_orders_service = _raw_evidence_unit("evidence-unit:graphify:orders-service")
    invalid_replacement = RawEvidenceUnit(
        id="evidence-unit:graphify:orders-invalid",
        owner_entity_type="service",
        owner_entity_id="service:orders",
        source_file="apps/api/src/orders.ts",
        start_line=12,
        end_line=18,
        text="OrdersService handles order commands.",
        provenance="graphify",
        confidence=0.91,
        metadata={"unserializable": object()},
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, (stale_orders_service,))
        with pytest.raises(TypeError):
            replace_raw_evidence_units_by_scan_scope(
                connection,
                provenance="graphify",
                source_file="apps/api/src/orders.ts",
                evidence_units=(invalid_replacement,),
            )
        units = list_raw_evidence_units(connection)

    assert units == (stale_orders_service,)


def test_refresh_evidence_embeddings_creates_and_skips_unchanged_text(
    tmp_path,
) -> None:
    provider = _CountingEmbeddingProvider()
    unit = _raw_evidence_unit("evidence-unit:graphify:orders")

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, (unit,))
        first_counts = refresh_evidence_embeddings(
            connection,
            (unit,),
            provider=provider,
        )
        second_counts = refresh_evidence_embeddings(
            connection,
            (unit,),
            provider=provider,
        )
        embeddings = list_evidence_embeddings(connection)

    assert first_counts.created == 1
    assert first_counts.refreshed == 0
    assert first_counts.unchanged == 0
    assert second_counts.created == 0
    assert second_counts.refreshed == 0
    assert second_counts.unchanged == 1
    assert provider.texts == [unit.text]
    assert embeddings[0].evidence_unit_id == unit.id
    assert embeddings[0].model_id == provider.model_id
    assert embeddings[0].dimensions == provider.dimensions
    assert embeddings[0].text_hash == evidence_text_hash(unit.text)
    assert len(embeddings) == 1


def test_refresh_evidence_embeddings_refreshes_changed_text_for_same_unit(
    tmp_path,
) -> None:
    provider = _CountingEmbeddingProvider()
    unit = _raw_evidence_unit("evidence-unit:graphify:orders")
    updated_unit = RawEvidenceUnit(
        id=unit.id,
        owner_entity_type=unit.owner_entity_type,
        owner_entity_id=unit.owner_entity_id,
        source_file=unit.source_file,
        start_line=unit.start_line,
        end_line=unit.end_line,
        text="OrdersService handles order commands and queries.",
        provenance=unit.provenance,
        confidence=unit.confidence,
        metadata=unit.metadata,
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, (unit,))
        refresh_evidence_embeddings(connection, (unit,), provider=provider)
        upsert_raw_evidence_units(connection, (updated_unit,))
        counts = refresh_evidence_embeddings(
            connection,
            (updated_unit,),
            provider=provider,
        )
        embeddings = list_evidence_embeddings(connection)

    assert counts.created == 0
    assert counts.refreshed == 1
    assert counts.unchanged == 0
    assert provider.texts == [unit.text, updated_unit.text]
    assert embeddings[0].text_hash == evidence_text_hash(updated_unit.text)
    assert len(embeddings) == 1


def test_delete_stale_evidence_embeddings_removes_missing_evidence_units(
    tmp_path,
) -> None:
    provider = _CountingEmbeddingProvider()
    unit = _raw_evidence_unit("evidence-unit:graphify:orders")

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, (unit,))
        refresh_evidence_embeddings(connection, (unit,), provider=provider)
        delete_raw_evidence_units_by_scan_scope(
            connection,
            provenance=unit.provenance,
            source_file=unit.source_file,
        )
        removed = delete_stale_evidence_embeddings(connection)
        embeddings = list_evidence_embeddings(connection)

    assert removed == 1
    assert embeddings == ()


def test_upsert_repository_preserves_stable_id_and_updates_values(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection,
            id="workspace",
            name="Workspace",
            root_path=str(tmp_path),
        )
        repository = Repository(
            id="repo:code-scout",
            name="code-scout",
            path=".",
            default_branch="main",
        )
        updated_repository = Repository(
            id=repository.id,
            name="code-scout Core",
            path=".",
            remote_url="https://example.test/code-scout.git",
            default_branch="trunk",
            language_hints=("TypeScript",),
            framework_hints=("Next.js", "turbo"),
            module_hints=("frontend",),
        )

        assert upsert_repository(connection, "workspace", repository) == repository.id
        assert upsert_repository(connection, "workspace", updated_repository) == (
            repository.id
        )

        rows = connection.execute(
            """
            SELECT id, workspace_id, name, path, remote_url, default_branch,
                   language_hints_json, framework_hints_json, module_hints_json
            FROM repositories
            """
        ).fetchall()

    assert [dict(row) for row in rows] == [
        {
            "id": "repo:code-scout",
            "workspace_id": "workspace",
            "name": "code-scout Core",
            "path": ".",
            "remote_url": "https://example.test/code-scout.git",
            "default_branch": "trunk",
            "language_hints_json": '["TypeScript"]',
            "framework_hints_json": '["Next.js", "turbo"]',
            "module_hints_json": '["frontend"]',
        }
    ]


def test_upsert_file_and_symbol_preserve_ids_and_update_location(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        _seed_repository(connection, tmp_path)
        file = File(
            id="file:src/code_scout/storage.py",
            repository_id="repo:code-scout",
            path="src/code_scout/storage.py",
            kind="source",
            extension=".py",
            language="python",
            content_hash="old",
            modified_at="2026-06-20T10:00:00Z",
        )
        updated_file = File(
            id=file.id,
            repository_id=file.repository_id,
            path=file.path,
            kind=file.kind,
            extension=file.extension,
            language="python",
            content_hash="new",
            modified_at="2026-06-20T10:01:00Z",
        )
        symbol = Symbol(
            id="symbol:code_scout.storage.open_storage",
            file_id=file.id,
            name="open_storage",
            kind="function",
            qualified_name="code_scout.storage.open_storage",
            start_line=10,
            end_line=20,
        )
        updated_symbol = Symbol(
            id=symbol.id,
            file_id=file.id,
            name=symbol.name,
            kind=symbol.kind,
            qualified_name=symbol.qualified_name,
            start_line=12,
            end_line=24,
        )

        assert upsert_file(connection, file) == file.id
        assert upsert_file(connection, updated_file) == file.id
        assert upsert_symbol(connection, symbol) == symbol.id
        assert upsert_symbol(connection, updated_symbol) == symbol.id

        file_rows = connection.execute(
            """
            SELECT id, kind, extension, content_hash, modified_at
            FROM files
            """
        ).fetchall()
        symbol_rows = connection.execute(
            "SELECT id, start_line, end_line FROM symbols"
        ).fetchall()

    assert [dict(row) for row in file_rows] == [
        {
            "id": "file:src/code_scout/storage.py",
            "kind": "source",
            "extension": ".py",
            "content_hash": "new",
            "modified_at": "2026-06-20T10:01:00Z",
        }
    ]
    assert [dict(row) for row in symbol_rows] == [
        {
            "id": "symbol:code_scout.storage.open_storage",
            "start_line": 12,
            "end_line": 24,
        }
    ]


def test_upsert_domain_nodes_preserve_ids_and_update_values(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        _seed_repository(connection, tmp_path)

        assert (
            upsert_service(
                connection,
                Service(
                    id="service:cli",
                    name="CLI",
                    repository_id="repo:code-scout",
                ),
            )
            == "service:cli"
        )
        assert (
            upsert_service(
                connection,
                Service(
                    id="service:cli",
                    name="CLI",
                    repository_id="repo:code-scout",
                    description="Command surface",
                    aliases=("Command line", "code-scout cli"),
                    evidence=(
                        Evidence(
                            file_path="src\/code_scout\/commands\/cli.py",
                            extractor_name="dotnet",
                            confidence=0.91,
                            start_line=1,
                            end_line=1,
                        ),
                    ),
                ),
            )
            == "service:cli"
        )
        assert (
            upsert_contract(
                connection,
                Contract(id="contract:okf", name="OKF", kind="export"),
            )
            == "contract:okf"
        )
        assert (
            upsert_contract(
                connection,
                Contract(
                    id="contract:okf",
                    name="OKF",
                    kind="export",
                    path="/health",
                    version="1",
                    method="GET",
                    producer_hint="Docs API",
                    operation_id="getDocs",
                    evidence=(
                        Evidence(
                            file_path="openapi.yaml",
                            extractor_name="openapi",
                            confidence=1.0,
                        ),
                    ),
                ),
            )
            == "contract:okf"
        )
        assert (
            upsert_concept(
                connection,
                Concept(id="concept:memory", name="Memory"),
            )
            == "concept:memory"
        )
        assert (
            upsert_concept(
                connection,
                Concept(
                    id="concept:memory",
                    name="Architecture memory",
                    description="Local code knowledge",
                ),
            )
            == "concept:memory"
        )
        assert (
            upsert_decision(
                connection,
                Decision(
                    id="decision:sqlite",
                    title="Use SQLite",
                    status="proposed",
                ),
            )
            == "decision:sqlite"
        )
        assert (
            upsert_decision(
                connection,
                Decision(
                    id="decision:sqlite",
                    title="Use SQLite",
                    status="accepted",
                    adr_id="0001",
                    path="docs/adr/0001-storage.md",
                    decided_at="2026-06-20",
                    context="code-scout needs durable local storage.",
                    decision="Use SQLite.",
                    consequences="Storage remains available offline.",
                    evidence=(
                        Evidence(
                            file_path="docs/adr/0001-storage.md",
                            extractor_name="adr_markdown",
                            confidence=1.0,
                            start_line=7,
                            end_line=15,
                        ),
                    ),
                ),
            )
            == "decision:sqlite"
        )

        service_rows = connection.execute(
            "SELECT id, description, aliases_json, evidence_json FROM services"
        ).fetchall()
        contract_rows = connection.execute(
            """
            SELECT id, path, version, method, producer_hint, operation_id,
                   request_dtos_json, response_dtos_json, evidence_json
            FROM contracts
            """
        ).fetchall()
        concept_rows = connection.execute(
            "SELECT id, name, description FROM concepts"
        ).fetchall()
        decision_rows = connection.execute(
            """
            SELECT id, status, adr_id, path, decided_at, context, decision,
                   consequences, evidence_json
            FROM decisions
            """
        ).fetchall()

    assert [dict(row) for row in service_rows] == [
        {
            "id": "service:cli",
            "description": "Command surface",
            "aliases_json": service_rows[0]["aliases_json"],
            "evidence_json": service_rows[0]["evidence_json"],
        }
    ]
    assert json.loads(service_rows[0]["aliases_json"]) == [
        "Command line",
        "code-scout cli",
    ]
    assert json.loads(service_rows[0]["evidence_json"]) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {}, extractor_provenance=("dotnet",)
                    ).to_dict()
                )
            ),
            "file_path": "src\/code_scout\/commands\/cli.py",
            "extractor_name": "dotnet",
            "confidence": 0.91,
            "start_line": 1,
            "end_line": 1,
            "metadata": {},
        }
    ]
    assert [dict(row) for row in contract_rows] == [
        {
            "id": "contract:okf",
            "path": "/health",
            "version": "1",
            "method": "GET",
            "producer_hint": "Docs API",
            "operation_id": "getDocs",
            "request_dtos_json": "[]",
            "response_dtos_json": "[]",
            "evidence_json": contract_rows[0]["evidence_json"],
        }
    ]
    assert json.loads(contract_rows[0]["evidence_json"]) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {}, extractor_provenance=("openapi",)
                    ).to_dict()
                )
            ),
            "file_path": "openapi.yaml",
            "extractor_name": "openapi",
            "confidence": 1.0,
            "start_line": None,
            "end_line": None,
            "metadata": {},
        }
    ]
    assert [dict(row) for row in concept_rows] == [
        {
            "id": "concept:memory",
            "name": "Architecture memory",
            "description": "Local code knowledge",
        }
    ]
    assert [dict(row) for row in decision_rows] == [
        {
            "id": "decision:sqlite",
            "status": "accepted",
            "adr_id": "0001",
            "path": "docs/adr/0001-storage.md",
            "decided_at": "2026-06-20",
            "context": "code-scout needs durable local storage.",
            "decision": "Use SQLite.",
            "consequences": "Storage remains available offline.",
            "evidence_json": decision_rows[0]["evidence_json"],
        }
    ]
    assert json.loads(decision_rows[0]["evidence_json"]) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {}, extractor_provenance=("adr_markdown",)
                    ).to_dict()
                )
            ),
            "file_path": "docs/adr/0001-storage.md",
            "extractor_name": "adr_markdown",
            "confidence": 1.0,
            "start_line": 7,
            "end_line": 15,
            "metadata": {},
        }
    ]


def test_upsert_edge_preserves_stable_relation_and_updates_evidence(tmp_path) -> None:
    old_evidence = Evidence(
        file_path="src\/code_scout\/commands\/cli.py",
        extractor_name="manual",
        confidence=0.7,
        start_line=10,
    )
    new_evidence = Evidence(
        file_path="src\/code_scout\/commands\/cli.py",
        extractor_name="tree_sitter",
        confidence=0.95,
        start_line=12,
        end_line=18,
    )
    edge = Edge(
        source_type="service",
        source_id="service:cli",
        target_type="concept",
        target_id="concept:memory",
        relation_type="documents",
        confidence=0.7,
        evidence=(old_evidence,),
    )
    updated_edge = Edge(
        source_type=edge.source_type,
        source_id=edge.source_id,
        target_type=edge.target_type,
        target_id=edge.target_id,
        relation_type=edge.relation_type,
        confidence=0.95,
        evidence=(new_evidence,),
    )

    with open_storage(tmp_path) as connection:
        first_id = upsert_edge(connection, edge)
        second_id = upsert_edge(connection, updated_edge)
        rows = connection.execute(
            "SELECT id, confidence, evidence_json FROM edges"
        ).fetchall()

    assert second_id == first_id
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.95
    assert json.loads(rows[0]["evidence_json"]) == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence(
                        {}, extractor_provenance=("tree_sitter",)
                    ).to_dict()
                )
            ),
            "file_path": "src\/code_scout\/commands\/cli.py",
            "extractor_name": "tree_sitter",
            "confidence": 0.95,
            "start_line": 12,
            "end_line": 18,
            "metadata": {},
        }
    ]


def test_graph_summary_counts_read_persisted_graph_rows(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        _seed_repository(connection, tmp_path)
        upsert_file(
            connection,
            File(
                id="file:api",
                repository_id="repo:code-scout",
                path="api/openapi.yaml",
            ),
        )
        upsert_symbol(
            connection,
            Symbol(
                id="symbol:handler",
                file_id="file:api",
                name="handler",
                kind="function",
            ),
        )
        upsert_service(
            connection,
            Service(
                id="service:api",
                name="API",
                repository_id="repo:code-scout",
            ),
        )
        upsert_contract(
            connection,
            Contract(id="contract:api", name="GET /api", kind="http"),
        )
        upsert_concept(connection, Concept(id="concept:api", name="API"))
        upsert_decision(
            connection,
            Decision(id="decision:api", title="Expose API", status="accepted"),
        )
        upsert_edge(
            connection,
            Edge(
                source_type="concept",
                source_id="concept:api",
                target_type="contract",
                target_id="contract:api",
                relation_type="describes_contract",
                confidence=0.85,
            ),
        )

        assert graph_summary_counts(connection) == {
            "files": 1,
            "symbols": 1,
            "services": 1,
            "contracts": 1,
            "concepts": 1,
            "decisions": 1,
            "edges": 1,
        }


def test_create_snapshot_records_store_metadata(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection,
            id="workspace",
            name="Workspace",
            root_path=str(tmp_path),
        )
        snapshot = Snapshot(
            id="snapshot:2026-06-20T10:00:00Z",
            workspace_id="workspace",
            created_at="2026-06-20T10:00:00Z",
        )
        assert (
            create_snapshot(
                connection,
                snapshot,
                metadata={"extractor_count": 2},
            )
            == snapshot.id
        )
        snapshot_rows = connection.execute(
            "SELECT id, workspace_id, metadata_json FROM snapshots"
        ).fetchall()

    assert [dict(row) for row in snapshot_rows] == [
        {
            "id": "snapshot:2026-06-20T10:00:00Z",
            "workspace_id": "workspace",
            "metadata_json": '{"extractor_count": 2}',
        }
    ]


def test_snapshot_payload_is_deterministic_deduplicated_and_retrievable(
    tmp_path,
) -> None:
    payload = SnapshotPayload(
        active_facts=(
            {
                "id": "fact:one",
                "properties": {
                    "timestamp": "domain-timestamp",
                    "nested": {"created_at": "domain-created-at"},
                },
                "evidence": (
                    {
                        "metadata": {
                            "updated_at": "evidence-updated-at",
                            "nested": {"scanned_at": "evidence-scanned-at"},
                        }
                    },
                ),
            },
        ),
        resolved_entities={"repositories": ({"id": "repo:one"},)},
        resolved_edges=(),
        raw_evidence=({"id": "evidence:one"},),
        revision_references=(
            {
                "id": "source-revision:repo:one:file.py:digest",
                "content_sha256": "a" * 64,
            },
        ),
        repository_identities=({"id": "repo:one"},),
        extractor_versions=({"extractor": "tree_sitter", "version": "1"},),
        scan_scope={"selected_extractors": ["tree_sitter"]},
        coverage={"discovered_files": 1},
    )
    reordered = payload.to_dict() | {"coverage": {"discovered_files": 1}}

    assert canonical_snapshot_payload(payload) == canonical_snapshot_payload(reordered)
    digest = snapshot_payload_sha256(payload)
    with open_storage(tmp_path) as connection:
        assert store_snapshot_payload(connection, payload) == digest
        assert store_snapshot_payload(connection, reordered) == digest
        assert get_snapshot_payload(connection, digest) == get_snapshot_payload(
            connection, digest
        )
        assert (
            connection.execute("SELECT count(*) FROM snapshot_payloads").fetchone()[0]
            == 1
        )
        stored = get_snapshot_payload(connection, digest)
        assert stored is not None
        assert stored.active_facts[0]["properties"] == {
            "timestamp": "domain-timestamp",
            "nested": {"created_at": "domain-created-at"},
        }
        assert stored.active_facts[0]["evidence"][0]["metadata"] == {
            "updated_at": "evidence-updated-at",
            "nested": {"scanned_at": "evidence-scanned-at"},
        }

    changed = payload.to_dict()
    changed["active_facts"][0]["properties"]["timestamp"] = "domain-timestamp-v2"
    assert snapshot_payload_sha256(changed) != digest


def test_snapshot_occurrence_time_is_not_immutable_payload_content(tmp_path) -> None:
    payload = SnapshotPayload(
        active_facts=(),
        resolved_entities={},
        resolved_edges=(),
        raw_evidence=(),
        revision_references=(),
        repository_identities=(),
        extractor_versions=(),
        scan_scope={},
        coverage={},
    )
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection, id="workspace", name="Workspace", root_path=str(tmp_path)
        )
        create_snapshot(
            connection,
            Snapshot("snapshot:first", "workspace", "2026-06-20T10:00:00Z"),
            payload=payload,
        )
        create_snapshot(
            connection,
            Snapshot("snapshot:second", "workspace", "2026-06-20T10:01:00Z"),
            payload=payload,
        )
        assert (
            connection.execute("SELECT count(*) FROM snapshot_payloads").fetchone()[0]
            == 1
        )


def test_snapshot_payload_publication_is_atomic_immutable_and_detects_corruption(
    tmp_path,
) -> None:
    payload = SnapshotPayload(
        active_facts=(),
        resolved_entities={},
        resolved_edges=(),
        raw_evidence=(),
        revision_references=(),
        repository_identities=(),
        extractor_versions=(),
        scan_scope={},
        coverage={},
    )
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection, id="workspace", name="Workspace", root_path=str(tmp_path)
        )
        snapshot = Snapshot("snapshot:one", "workspace", "2026-06-20T10:00:00Z")
        create_snapshot(connection, snapshot, payload=payload)
        digest = snapshot_payload_sha256(payload)
        assert list_snapshots(connection)[0].content_status == "available"
        with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced"):
            connection.execute(
                "UPDATE snapshots SET payload_sha256 = NULL WHERE id = ?",
                (snapshot.id,),
            )
        connection.execute("DROP TRIGGER prevent_snapshot_payload_mutation_content")
        connection.execute(
            "UPDATE snapshot_payloads SET payload_json = '{}' WHERE content_sha256 = ?",
            (digest,),
        )
        with pytest.raises(ValueError, match="integrity"):
            get_snapshot_payload(connection, digest)

    with open_storage(tmp_path) as connection:
        create_snapshot(
            connection,
            Snapshot("snapshot:legacy", "workspace", "2026-06-21T10:00:00Z"),
        )
        statuses = {item.id: item.content_status for item in list_snapshots(connection)}
    assert statuses["snapshot:legacy"] == "historical_content_unavailable"


def test_snapshot_payload_publication_rolls_back_on_invalid_content(tmp_path) -> None:
    invalid_payload = SnapshotPayload(
        active_facts=(),
        resolved_entities={},
        resolved_edges=(),
        raw_evidence=(),
        revision_references=(),
        repository_identities=(),
        extractor_versions=(),
        scan_scope={},
        coverage={},
        version=2,
    )
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection, id="workspace", name="Workspace", root_path=str(tmp_path)
        )
        with pytest.raises(ValueError, match="Unsupported snapshot payload version"):
            create_snapshot(
                connection,
                Snapshot("snapshot:invalid", "workspace", "2026-06-20T10:00:00Z"),
                payload=invalid_payload,
            )
        assert connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM snapshot_payloads").fetchone()[0]
            == 0
        )


def _seed_repository(connection: sqlite3.Connection, tmp_path) -> None:
    upsert_workspace(
        connection,
        id="workspace",
        name="Workspace",
        root_path=str(tmp_path),
    )
    upsert_repository(
        connection,
        "workspace",
        Repository(id="repo:code-scout", name="code-scout", path="."),
    )
