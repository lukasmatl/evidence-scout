from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceBlob:
    content_sha256: str
    bytes: bytes


@dataclass(frozen=True)
class SourceRevision:
    id: str
    repository_id: str
    path: str
    content_sha256: str


def source_content_sha256(source_bytes: bytes) -> str:
    """Return the full SHA-256 for exact captured source bytes."""
    return hashlib.sha256(source_bytes).hexdigest()


def deterministic_source_revision_id(
    *,
    repository_id: str,
    path: str,
    content_sha256: str,
) -> str:
    canonical_path = _canonical_path(path)
    _validate_digest(content_sha256)
    if not repository_id:
        raise ValueError("Source revisions require a repository_id")
    return f"source-revision:{repository_id}:{canonical_path}:{content_sha256}"


def store_source_blob(
    connection: sqlite3.Connection,
    source_bytes: bytes,
    *,
    content_sha256: str | None = None,
) -> SourceBlob:
    """Store bytes once, rejecting a supplied or retained digest mismatch."""
    digest = content_sha256 or source_content_sha256(source_bytes)
    _validate_digest(digest)
    if source_content_sha256(source_bytes) != digest:
        raise ValueError("Source bytes do not match content_sha256")

    connection.execute(
        """
        INSERT INTO source_blobs (content_sha256, bytes, size_bytes)
        VALUES (?, ?, ?)
        ON CONFLICT(content_sha256) DO NOTHING
        """,
        (digest, source_bytes, len(source_bytes)),
    )
    blob = get_source_blob(connection, digest)
    if blob is None:  # pragma: no cover - defensive guard for broken connections
        raise RuntimeError("Stored source blob could not be read")
    if blob.bytes != source_bytes:
        raise ValueError("Existing source blob bytes do not match content_sha256")
    return blob


def store_source_revision(
    connection: sqlite3.Connection,
    *,
    repository_id: str,
    path: str,
    source_bytes: bytes,
    content_sha256: str | None = None,
    revision_id: str | None = None,
) -> SourceRevision:
    """Atomically retain source bytes and a repository-qualified revision."""
    digest = content_sha256 or source_content_sha256(source_bytes)
    canonical_path = _canonical_path(path)
    expected_id = deterministic_source_revision_id(
        repository_id=repository_id,
        path=canonical_path,
        content_sha256=digest,
    )
    if revision_id is not None and revision_id != expected_id:
        raise ValueError("Source revision id does not match its immutable identity")

    connection.execute("SAVEPOINT store_source_revision")
    try:
        store_source_blob(connection, source_bytes, content_sha256=digest)
        connection.execute(
            """
            INSERT INTO source_revisions (id, repository_id, path, content_sha256)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (expected_id, repository_id, canonical_path, digest),
        )
        revision = get_source_revision(connection, expected_id)
        if (
            revision is None
        ):  # pragma: no cover - defensive guard for broken connections
            raise RuntimeError("Stored source revision could not be read")
        if revision != SourceRevision(
            id=expected_id,
            repository_id=repository_id,
            path=canonical_path,
            content_sha256=digest,
        ):
            raise ValueError(
                "Existing source revision does not match immutable identity"
            )
        connection.execute("RELEASE SAVEPOINT store_source_revision")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT store_source_revision")
        connection.execute("RELEASE SAVEPOINT store_source_revision")
        raise
    return revision


def get_source_blob(
    connection: sqlite3.Connection,
    content_sha256: str,
) -> SourceBlob | None:
    _validate_digest(content_sha256)
    row = connection.execute(
        "SELECT content_sha256, bytes FROM source_blobs WHERE content_sha256 = ?",
        (content_sha256,),
    ).fetchone()
    if row is None:
        return None
    source_bytes = bytes(row["bytes"])
    if source_content_sha256(source_bytes) != row["content_sha256"]:
        raise ValueError("Stored source blob failed content_sha256 integrity check")
    return SourceBlob(content_sha256=row["content_sha256"], bytes=source_bytes)


def get_source_revision(
    connection: sqlite3.Connection,
    revision_id: str,
) -> SourceRevision | None:
    row = connection.execute(
        """
        SELECT id, repository_id, path, content_sha256
        FROM source_revisions
        WHERE id = ?
        """,
        (revision_id,),
    ).fetchone()
    if row is None:
        return None
    return SourceRevision(
        id=row["id"],
        repository_id=row["repository_id"],
        path=row["path"],
        content_sha256=row["content_sha256"],
    )


def list_source_revisions(
    connection: sqlite3.Connection,
    *,
    repository_id: str | None = None,
    path: str | None = None,
) -> tuple[SourceRevision, ...]:
    query = "SELECT id, repository_id, path, content_sha256 FROM source_revisions"
    values: list[str] = []
    clauses: list[str] = []
    if repository_id is not None:
        clauses.append("repository_id = ?")
        values.append(repository_id)
    if path is not None:
        clauses.append("path = ?")
        values.append(_canonical_path(path))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY repository_id, path, content_sha256, id"
    rows = connection.execute(query, values).fetchall()
    return tuple(
        SourceRevision(
            id=row["id"],
            repository_id=row["repository_id"],
            path=row["path"],
            content_sha256=row["content_sha256"],
        )
        for row in rows
    )


def _canonical_path(path: str) -> str:
    canonical_path = path.replace("\\", "/").removeprefix("./")
    if not canonical_path:
        raise ValueError("Source revisions require a non-empty path")
    return canonical_path


def _validate_digest(content_sha256: str) -> None:
    if len(content_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in content_sha256
    ):
        raise ValueError("content_sha256 must be a full lowercase SHA-256 digest")
