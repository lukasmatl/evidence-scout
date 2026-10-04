from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from code_scout.evidence.embeddings import EmbeddingProvider, Vector
from code_scout.storage.raw_evidence import RawEvidenceUnit


@dataclass(frozen=True)
class EvidenceEmbedding:
    evidence_unit_id: str
    model_id: str
    vector: Vector
    dimensions: int
    text_hash: str


@dataclass(frozen=True)
class EvidenceEmbeddingRefreshCounts:
    created: int
    refreshed: int
    unchanged: int


def evidence_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def refresh_evidence_embeddings(
    connection: sqlite3.Connection,
    evidence_units: Iterable[RawEvidenceUnit],
    *,
    provider: EmbeddingProvider,
) -> EvidenceEmbeddingRefreshCounts:
    created = 0
    refreshed = 0
    unchanged = 0

    for evidence_unit in evidence_units:
        text_hash = evidence_text_hash(evidence_unit.text)
        existing = connection.execute(
            """
            SELECT text_hash, dimensions
            FROM evidence_embeddings
            WHERE evidence_unit_id = ?
              AND model_id = ?
            """,
            (evidence_unit.id, provider.model_id),
        ).fetchone()
        if (
            existing is not None
            and existing["text_hash"] == text_hash
            and existing["dimensions"] == provider.dimensions
        ):
            unchanged += 1
            continue

        vector = provider.embed_text(evidence_unit.text)
        if len(vector) != provider.dimensions:
            msg = (
                "Embedding provider returned a vector whose length does not "
                "match its dimensions"
            )
            raise ValueError(msg)

        connection.execute(
            """
            INSERT INTO evidence_embeddings (
                evidence_unit_id,
                model_id,
                vector_json,
                dimensions,
                text_hash
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(evidence_unit_id, model_id) DO UPDATE SET
                vector_json = excluded.vector_json,
                dimensions = excluded.dimensions,
                text_hash = excluded.text_hash,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                evidence_unit.id,
                provider.model_id,
                _vector_json(vector),
                provider.dimensions,
                text_hash,
            ),
        )
        if existing is None:
            created += 1
        else:
            refreshed += 1

    return EvidenceEmbeddingRefreshCounts(
        created=created,
        refreshed=refreshed,
        unchanged=unchanged,
    )


def list_evidence_embeddings(
    connection: sqlite3.Connection,
) -> tuple[EvidenceEmbedding, ...]:
    rows = connection.execute(
        """
        SELECT evidence_unit_id, model_id, vector_json, dimensions, text_hash
        FROM evidence_embeddings
        ORDER BY evidence_unit_id, model_id
        """
    ).fetchall()
    return tuple(_evidence_embedding_from_row(row) for row in rows)


def delete_stale_evidence_embeddings(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        """
        DELETE FROM evidence_embeddings
        WHERE evidence_unit_id NOT IN (
            SELECT id
            FROM raw_evidence_units
        )
        """
    )
    return cursor.rowcount


def _vector_json(vector: Vector) -> str:
    return json.dumps(list(vector), separators=(",", ":"))


def _evidence_embedding_from_row(row: sqlite3.Row) -> EvidenceEmbedding:
    vector = json.loads(row["vector_json"])
    return EvidenceEmbedding(
        evidence_unit_id=row["evidence_unit_id"],
        model_id=row["model_id"],
        vector=tuple(float(value) for value in vector),
        dimensions=row["dimensions"],
        text_hash=row["text_hash"],
    )


__all__ = [
    "EvidenceEmbedding",
    "EvidenceEmbeddingRefreshCounts",
    "delete_stale_evidence_embeddings",
    "evidence_text_hash",
    "list_evidence_embeddings",
    "refresh_evidence_embeddings",
]
