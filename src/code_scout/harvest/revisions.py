"""Bind harvested observations to bytes seen during the harvest window."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.storage import store_source_revision
from code_scout.storage.source_revisions import source_content_sha256

_EXTERNAL_EXTRACTORS = frozenset({"graphify", "lsp"})


@dataclass(frozen=True)
class SourceCapture:
    repository_id: str
    path: str
    absolute_path: str
    source_bytes: bytes | None = None
    unavailable_reason: str | None = None

    @property
    def content_sha256(self) -> str | None:
        if self.source_bytes is None:
            return None
        return source_content_sha256(self.source_bytes)


def capture_sources_before_harvest(
    discovered_files: Iterable[DiscoveredFile], *, max_bytes: int
) -> tuple[SourceCapture, ...]:
    """Read eligible discovered local text files before extractors run."""
    return tuple(
        _capture_source(file, max_bytes=max_bytes) for file in discovered_files
    )


def bind_harvested_revisions(
    connection,
    facts: Iterable[ActiveFact],
    *,
    before: Iterable[SourceCapture],
    max_bytes: int,
) -> tuple[ActiveFact, ...]:
    """Persist stable captures and project their identities onto fact evidence.

    A second read is deliberately compared with the pre-extraction capture.  This
    is evidence that in-process extractors had a stable local input window, not
    proof that an external provider read either version.
    """
    captures = {(item.repository_id, item.path): item for item in before}
    after = {
        key: _capture_source_from_capture(capture, max_bytes=max_bytes)
        for key, capture in captures.items()
    }
    revisions = {}
    for key, captured in captures.items():
        observed_after = after[key]
        if captured.source_bytes is None:
            continue
        if observed_after.source_bytes is None:
            continue
        if captured.content_sha256 != observed_after.content_sha256:
            continue
        revision = store_source_revision(
            connection,
            repository_id=captured.repository_id,
            path=captured.path,
            source_bytes=captured.source_bytes,
        )
        revisions[key] = revision

    return tuple(
        replace(
            fact,
            evidence=tuple(
                _bind_evidence(evidence, captures, after, revisions)
                for evidence in fact.evidence
            ),
        )
        for fact in facts
    )


def _bind_evidence(evidence, captures, after, revisions) -> FactEvidence:
    metadata = dict(evidence.metadata)
    repository_id = metadata.get("repository_id")
    path = _canonical_path(evidence.file_path)
    if not isinstance(repository_id, str):
        matches = [key for key in captures if key[1] == path]
        repository_id = matches[0][0] if len(matches) == 1 else None
    key = (repository_id, path) if isinstance(repository_id, str) else None
    if evidence.extractor_name in _EXTERNAL_EXTRACTORS:
        return replace(
            evidence,
            metadata=_unavailable_metadata(
                metadata, repository_id, "external_provider_input_unbound"
            ),
        )
    if key is None or key not in captures:
        return replace(
            evidence,
            metadata=_unavailable_metadata(metadata, repository_id, "external_source"),
        )
    capture = captures[key]
    observed_after = after[key]
    if capture.unavailable_reason is not None:
        return replace(
            evidence,
            metadata=_unavailable_metadata(
                metadata, repository_id, capture.unavailable_reason
            ),
        )
    if observed_after.unavailable_reason is not None:
        return replace(
            evidence,
            metadata=_unavailable_metadata(
                metadata, repository_id, observed_after.unavailable_reason
            ),
        )
    if capture.content_sha256 != observed_after.content_sha256:
        return replace(
            evidence,
            metadata=_unavailable_metadata(
                metadata, repository_id, "source_changed_during_extraction"
            ),
        )
    revision = revisions[key]
    metadata.update(
        {
            "repository_id": revision.repository_id,
            "source_revision_id": revision.id,
            "content_sha256": revision.content_sha256,
        }
    )
    metadata.pop("source_revision_unavailable_reason", None)
    return replace(evidence, metadata=metadata)


def _unavailable_metadata(metadata, repository_id: str | None, reason: str) -> dict:
    updated = dict(metadata)
    if repository_id is not None:
        updated["repository_id"] = repository_id
    updated.pop("source_revision_id", None)
    updated.pop("content_sha256", None)
    updated["source_revision_unavailable_reason"] = reason
    return updated


def _capture_source(file: DiscoveredFile, *, max_bytes: int) -> SourceCapture:
    return _read_capture(
        repository_id=file.repository_id,
        path=file.path,
        absolute_path=file.absolute_path,
        max_bytes=max_bytes,
    )


def _capture_source_from_capture(
    capture: SourceCapture, *, max_bytes: int
) -> SourceCapture:
    return _read_capture(
        repository_id=capture.repository_id,
        path=capture.path,
        absolute_path=capture.absolute_path,
        max_bytes=max_bytes,
    )


def _read_capture(
    *, repository_id: str, path: str, absolute_path: str, max_bytes: int
) -> SourceCapture:
    try:
        size = Path(absolute_path).stat().st_size
        if size > max_bytes:
            return SourceCapture(
                repository_id,
                path,
                absolute_path,
                unavailable_reason="source_too_large",
            )
        source_bytes = Path(absolute_path).read_bytes()
    except OSError:
        return SourceCapture(
            repository_id, path, absolute_path, unavailable_reason="source_unreadable"
        )
    if b"\0" in source_bytes:
        return SourceCapture(
            repository_id, path, absolute_path, unavailable_reason="source_binary"
        )
    try:
        source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return SourceCapture(
            repository_id, path, absolute_path, unavailable_reason="source_binary"
        )
    return SourceCapture(repository_id, path, absolute_path, source_bytes=source_bytes)


def _canonical_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./")
