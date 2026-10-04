from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from code_scout.core.errors import CodeScoutError
from code_scout.engines.contracts import (
    Citation,
    CodeScoutResult,
    EngineWarning,
    SourceAssessment,
)
from code_scout.storage.schema import database_path


class EvidenceRequiredError(CodeScoutError):
    """An engine requiring disk-backed evidence returned no valid citations."""

    def __init__(
        self,
        message: str,
        hint: str,
        result: CodeScoutResult,
    ) -> None:
        self.result = result
        super().__init__(message, hint)


@dataclass(frozen=True)
class CitationValidation:
    """Ordered valid citations and diagnostics for rejected provider locations."""

    citations: tuple[Citation, ...]
    warnings: tuple[EngineWarning, ...]

    @property
    def verified(self) -> bool:
        return bool(self.citations)


@dataclass(frozen=True)
class ObservedSource:
    """A complete source file returned by a trusted request-scoped tool call."""

    path: str
    content: str


def validate_citations(
    workspace: Path,
    citations: Iterable[Citation],
) -> CitationValidation:
    """Normalize and check provider citations against one selected workspace."""

    workspace = Path(workspace).resolve()
    valid: list[Citation] = []
    warnings: list[EngineWarning] = []
    seen: set[Citation] = set()

    for citation in citations:
        normalized, warning = _validate_citation(workspace, citation)
        if warning is not None:
            warnings.append(warning)
        elif normalized not in seen:
            seen.add(normalized)
            valid.append(normalized)

    return CitationValidation(tuple(valid), tuple(warnings))


def validate_result_citations(
    result: CodeScoutResult,
    workspace: Path,
    *,
    require_evidence: bool = False,
) -> CodeScoutResult:
    """Return a result containing only normalized, disk-backed citations."""

    validation = validate_citations(workspace, result.citations)
    validated_result = replace(
        result,
        citations=validation.citations,
        warnings=(*result.warnings, *validation.warnings),
    )
    if require_evidence and not validated_result.verified:
        raise EvidenceRequiredError(
            f"Engine '{result.engine.name}' returned no valid citations.",
            "Try a different query or an engine that does not require evidence.",
            validated_result,
        )
    return validated_result


def bind_citations_to_index(
    workspace: Path,
    citations: Iterable[Citation],
    *,
    observed_sources: Iterable[ObservedSource] = (),
) -> tuple[Citation, ...]:
    """Attach a revision only from an explicit identity or tool observation.

    A path and range are locations, not source identities.  In particular, they
    must not allow a current request to borrow a matching historical index row.
    """
    database = database_path(workspace)
    if not database.is_file():
        return tuple(citations)
    observed_digests = {
        (source.path, hashlib.sha256(source.content.encode()).hexdigest())
        for source in observed_sources
    }
    bound: list[Citation] = []
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        for citation in citations:
            if any(
                (
                    citation.repository_id,
                    citation.source_revision_id,
                    citation.content_sha256,
                )
            ):
                bound.append(citation)
                continue
            # A source must have been observed by this request.  Looking up a
            # range in raw evidence is deliberately insufficient: its revision
            # may predate the provider's live-file observation.
            matching_digests = [
                digest for path, digest in observed_digests if path == citation.path
            ]
            if not matching_digests:
                bound.append(citation)
                continue
            placeholders = ", ".join("?" for _ in matching_digests)
            rows = connection.execute(
                f"""
                SELECT repository_id, id, content_sha256 FROM source_revisions
                WHERE path = ? AND content_sha256 IN ({placeholders})
                ORDER BY id
                """,
                (citation.path, *matching_digests),
            ).fetchall()
            identities = {
                (repository_id, revision_id, content_sha256)
                for repository_id, revision_id, content_sha256 in rows
            }
            bound.append(
                replace(
                    citation,
                    repository_id=identity[0],
                    source_revision_id=identity[1],
                    content_sha256=identity[2],
                )
                if len(identities) == 1 and (identity := next(iter(identities)))
                else citation
            )
    except (sqlite3.Error, OSError, ValueError, TypeError, json.JSONDecodeError):
        return tuple(citations)
    finally:
        if "connection" in locals():
            connection.close()
    return tuple(bound)


