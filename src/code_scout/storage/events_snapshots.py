from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from code_scout.models.graph import Snapshot
from code_scout.storage.common import _json_object, _json_string_tuple
from code_scout.storage.snapshot_payloads import (
    SnapshotPayload,
    attach_snapshot_payload,
    store_snapshot_payload,
)


@dataclass(frozen=True)
class ChangeEvent:
    id: str
    type: str
    file: str
    extractors_run: tuple[str, ...]
    facts_added: int
    facts_removed: int
    nodes_updated: int
    outputs_rebuilt: bool
    timestamp: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        event = asdict(self)
        event["metadata"] = self.metadata
        return event


def deterministic_change_event_id(
    *,
    type: str,
    file: str,
    extractors_run: tuple[str, ...],
    facts_added: int,
    facts_removed: int,
    nodes_updated: int,
    outputs_rebuilt: bool,
    timestamp: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    payload = {
        "type": type,
        "file": file,
        "extractors_run": extractors_run,
        "facts_added": facts_added,
        "facts_removed": facts_removed,
        "nodes_updated": nodes_updated,
        "outputs_rebuilt": outputs_rebuilt,
        "timestamp": timestamp,
        "metadata": metadata or {},
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()[:12]
    safe_timestamp = (
        timestamp.replace(":", "").replace("-", "").replace(".", "").replace("+", "")
    )
    return f"change-event:{safe_timestamp}:{digest}"


def record_change_event(
    connection: sqlite3.Connection,
    event: ChangeEvent,
) -> str:
    connection.execute(
        """
        INSERT INTO change_events (
            id,
            type,
            file,
            extractors_run_json,
            facts_added,
            facts_removed,
            nodes_updated,
            outputs_rebuilt,
            metadata_json,
            timestamp
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _change_event_row(event),
    )
    return event.id


def update_change_event_metadata(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    metadata: dict[str, Any],
) -> None:
    connection.execute(
        """
        UPDATE change_events
        SET metadata_json = ?
        WHERE id = ?
        """,
        (_json_object(metadata), event_id),
    )


def list_change_events(connection: sqlite3.Connection) -> tuple[ChangeEvent, ...]:
    rows = connection.execute(
        """
        SELECT id, type, file, extractors_run_json, facts_added, facts_removed,
               nodes_updated, outputs_rebuilt, metadata_json, timestamp
        FROM change_events
        ORDER BY timestamp, id
        """
    ).fetchall()
    return tuple(_change_event_from_row(row) for row in rows)


def create_snapshot(
    connection: sqlite3.Connection,
    snapshot: Snapshot,
    metadata: dict[str, Any] | None = None,
    payload: SnapshotPayload | dict[str, Any] | None = None,
) -> str:
    """Publish an occurrence and, when supplied, its immutable content atomically."""
    connection.execute("SAVEPOINT create_snapshot")
    try:
        connection.execute(
            """
            INSERT INTO snapshots (id, workspace_id, created_at, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                snapshot.id,
                snapshot.workspace_id,
                snapshot.created_at,
                _json_object(metadata or {}),
            ),
        )
        if payload is not None:
            attach_snapshot_payload(
                connection,
                snapshot.id,
                store_snapshot_payload(connection, payload),
            )
        connection.execute("RELEASE SAVEPOINT create_snapshot")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT create_snapshot")
        connection.execute("RELEASE SAVEPOINT create_snapshot")
        raise
    return snapshot.id


def _change_event_row(event: ChangeEvent) -> tuple[Any, ...]:
    return (
        event.id,
        event.type,
        event.file,
        _json_string_tuple(event.extractors_run),
        event.facts_added,
        event.facts_removed,
        event.nodes_updated,
        int(event.outputs_rebuilt),
        _json_object(event.metadata or {}),
        event.timestamp,
    )


def _change_event_from_row(row: sqlite3.Row) -> ChangeEvent:
    return ChangeEvent(
        id=row["id"],
        type=row["type"],
        file=row["file"],
        extractors_run=tuple(json.loads(row["extractors_run_json"])),
        facts_added=row["facts_added"],
        facts_removed=row["facts_removed"],
        nodes_updated=row["nodes_updated"],
        outputs_rebuilt=bool(row["outputs_rebuilt"]),
        metadata=json.loads(row["metadata_json"]),
        timestamp=row["timestamp"],
    )
