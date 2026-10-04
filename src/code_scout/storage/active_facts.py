from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.storage.common import _json_object, _json_tuple


@dataclass(frozen=True)
class ActiveFactReplacementCounts:
    removed: int
    inserted: int


def upsert_active_facts(
    connection: sqlite3.Connection,
    facts: Iterable[ActiveFact],
) -> tuple[str, ...]:
    fact_ids: list[str] = []
    for fact in facts:
        connection.execute(
            """
            INSERT INTO facts (
                id,
                extractor,
                kind,
                source_file,
                identity_json,
                properties_json,
                evidence_json,
                confidence,
                conflict_metadata_json,
                extractor_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                extractor = excluded.extractor,
                kind = excluded.kind,
                source_file = excluded.source_file,
                identity_json = excluded.identity_json,
                properties_json = excluded.properties_json,
                evidence_json = excluded.evidence_json,
                confidence = excluded.confidence,
                conflict_metadata_json = excluded.conflict_metadata_json,
                extractor_version = excluded.extractor_version,
                updated_at = CURRENT_TIMESTAMP
            """,
            _active_fact_row(fact),
        )
        fact_ids.append(fact.id)
    return tuple(fact_ids)


def delete_active_facts_by_extractor_and_source_file(
    connection: sqlite3.Connection,
    *,
    extractor: str,
    source_file: str,
    repository_id: str | None = None,
) -> int:
    if repository_id is None:
        cursor = connection.execute(
            """
            DELETE FROM facts
            WHERE extractor = ?
              AND source_file = ?
            """,
            (extractor, source_file),
        )
    else:
        cursor = connection.execute(
            """
            DELETE FROM facts
            WHERE extractor = ?
              AND source_file = ?
              AND (
                  json_extract(evidence_json, '$[0].metadata.repository_id') = ?
                  OR json_extract(properties_json, '$.repository_id') = ?
              )
            """,
            (extractor, source_file, repository_id, repository_id),
        )
    return cursor.rowcount


def replace_active_facts_by_extractor_and_source_file(
    connection: sqlite3.Connection,
    *,
    extractor: str,
    source_file: str,
    facts: Iterable[ActiveFact],
    repository_id: str | None = None,
) -> ActiveFactReplacementCounts:
    replacement_facts = tuple(facts)
    for fact in replacement_facts:
        if fact.extractor != extractor or fact.source_file != source_file:
            msg = (
                "Replacement facts must match the extractor and source_file "
                "being replaced"
            )
            raise ValueError(msg)
        if repository_id is not None and _repository_id_for_fact(fact) != repository_id:
            msg = "Replacement facts must match the repository being replaced"
            raise ValueError(msg)
    connection.execute("SAVEPOINT replace_active_facts_by_source")
    try:
        removed = delete_active_facts_by_extractor_and_source_file(
            connection,
            extractor=extractor,
            source_file=source_file,
            repository_id=repository_id,
        )
        inserted = len(upsert_active_facts(connection, replacement_facts))
        connection.execute("RELEASE SAVEPOINT replace_active_facts_by_source")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT replace_active_facts_by_source")
        connection.execute("RELEASE SAVEPOINT replace_active_facts_by_source")
        raise
    return ActiveFactReplacementCounts(removed=removed, inserted=inserted)


def list_active_facts(connection: sqlite3.Connection) -> tuple[ActiveFact, ...]:
    rows = connection.execute(
        """
        SELECT id, extractor, kind, source_file, identity_json, properties_json,
               evidence_json, confidence, conflict_metadata_json, extractor_version
        FROM facts
        ORDER BY id
        """
    ).fetchall()
    return tuple(_active_fact_from_row(row) for row in rows)


def list_active_facts_by_extractor_and_source_file(
    connection: sqlite3.Connection,
    *,
    extractor: str,
    source_file: str,
) -> tuple[ActiveFact, ...]:
    rows = connection.execute(
        """
        SELECT id, extractor, kind, source_file, identity_json, properties_json,
               evidence_json, confidence, conflict_metadata_json, extractor_version
        FROM facts
        WHERE extractor = ?
          AND source_file = ?
        ORDER BY id
        """,
        (extractor, source_file),
    ).fetchall()
    return tuple(_active_fact_from_row(row) for row in rows)


def _active_fact_row(fact: ActiveFact) -> tuple[Any, ...]:
    return (
        fact.id,
        fact.extractor,
        fact.kind,
        fact.source_file,
        _json_object(fact.identity),
        _json_object(fact.properties),
        _json_tuple(fact.evidence),
        fact.confidence,
        _json_object(fact.conflict_metadata),
        fact.extractor_version,
    )


def _active_fact_from_row(row: sqlite3.Row) -> ActiveFact:
    return ActiveFact(
        id=row["id"],
        extractor=row["extractor"],
        kind=row["kind"],
        source_file=row["source_file"],
        identity=json.loads(row["identity_json"]),
        properties=json.loads(row["properties_json"]),
        evidence=tuple(
            FactEvidence(
                file_path=evidence["file_path"],
                extractor_name=evidence["extractor_name"],
                confidence=evidence["confidence"],
                start_line=evidence.get("start_line"),
                end_line=evidence.get("end_line"),
                metadata=evidence.get("metadata", {}),
            )
            for evidence in json.loads(row["evidence_json"])
        ),
        confidence=row["confidence"],
        conflict_metadata=json.loads(row["conflict_metadata_json"]),
        extractor_version=row["extractor_version"],
    )


def _repository_id_for_fact(fact: ActiveFact) -> str | None:
    for evidence in fact.evidence:
        repository_id = evidence.metadata.get("repository_id")
        if isinstance(repository_id, str):
            return repository_id
    repository_id = fact.properties.get("repository_id")
    return repository_id if isinstance(repository_id, str) else None
