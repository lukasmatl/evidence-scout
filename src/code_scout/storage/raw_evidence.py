from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from code_scout.storage.common import _json_object
from code_scout.utils.identifiers import slugify_identifier


@dataclass(frozen=True)
class RawEvidenceUnit:
    id: str
    owner_entity_type: str
    owner_entity_id: str
    source_file: str
    text: str
    provenance: str
    confidence: float
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RawEvidenceUnitReplacementCounts:
    removed: int
    inserted: int


def deterministic_raw_evidence_unit_id(
    *,
    owner_entity_type: str,
    owner_entity_id: str,
    source_file: str,
    text: str,
    provenance: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    source_file = _canonical_source_file(source_file)
    payload = {
        "owner_entity_type": owner_entity_type,
        "owner_entity_id": owner_entity_id,
        "source_file": source_file,
        "start_line": start_line,
        "end_line": end_line,
        "text": text,
        "provenance": provenance,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()[:12]
    owner_type_slug = slugify_identifier(owner_entity_type, fallback="entity")
    owner_id_slug = slugify_identifier(owner_entity_id, fallback="owner")
    source_slug = slugify_identifier(source_file, fallback="source")
    provenance_slug = slugify_identifier(provenance, fallback="provenance")
    return (
        f"evidence-unit:{provenance_slug}:{source_slug}:"
        f"{owner_type_slug}:{owner_id_slug}:{digest}"
    )


def upsert_raw_evidence_units(
    connection: sqlite3.Connection,
    evidence_units: Iterable[RawEvidenceUnit],
) -> tuple[str, ...]:
    evidence_unit_ids: list[str] = []
    for evidence_unit in evidence_units:
        connection.execute(
            """
            INSERT INTO raw_evidence_units (
                id,
                owner_entity_type,
                owner_entity_id,
                source_file,
                start_line,
                end_line,
                text,
                provenance,
                confidence,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                owner_entity_type = excluded.owner_entity_type,
                owner_entity_id = excluded.owner_entity_id,
                source_file = excluded.source_file,
                start_line = excluded.start_line,
                end_line = excluded.end_line,
                text = excluded.text,
                provenance = excluded.provenance,
                confidence = excluded.confidence,
                metadata_json = excluded.metadata_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            _raw_evidence_unit_row(evidence_unit),
        )
        evidence_unit_ids.append(evidence_unit.id)
    return tuple(evidence_unit_ids)


def delete_raw_evidence_units_by_scan_scope(
    connection: sqlite3.Connection,
    *,
    provenance: str,
    source_file: str,
) -> int:
    cursor = connection.execute(
        """
        DELETE FROM raw_evidence_units
        WHERE provenance = ?
          AND source_file = ?
        """,
        (provenance, source_file),
    )
    return cursor.rowcount


def replace_raw_evidence_units_by_scan_scope(
    connection: sqlite3.Connection,
    *,
    provenance: str,
    source_file: str,
    evidence_units: Iterable[RawEvidenceUnit],
) -> RawEvidenceUnitReplacementCounts:
    replacement_units = tuple(evidence_units)
    for evidence_unit in replacement_units:
        if (
            evidence_unit.provenance != provenance
            or evidence_unit.source_file != source_file
        ):
            msg = (
                "Replacement evidence units must match the provenance and "
                "source_file being replaced"
            )
            raise ValueError(msg)
    connection.execute("SAVEPOINT replace_raw_evidence_units_by_scan_scope")
    try:
        removed = delete_raw_evidence_units_by_scan_scope(
            connection,
            provenance=provenance,
            source_file=source_file,
        )
        inserted = len(upsert_raw_evidence_units(connection, replacement_units))
        connection.execute("RELEASE SAVEPOINT replace_raw_evidence_units_by_scan_scope")
    except Exception:
        connection.execute("ROLLBACK TO replace_raw_evidence_units_by_scan_scope")
        connection.execute("RELEASE SAVEPOINT replace_raw_evidence_units_by_scan_scope")
        raise
    return RawEvidenceUnitReplacementCounts(removed=removed, inserted=inserted)


def list_raw_evidence_units(
    connection: sqlite3.Connection,
) -> tuple[RawEvidenceUnit, ...]:
    rows = connection.execute(
        """
        SELECT id, owner_entity_type, owner_entity_id, source_file,
               start_line, end_line, text, provenance, confidence, metadata_json
        FROM raw_evidence_units
        ORDER BY id
        """
    ).fetchall()
    return tuple(_raw_evidence_unit_from_row(row) for row in rows)


def list_raw_evidence_units_by_scan_scope(
    connection: sqlite3.Connection,
    *,
    provenance: str,
    source_file: str,
) -> tuple[RawEvidenceUnit, ...]:
    rows = connection.execute(
        """
        SELECT id, owner_entity_type, owner_entity_id, source_file,
               start_line, end_line, text, provenance, confidence, metadata_json
        FROM raw_evidence_units
        WHERE provenance = ?
          AND source_file = ?
        ORDER BY id
        """,
        (provenance, source_file),
    ).fetchall()
    return tuple(_raw_evidence_unit_from_row(row) for row in rows)


def _raw_evidence_unit_row(evidence_unit: RawEvidenceUnit) -> tuple[Any, ...]:
    return (
        evidence_unit.id,
        evidence_unit.owner_entity_type,
        evidence_unit.owner_entity_id,
        evidence_unit.source_file,
        evidence_unit.start_line,
        evidence_unit.end_line,
        evidence_unit.text,
        evidence_unit.provenance,
        evidence_unit.confidence,
        _json_object(evidence_unit.metadata),
    )


def _raw_evidence_unit_from_row(row: sqlite3.Row) -> RawEvidenceUnit:
    return RawEvidenceUnit(
        id=row["id"],
        owner_entity_type=row["owner_entity_type"],
        owner_entity_id=row["owner_entity_id"],
        source_file=row["source_file"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        text=row["text"],
        provenance=row["provenance"],
        confidence=row["confidence"],
        metadata=json.loads(row["metadata_json"]),
    )


def _canonical_source_file(source_file: str) -> str:
    return source_file.replace("\\", "/").removeprefix("./")
