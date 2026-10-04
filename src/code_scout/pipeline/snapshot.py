"""Create an explicit historical checkpoint from already committed index state."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_scout.core.errors import StorageError
from code_scout.domain.resolver import resolve_active_facts
from code_scout.models.graph import Snapshot
from code_scout.pipeline.scan import _referenced_source_revisions, _snapshot_id
from code_scout.storage import (
    create_snapshot,
    database_path,
    get_snapshot_payload,
    list_active_facts,
    list_raw_evidence_units,
    snapshot_payload_from_records,
)
from code_scout.storage.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class SnapshotCheckpoint:
    snapshot_id: str
    content_sha256: str

    def render(self, output_format: str) -> str:
        payload = {
            "snapshot_id": self.snapshot_id,
            "content_sha256": self.content_sha256,
        }
        if output_format == "json":
            return json.dumps(payload, indent=2, sort_keys=True)
        return (
            f"Snapshot created: {self.snapshot_id}\n"
            f"Content digest: {self.content_sha256}"
        )


def snapshot_workspace(workspace_path: str = ".") -> SnapshotCheckpoint:
    """Publish current committed index state without reading workspace files."""
    path = database_path(workspace_path)
    connection = _open_compatible_index(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            workspace_id = _indexed_workspace_id(connection, path)
            active_facts = list_active_facts(connection)
            resolved_graph = resolve_active_facts(active_facts)
            raw_evidence = list_raw_evidence_units(connection)
            revisions = _referenced_source_revisions(connection, active_facts)
            latest_snapshot = connection.execute(
                """
                SELECT id, metadata_json, payload_sha256
                FROM snapshots
                WHERE workspace_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (workspace_id,),
            ).fetchone()
            known_scope, known_coverage = _known_snapshot_context(
                connection, latest_snapshot
            )
            latest_event = connection.execute(
                """
                SELECT id FROM change_events
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            timestamp = _timestamp()
            snapshot = Snapshot(
                id=_snapshot_id(workspace_id, timestamp),
                workspace_id=workspace_id,
                created_at=timestamp,
            )
            metadata: dict[str, Any] = {
                "checkpoint": True,
                "known_scan_scope": known_scope,
                "known_coverage": known_coverage,
                "latest_change_event_id": latest_event["id"]
                if latest_event is not None
                else None,
                "predecessor_snapshot_id": latest_snapshot["id"]
                if latest_snapshot is not None
                else None,
            }
            payload = snapshot_payload_from_records(
                active_facts=active_facts,
                resolved_graph=resolved_graph,
                raw_evidence=raw_evidence,
                revision_references=revisions,
                scan_scope=known_scope,
                coverage=known_coverage,
            )
            create_snapshot(connection, snapshot, metadata=metadata, payload=payload)
            digest = connection.execute(
                "SELECT payload_sha256 FROM snapshots WHERE id = ?", (snapshot.id,)
            ).fetchone()["payload_sha256"]
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return SnapshotCheckpoint(snapshot.id, digest)


def _open_compatible_index(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise StorageError(
            f"No initialized index exists at {path.resolve()}.",
            "Run `code-scout scan --workspace PATH` before creating a snapshot.",
        )
    try:
        connection = sqlite3.connect(path, timeout=0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        required = {
            "workspaces",
            "facts",
            "raw_evidence_units",
            "snapshots",
            "snapshot_payloads",
            "source_revisions",
            "change_events",
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if version != SCHEMA_VERSION or not required <= tables:
            connection.close()
            raise StorageError(
                f"Index at {path.resolve()} is not compatible with snapshots.",
                "Run `code-scout scan --workspace PATH` to initialize or upgrade "
                "the index, then retry.",
            )
        return connection
    except sqlite3.OperationalError as error:
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise StorageError(
                f"Workspace database is busy: {path.resolve()}.",
                "Wait for the active scan or watch update to finish, then retry. "
                "Do not delete database lock files.",
            ) from error
        raise StorageError(
            f"Workspace database is unreadable: {path.resolve()}.",
            "Check the database permissions and integrity, then retry.",
        ) from error


def _indexed_workspace_id(connection: sqlite3.Connection, path: Path) -> str:
    rows = connection.execute("SELECT id FROM workspaces ORDER BY id").fetchall()
    if len(rows) != 1:
        raise StorageError(
            f"Index at {path.resolve()} has no unambiguous initialized workspace.",
            "Run `code-scout scan --workspace PATH` before creating a snapshot.",
        )
    return str(rows[0]["id"])


def _known_snapshot_context(
    connection: sqlite3.Connection, latest_snapshot: sqlite3.Row | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if latest_snapshot is None:
        return {}, {}
    metadata = json.loads(latest_snapshot["metadata_json"])
    digest = latest_snapshot["payload_sha256"]
    payload = get_snapshot_payload(connection, digest) if digest else None
    if payload is not None:
        return dict(payload.scan_scope), dict(payload.coverage)
    return _legacy_snapshot_context(metadata)


def _legacy_snapshot_context(metadata: object) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read historical nested checkpoint context without republishing its history."""
    current = metadata
    while isinstance(current, Mapping):
        scope = current.get("known_scan_scope")
        coverage = current.get("known_coverage")
        if isinstance(scope, Mapping) and isinstance(coverage, Mapping):
            return dict(scope), dict(coverage)
        current = current.get("prior_snapshot_metadata")
    return {}, {}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
