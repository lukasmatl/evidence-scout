"""Immutable, content-addressed snapshot payload storage."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from code_scout.models.facts import ActiveFact
from code_scout.storage.raw_evidence import RawEvidenceUnit
from code_scout.storage.source_revisions import SourceRevision

SNAPSHOT_PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class SnapshotPayload:
    """The portable historical content of a scan, excluding occurrence data."""

    active_facts: tuple[Any, ...]
    resolved_entities: Mapping[str, tuple[Any, ...]]
    resolved_edges: tuple[Any, ...]
    raw_evidence: tuple[Any, ...]
    revision_references: tuple[Any, ...]
    repository_identities: tuple[Any, ...]
    extractor_versions: tuple[Any, ...]
    scan_scope: Mapping[str, Any]
    coverage: Mapping[str, Any]
    version: int = SNAPSHOT_PAYLOAD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "active_facts": self.active_facts,
            "resolved_entities": self.resolved_entities,
            "resolved_edges": self.resolved_edges,
            "raw_evidence": self.raw_evidence,
            "revision_references": self.revision_references,
            "repository_identities": self.repository_identities,
            "extractor_versions": self.extractor_versions,
            "scan_scope": self.scan_scope,
            "coverage": self.coverage,
        }


@dataclass(frozen=True)
class StoredSnapshot:
    id: str
    workspace_id: str
    created_at: str
    metadata: dict[str, Any]
    content_sha256: str | None
    content_status: str


def snapshot_payload_from_records(
    *,
    active_facts: tuple[ActiveFact, ...],
    resolved_graph: Any,
    raw_evidence: tuple[RawEvidenceUnit, ...],
    revision_references: tuple[SourceRevision, ...],
    scan_scope: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> SnapshotPayload:
    """Build the full historical payload from the publication boundary."""
    entity_names = (
        "repositories",
        "files",
        "symbols",
        "services",
        "contracts",
        "concepts",
        "decisions",
    )
    entities = {name: tuple(getattr(resolved_graph, name)) for name in entity_names}
    extractor_versions = tuple(
        {
            "extractor": extractor,
            "version": version,
        }
        for extractor, version in sorted(
            {(fact.extractor, fact.extractor_version) for fact in active_facts},
            key=lambda item: (item[0], item[1] or ""),
        )
    )
    return SnapshotPayload(
        active_facts=active_facts,
        resolved_entities=entities,
        resolved_edges=tuple(resolved_graph.edges),
        raw_evidence=raw_evidence,
        revision_references=revision_references,
        repository_identities=tuple(resolved_graph.repositories),
        extractor_versions=extractor_versions,
        scan_scope=dict(scan_scope),
        coverage=dict(coverage),
    )


def canonical_snapshot_payload(payload: SnapshotPayload | Mapping[str, Any]) -> bytes:
    """Serialize immutable content deterministically.

    Snapshot occurrence data (including ``Snapshot.created_at``) is stored on
    the snapshot row, not in this payload. Do not remove keys by name here:
    fact properties and evidence metadata are extractor-owned domain evidence.
    """
    value = payload.to_dict() if isinstance(payload, SnapshotPayload) else dict(payload)
    if value.get("version") != SNAPSHOT_PAYLOAD_VERSION:
        raise ValueError(
            f"Unsupported snapshot payload version: {value.get('version')!r}"
        )
    canonical = _json_value(value)
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def snapshot_payload_sha256(payload: SnapshotPayload | Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_snapshot_payload(payload)).hexdigest()


def store_snapshot_payload(
    connection: sqlite3.Connection,
    payload: SnapshotPayload | Mapping[str, Any],
) -> str:
    """Deduplicate and verify a canonical immutable payload."""
    serialized = canonical_snapshot_payload(payload)
    digest = hashlib.sha256(serialized).hexdigest()
    connection.execute(
        """
        INSERT INTO snapshot_payloads (content_sha256, format_version, payload_json)
        VALUES (?, ?, ?)
        ON CONFLICT(content_sha256) DO NOTHING
        """,
        (digest, SNAPSHOT_PAYLOAD_VERSION, serialized.decode("utf-8")),
    )
    stored = _get_snapshot_payload_bytes(connection, digest)
    if stored is None or stored != serialized:
        raise ValueError(
            "Stored snapshot payload failed content_sha256 integrity check"
        )
    return digest


def get_snapshot_payload(
    connection: sqlite3.Connection, content_sha256: str
) -> SnapshotPayload | None:
    stored = _get_snapshot_payload_bytes(connection, content_sha256)
    if stored is None:
        return None
    return _payload_from_dict(json.loads(stored))


def attach_snapshot_payload(
    connection: sqlite3.Connection, snapshot_id: str, content_sha256: str
) -> None:
    if _get_snapshot_payload_bytes(connection, content_sha256) is None:
        raise ValueError("Snapshot payload does not exist")
    cursor = connection.execute(
        "UPDATE snapshots SET payload_sha256 = ? WHERE id = ?",
        (content_sha256, snapshot_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("Snapshot does not exist")


def list_snapshots(connection: sqlite3.Connection) -> tuple[StoredSnapshot, ...]:
    rows = connection.execute(
        """
        SELECT id, workspace_id, created_at, metadata_json, payload_sha256
        FROM snapshots ORDER BY created_at, id
        """
    ).fetchall()
    return tuple(
        StoredSnapshot(
            id=row["id"],
            workspace_id=row["workspace_id"],
            created_at=row["created_at"],
            metadata=json.loads(row["metadata_json"]),
            content_sha256=row["payload_sha256"],
            content_status=(
                "available"
                if row["payload_sha256"] is not None
                else "historical_content_unavailable"
            ),
        )
        for row in rows
    )


def _get_snapshot_payload_bytes(
    connection: sqlite3.Connection, content_sha256: str
) -> bytes | None:
    row = connection.execute(
        """
        SELECT content_sha256, payload_json
        FROM snapshot_payloads
        WHERE content_sha256 = ?
        """,
        (content_sha256,),
    ).fetchone()
    if row is None:
        return None
    value = row["payload_json"].encode("utf-8")
    if hashlib.sha256(value).hexdigest() != row["content_sha256"]:
        raise ValueError(
            "Stored snapshot payload failed content_sha256 integrity check"
        )
    return value


def _payload_from_dict(value: dict[str, Any]) -> SnapshotPayload:
    return SnapshotPayload(
        version=value["version"],
        active_facts=tuple(value["active_facts"]),
        resolved_entities={
            name: tuple(entities)
            for name, entities in value["resolved_entities"].items()
        },
        resolved_edges=tuple(value["resolved_edges"]),
        raw_evidence=tuple(value["raw_evidence"]),
        revision_references=tuple(value["revision_references"]),
        repository_identities=tuple(value["repository_identities"]),
        extractor_versions=tuple(value["extractor_versions"]),
        scan_scope=value["scan_scope"],
        coverage=value["coverage"],
    )


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    return value