def _validate_citation(
    workspace: Path,
    citation: Citation,
) -> tuple[Citation | None, EngineWarning | None]:
    display = _display(citation)
    raw_path = citation.path.strip()
    parsed = PurePosixPath(raw_path)

    if not raw_path or parsed == PurePosixPath("."):
        return None, _warning("citation-invalid-path", display, "path is empty")
    if ".." in parsed.parts:
        return None, _warning(
            "citation-path-traversal", display, "path contains '..' traversal"
        )
    if citation.start_line < 1:
        return None, _warning(
            "citation-invalid-range", display, "start line must be at least 1"
        )
    if citation.end_line is None or citation.end_line < citation.start_line:
        return None, _warning(
            "citation-invalid-range", display, "end line precedes start line"
        )

    candidate = Path(raw_path)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(workspace)
        except ValueError:
            return None, _warning(
                "citation-outside-workspace", display, "path is outside the workspace"
            )
    else:
        relative = Path(*parsed.parts)
        candidate = workspace / relative

    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return None, _warning(
            "citation-symlink-escape",
            display,
            "path resolves outside the workspace",
        )

    retained, retained_warning = _retained_source(workspace, citation, relative)
    if retained_warning is not None:
        return None, retained_warning

    live_bytes, live_status = _live_source_bytes(candidate)
    if retained is None and live_status == "missing":
        return None, _warning("citation-missing-file", display, "file does not exist")
    if retained is None and live_status == "not-file":
        return None, _warning("citation-not-file", display, "path is not a file")
    if retained is None and live_status == "unreadable":
        return None, _warning(
            "citation-unreadable-file", display, "file cannot be read"
        )

    source_bytes = retained if retained is not None else live_bytes
    assert source_bytes is not None
    line_count = _line_count(source_bytes)
    if citation.end_line > line_count:
        return None, _warning(
            "citation-range-out-of-bounds",
            display,
            f"file has {line_count} lines",
        )

    assessment = _source_assessment(retained, live_bytes, live_status)
    normalized = replace(
        citation, path=relative.as_posix(), source_assessment=assessment
    )
    return normalized, None


def _retained_source(
    workspace: Path, citation: Citation, relative: Path
) -> tuple[bytes | None, EngineWarning | None]:
    """Read and authenticate the immutable source revision when fully bound."""
    fields = (
        citation.repository_id,
        citation.source_revision_id,
        citation.content_sha256,
    )
    if not any(fields):
        return None, None
    if not all(isinstance(value, str) and value for value in fields):
        # Partial source metadata is not a trustworthy binding, but preserves
        # legacy location validation and reports revision-unknown below.
        return None, None

    path = database_path(workspace)
    if not path.is_file():
        return None, _warning(
            "citation-retained-source-missing",
            _display(citation),
            "retained source revision is unavailable",
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = connection.execute(
            """
            SELECT revisions.repository_id, revisions.path, revisions.content_sha256,
                   blobs.bytes
            FROM source_revisions AS revisions
            JOIN source_blobs AS blobs
              ON blobs.content_sha256 = revisions.content_sha256
            WHERE revisions.id = ?
            """,
            (citation.source_revision_id,),
        ).fetchone()
        connection.close()
    except sqlite3.Error:
        return None, _warning(
            "citation-retained-source-unreadable",
            _display(citation),
            "retained source revision cannot be read",
        )
    if row is None:
        return None, _warning(
            "citation-retained-source-missing",
            _display(citation),
            "retained source revision is unavailable",
        )

    repository_id, path_value, digest, source_bytes = row
    if (
        repository_id != citation.repository_id
        or path_value != relative.as_posix()
        or digest != citation.content_sha256
    ):
        return None, _warning(
            "citation-source-binding-mismatch",
            _display(citation),
            "retained source revision does not match citation identity",
        )
    source_bytes = bytes(source_bytes)
    if hashlib.sha256(source_bytes).hexdigest() != digest:
        return None, _warning(
            "citation-retained-source-corrupt",
            _display(citation),
            "retained source bytes do not match content digest",
        )
    return source_bytes, None


def _live_source_bytes(candidate: Path) -> tuple[bytes | None, str]:
    if not candidate.exists():
        return None, "missing"
    if not candidate.is_file():
        return None, "not-file"
    try:
        return candidate.read_bytes(), "available"
    except OSError:
        return None, "unreadable"


def _line_count(source_bytes: bytes) -> int:
    return len(source_bytes.splitlines())


def _source_assessment(
    retained: bytes | None, live_bytes: bytes | None, live_status: str
) -> SourceAssessment:
    if retained is None:
        return SourceAssessment("unknown", "unknown", ("source-unbound",))
    if live_status == "available":
        freshness = "fresh" if live_bytes == retained else "stale"
        reason = "source-current" if freshness == "fresh" else "source-changed"
        return SourceAssessment("verified", freshness, (reason,))
    if live_status == "missing":
        return SourceAssessment("verified", "stale", ("source-missing",))
    if live_status == "unreadable":
        return SourceAssessment("verified", "unknown", ("source-unreadable",))
    return SourceAssessment("verified", "unknown", ("source-not-file",))


def _display(citation: Citation) -> str:
    if citation.end_line == citation.start_line:
        return f"{citation.path}:{citation.start_line}"
    return f"{citation.path}:{citation.start_line}-{citation.end_line}"


def _warning(code: str, citation: str, reason: str) -> EngineWarning:
    return EngineWarning(code, f"Rejected citation '{citation}': {reason}.")
