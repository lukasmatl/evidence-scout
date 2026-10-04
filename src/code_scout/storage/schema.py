from __future__ import annotations

import sqlite3
from pathlib import Path

from code_scout.core.errors import StorageError
from code_scout.workspace.paths import DATABASE_PATH

DEFAULT_DATABASE_PATH = DATABASE_PATH
SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT '1',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    remote_url TEXT,
    default_branch TEXT,
    language_hints_json TEXT NOT NULL DEFAULT '[]',
    framework_hints_json TEXT NOT NULL DEFAULT '[]',
    module_hints_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'source',
    extension TEXT NOT NULL DEFAULT '',
    language TEXT,
    content_hash TEXT,
    modified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS symbols (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    qualified_name TEXT,
    start_line INTEGER,
    end_line INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    repository_id TEXT REFERENCES repositories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    description TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT,
    version TEXT,
    method TEXT,
    producer_hint TEXT,
    operation_id TEXT,
    request_dtos_json TEXT NOT NULL DEFAULT '[]',
    response_dtos_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS concepts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    adr_id TEXT,
    path TEXT,
    decided_at TEXT,
    context TEXT,
    decision TEXT,
    consequences TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (
        source_type,
        source_id,
        target_type,
        target_id,
        relation_type
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique_relation
ON edges (
    source_type,
    source_id,
    target_type,
    target_id,
    relation_type
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    extractor TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_file TEXT NOT NULL,
    identity_json TEXT NOT NULL DEFAULT '{}',
    properties_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL,
    conflict_metadata_json TEXT NOT NULL DEFAULT '{}',
    extractor_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_facts_extractor_source_file
ON facts (extractor, source_file);

CREATE TABLE IF NOT EXISTS raw_evidence_units (
    id TEXT PRIMARY KEY,
    owner_entity_type TEXT NOT NULL,
    owner_entity_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    text TEXT NOT NULL,
    provenance TEXT NOT NULL,
    confidence REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_raw_evidence_units_scan_scope
ON raw_evidence_units (provenance, source_file);

CREATE INDEX IF NOT EXISTS idx_raw_evidence_units_owner
ON raw_evidence_units (owner_entity_type, owner_entity_id);

CREATE TABLE IF NOT EXISTS evidence_embeddings (
    evidence_unit_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (evidence_unit_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_embeddings_model
ON evidence_embeddings (model_id);

CREATE TABLE IF NOT EXISTS change_events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    file TEXT NOT NULL,
    extractors_run_json TEXT NOT NULL DEFAULT '[]',
    facts_added INTEGER NOT NULL,
    facts_removed INTEGER NOT NULL,
    nodes_updated INTEGER NOT NULL,
    outputs_rebuilt INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_events_timestamp
ON change_events (timestamp, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

-- A payload is immutable content addressed history.  A snapshot is an
-- occurrence (and can therefore have different timestamps/metadata while
-- pointing at the same content).
CREATE TABLE IF NOT EXISTS snapshot_payloads (
    content_sha256 TEXT PRIMARY KEY,
    format_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    CHECK (length(content_sha256) = 64),
    CHECK (content_sha256 = lower(content_sha256)),
    CHECK (content_sha256 NOT GLOB '*[^0-9a-f]*')
);

CREATE TABLE IF NOT EXISTS source_blobs (
    content_sha256 TEXT PRIMARY KEY,
    bytes BLOB NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(content_sha256) = 64),
    CHECK (content_sha256 = lower(content_sha256)),
    CHECK (content_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (size_bytes = length(bytes))
);

CREATE TABLE IF NOT EXISTS source_revisions (
    id TEXT PRIMARY KEY,
    repository_id TEXT NOT NULL,
    path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL REFERENCES source_blobs(content_sha256),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (repository_id, path, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_source_revisions_repository_path
ON source_revisions (repository_id, path, created_at, id);

-- Captured source is historical evidence, never current-graph state.  Keep it
-- immutable even if a caller bypasses the storage helpers.
CREATE TRIGGER IF NOT EXISTS prevent_source_blob_mutation
BEFORE UPDATE ON source_blobs
BEGIN
    SELECT RAISE(ABORT, 'source blobs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_source_blob_deletion
BEFORE DELETE ON source_blobs
BEGIN
    SELECT RAISE(ABORT, 'source blobs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_source_revision_mutation
BEFORE UPDATE ON source_revisions
BEGIN
    SELECT RAISE(ABORT, 'source revisions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS prevent_source_revision_deletion
BEFORE DELETE ON source_revisions
BEGIN
    SELECT RAISE(ABORT, 'source revisions are immutable');
END;

PRAGMA user_version = 3;
"""


def database_path(workspace_path: str | Path = ".") -> Path:
    return Path(workspace_path) / DEFAULT_DATABASE_PATH


def open_storage(workspace_path: str | Path = ".") -> sqlite3.Connection:
    path = database_path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_schema(connection)
    except Exception as error:
        connection.close()
        code = getattr(error, "sqlite_errorcode", 0)
        if isinstance(error, sqlite3.OperationalError) and code & 0xFF in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            raise StorageError(
                f"Workspace database is busy: {path.resolve()}.",
                "Wait for the active scan, watch update, or other database operation "
                "to finish, then retry. If your tool returned a running session, "
                "wait on that session instead of starting another scan. "
                "Do not delete the database or its lock/journal files.",
            ) from error
        raise
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    _ensure_column(
        connection,
        "repositories",
        "language_hints_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "repositories",
        "framework_hints_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "repositories",
        "module_hints_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(connection, "files", "kind", "TEXT NOT NULL DEFAULT 'source'")
    _ensure_column(connection, "files", "extension", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "files", "modified_at", "TEXT")
    _ensure_column(
        connection,
        "services",
        "aliases_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "services",
        "evidence_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(connection, "contracts", "method", "TEXT")
    _ensure_column(connection, "contracts", "producer_hint", "TEXT")
    _ensure_column(connection, "contracts", "operation_id", "TEXT")
    _ensure_column(
        connection,
        "contracts",
        "request_dtos_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "contracts",
        "response_dtos_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "contracts",
        "evidence_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(connection, "decisions", "adr_id", "TEXT")
    _ensure_column(connection, "decisions", "context", "TEXT")
    _ensure_column(connection, "decisions", "decision", "TEXT")
    _ensure_column(connection, "decisions", "consequences", "TEXT")
    _ensure_column(
        connection,
        "decisions",
        "evidence_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "change_events",
        "extractors_run_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "change_events",
        "metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        connection,
        "raw_evidence_units",
        "metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        connection,
        "facts",
        "identity_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        connection,
        "facts",
        "properties_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        connection,
        "facts",
        "evidence_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        connection,
        "facts",
        "conflict_metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(connection, "facts", "extractor_version", "TEXT")
    _ensure_column(
        connection,
        "snapshots",
        "metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(connection, "snapshots", "payload_sha256", "TEXT")
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_payload_sha256
        ON snapshots (payload_sha256);

        CREATE TRIGGER IF NOT EXISTS prevent_snapshot_payload_mutation
        BEFORE UPDATE OF payload_sha256 ON snapshots
        WHEN OLD.payload_sha256 IS NOT NULL
             AND NEW.payload_sha256 IS NOT OLD.payload_sha256
        BEGIN
            SELECT RAISE(ABORT, 'published snapshot payload cannot be replaced');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_snapshot_payload_mutation_content
        BEFORE UPDATE ON snapshot_payloads
        BEGIN
            SELECT RAISE(ABORT, 'snapshot payloads are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_snapshot_payload_deletion
        BEFORE DELETE ON snapshot_payloads
        BEGIN
            SELECT RAISE(ABORT, 'snapshot payloads are immutable');
        END;
        """
    )
    _ensure_column(
        connection,
        "evidence_embeddings",
        "created_at",
        "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    )
    _ensure_column(
        connection,
        "evidence_embeddings",
        "updated_at",
        "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    )
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    existing_columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")
    }
    if column_name not in existing_columns:
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"
        )
