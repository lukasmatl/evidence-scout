"""Trustworthy mapping from explicit Git changes to the resolved graph."""

from __future__ import annotations

import difflib
import hashlib
import os
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from code_scout.domain.normalize import NormalizedGraphFacts
from code_scout.domain.resolver import resolve_active_facts
from code_scout.impact.git import (
    ChangedFile,
    ChangedLineRange,
    GitChangeSelection,
    GitComparison,
    GitRepositoryIdentity,
    GitRevisionIdentity,
)
from code_scout.models.graph import Evidence, File, Symbol
from code_scout.storage import database_path, list_active_facts
from code_scout.workspace.status import (
    WorkspaceStatusReport,
    workspace_status,
)


class MappingSide(StrEnum):
    """The Git comparison side whose exact content supports a mapping."""

    OLD = "old"
    NEW = "new"


class MappingStatus(StrEnum):
    """Whether a changed file has a trustworthy symbol mapping."""

    MAPPED = "mapped"
    FILE_ONLY = "file_only"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ChangedSourceRange:
    side: MappingSide
    start_line: int
    line_count: int
    end_line: int | None


@dataclass(frozen=True)
class CoverageGap:
    code: str
    message: str
    repository_id: str
    path: str


@dataclass(frozen=True)
class ChangedFileSeed:
    repository: GitRepositoryIdentity
    old_path: str | None
    new_path: str | None
    change_kind: str
    binary: bool
    old_ranges: tuple[ChangedSourceRange, ...]
    new_ranges: tuple[ChangedSourceRange, ...]
    mapping_status: MappingStatus
    mapping_side: MappingSide | None
    mapping_reason: str | None
    file_id: str | None
    indexed_path: str | None
    indexed_content_hash: str | None
    provenance: tuple[Evidence, ...] = ()
    # Checkpoint comparisons retain these identities even when neither side was
    # ever a Git object.  Git callers leave them unset for compatibility.
    old_content_sha256: str | None = None
    new_content_sha256: str | None = None


@dataclass(frozen=True)
class ChangedSymbolSeed:
    repository: GitRepositoryIdentity
    path: str
    file_id: str
    symbol_id: str
    name: str
    kind: str
    qualified_name: str | None
    start_line: int
    end_line: int
    changed_ranges: tuple[ChangedSourceRange, ...]
    mapping_status: MappingStatus
    mapping_side: MappingSide
    provenance: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class ChangeSeedResult:
    repository: GitRepositoryIdentity
    comparison: GitComparison
    index_state: str
    freshness: dict[str, Any]
    files: tuple[ChangedFileSeed, ...]
    symbols: tuple[ChangedSymbolSeed, ...]
    coverage_gaps: tuple[CoverageGap, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _ExactIndexedSide:
    side: MappingSide
    file: File
    ranges: tuple[ChangedLineRange, ...]


def map_changed_ranges_to_symbols(
    workspace: str | Path,
    selection: GitChangeSelection,
    status: WorkspaceStatusReport | None = None,
) -> ChangeSeedResult:
    """Map Git ranges only where persisted file content exactly matches a blob."""
    root = Path(workspace).expanduser().resolve()
    status_report = status or workspace_status(root)
    graph = load_resolved_graph_read_only(root, status_report)
    files_by_path = (
        {(file.repository_id, file.path): file for file in graph.files}
        if graph is not None
        else {}
    )
    symbols_by_file: dict[str, list[Symbol]] = {}
    if graph is not None:
        for symbol in graph.symbols:
            symbols_by_file.setdefault(symbol.file_id, []).append(symbol)

    blob_hashes: dict[str, str | None] = {}
    file_seeds: list[ChangedFileSeed] = []
    symbol_seeds: list[ChangedSymbolSeed] = []
    gaps: list[CoverageGap] = []

    for changed in selection.files:
        exact, reason = _exact_indexed_side(
            changed,
            selection,
            files_by_path,
            blob_hashes,
        )
        path = changed.new_path or changed.old_path or ""
        old_ranges = _source_ranges(MappingSide.OLD, changed.old_ranges)
        new_ranges = _source_ranges(MappingSide.NEW, changed.new_ranges)
        mapping_status = MappingStatus.UNRESOLVED
        file_reason = reason
        mapped_symbols: tuple[ChangedSymbolSeed, ...] = ()

        if graph is None:
            file_reason = "index_unavailable"
        elif exact is not None:
            path = exact.file.path
            mapping_status = MappingStatus.FILE_ONLY
            if changed.binary:
                file_reason = "binary_change"
            elif not exact.ranges:
                file_reason = "no_changed_text_ranges"
            else:
                candidates = tuple(symbols_by_file.get(exact.file.id, ()))
                ranged = tuple(
                    symbol for symbol in candidates if _has_usable_symbol_range(symbol)
                )
                missing_range_count = len(candidates) - len(ranged)
                if missing_range_count:
                    gaps.append(
                        _gap(
                            "missing_symbol_ranges",
                            (
                                f"{missing_range_count} indexed symbol(s) have no "
                                "usable source range."
                            ),
                            selection,
                            path,
                        )
                    )
                mapped_symbols = _map_symbols(
                    selection.repository,
                    exact,
                    ranged,
                )
                if mapped_symbols:
                    mapping_status = MappingStatus.MAPPED
                    file_reason = None
                else:
                    file_reason = _unmapped_exact_reason(exact.file, status_report)

        file_seed = ChangedFileSeed(
            repository=selection.repository,
            old_path=changed.old_path,
            new_path=changed.new_path,
            change_kind=changed.kind.value,
            binary=changed.binary,
            old_ranges=old_ranges,
            new_ranges=new_ranges,
            mapping_status=mapping_status,
            mapping_side=exact.side if exact is not None else None,
            mapping_reason=file_reason,
            file_id=exact.file.id if exact is not None else None,
            indexed_path=exact.file.path if exact is not None else None,
            indexed_content_hash=exact.file.content_hash if exact is not None else None,
            provenance=exact.file.evidence if exact is not None else (),
        )
        file_seeds.append(file_seed)
        symbol_seeds.extend(mapped_symbols)
        if file_reason is not None:
            gaps.append(
                _gap(file_reason, _reason_message(file_reason), selection, path)
            )

    return ChangeSeedResult(
        repository=selection.repository,
        comparison=selection.comparison,
        index_state=str(status_report.index.get("state", "unknown")),
        freshness=dict(status_report.freshness),
        files=tuple(file_seeds),
        symbols=tuple(symbol_seeds),
        coverage_gaps=_dedupe_gaps(gaps),
    )


def map_checkpoint_ranges_to_symbols(
    workspace: str | Path,
    *,
    repository: GitRepositoryIdentity,
    before: dict[str, tuple[str, bytes] | None],
    after: dict[str, tuple[str, bytes] | None],
    status: WorkspaceStatusReport | None = None,
) -> ChangeSeedResult:
    """Map retained checkpoint bytes without consulting Git objects.

    Values are ``(sha256, bytes)`` pairs (or ``None`` for a deleted/added side).
    The caller owns blob retrieval; this function is intentionally read-only and
    never scans, extracts, or invokes a model.
    """
    root = Path(workspace).expanduser().resolve()
    status_report = status or workspace_status(root)
    graph = load_resolved_graph_read_only(root, status_report)
    files_by_path = (
        {(file.repository_id, file.path): file for file in graph.files}
        if graph is not None
        else {}
    )
    # A member may have been scanned as the default one-repository workspace.
    # This fallback is safe only for that one persisted repository and is never
    # used to combine evidence from two members.
    graph_ids = {file.repository_id for file in graph.files} if graph else set()
    scan_id = (
        repository.id
        if repository.id in graph_ids
        else (next(iter(graph_ids)) if len(graph_ids) == 1 else repository.id)
    )
    symbols_by_file: dict[str, list[Symbol]] = {}
    if graph is not None:
        for symbol in graph.symbols:
            symbols_by_file.setdefault(symbol.file_id, []).append(symbol)
    files: list[ChangedFileSeed] = []
    symbols: list[ChangedSymbolSeed] = []
    gaps: list[CoverageGap] = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        old_hash, old_bytes = old if old is not None else (None, None)
        new_hash, new_bytes = new if new is not None else (None, None)
        binary = _checkpoint_binary(old_bytes, new_bytes)
        old_ranges, new_ranges = _checkpoint_ranges(old_bytes, new_bytes, binary)
        candidate_sides = (
            (MappingSide.NEW, new_hash, new_ranges),
            (MappingSide.OLD, old_hash, old_ranges),
        )
        exact: _ExactIndexedSide | None = None
        reason = "index_unavailable" if graph is None else "unindexed_file"
        indexed = files_by_path.get((scan_id, path)) if graph is not None else None
        if graph is not None and indexed is not None:
            if not indexed.content_hash:
                reason = "missing_index_hash"
            else:
                for side, content_hash, ranges in candidate_sides:
                    if content_hash == indexed.content_hash:
                        exact = _ExactIndexedSide(side, indexed, ranges)
                        reason = ""
                        break
                if exact is None and reason != "missing_index_hash":
                    reason = "indexed_content_mismatch"
        mapping_status = MappingStatus.UNRESOLVED
        mapped: tuple[ChangedSymbolSeed, ...] = ()
        if exact is not None:
            mapping_status = MappingStatus.FILE_ONLY
            if binary:
                reason = "binary_change"
            elif not exact.ranges:
                reason = "no_changed_text_ranges"
            else:
                candidates = tuple(
                    symbol
                    for symbol in symbols_by_file.get(exact.file.id, ())
                    if _has_usable_symbol_range(symbol)
                )
                mapped = _map_symbols(repository, exact, candidates)
                if mapped:
                    mapping_status, reason = MappingStatus.MAPPED, None
                else:
                    reason = _unmapped_exact_reason(exact.file, status_report)
        seed = ChangedFileSeed(
            repository=repository,
            old_path=path if old is not None else None,
            new_path=path if new is not None else None,
            change_kind="added"
            if old is None
            else "deleted"
            if new is None
            else "modified",
            binary=binary,
            old_ranges=_source_ranges(MappingSide.OLD, old_ranges),
            new_ranges=_source_ranges(MappingSide.NEW, new_ranges),
            mapping_status=mapping_status,
            mapping_side=exact.side if exact else None,
            mapping_reason=reason,
            file_id=exact.file.id if exact else None,
            indexed_path=exact.file.path if exact else None,
            indexed_content_hash=exact.file.content_hash if exact else None,
            provenance=exact.file.evidence if exact else (),
            old_content_sha256=old_hash,
            new_content_sha256=new_hash,
        )
        files.append(seed)
        symbols.extend(mapped)
        if reason:
            gaps.append(
                _gap(
                    reason,
                    _reason_message(reason),
                    _checkpoint_selection(repository),
                    path,
                )
            )
    return ChangeSeedResult(
        repository,
        _checkpoint_selection(repository).comparison,
        str(status_report.index.get("state", "unknown")),
        dict(status_report.freshness),
        tuple(files),
        tuple(symbols),
        _dedupe_gaps(gaps),
    )


def _checkpoint_selection(repository: GitRepositoryIdentity) -> GitChangeSelection:
    return GitChangeSelection(
        repository,
        GitComparison(
            "checkpoint",
            GitRevisionIdentity("checkpoint-before", "checkpoint", None),
            GitRevisionIdentity("checkpoint-after", "checkpoint", None),
        ),
        (),
    )


def _checkpoint_binary(old: bytes | None, new: bytes | None) -> bool:
    return any(value is not None and b"\0" in value for value in (old, new))


def _checkpoint_ranges(
    old: bytes | None, new: bytes | None, binary: bool
) -> tuple[tuple[ChangedLineRange, ...], tuple[ChangedLineRange, ...]]:
    if binary:
        return (), ()
    try:
        old_lines = (old or b"").decode("utf-8").splitlines()
        new_lines = (new or b"").decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return (), ()
    old_ranges: list[ChangedLineRange] = []
    new_ranges: list[ChangedLineRange] = []
    for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(
        None, old_lines, new_lines
    ).get_opcodes():
        if tag == "equal":
            continue
        old_ranges.append(ChangedLineRange(a0 + 1, a1 - a0))
        new_ranges.append(ChangedLineRange(b0 + 1, b1 - b0))
    return tuple(old_ranges), tuple(new_ranges)


def load_resolved_graph_read_only(
    workspace: Path,
    status: WorkspaceStatusReport,
) -> NormalizedGraphFacts | None:
    """Load resolved active facts without creating or migrating workspace state."""
    if status.index.get("state") != "ready":
        return None
    path = database_path(workspace)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return resolve_active_facts(list_active_facts(connection))
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _exact_indexed_side(
    changed: ChangedFile,
    selection: GitChangeSelection,
    files_by_path: dict[tuple[str, str], File],
    blob_hashes: dict[str, str | None],
) -> tuple[_ExactIndexedSide | None, str]:
    candidates = (
        (MappingSide.NEW, changed.new_path, changed.new_blob, changed.new_ranges),
        (MappingSide.OLD, changed.old_path, changed.old_blob, changed.old_ranges),
    )
    indexed_files: list[File] = []
    blob_unavailable = False
    for side, path, blob, ranges in candidates:
        if path is None or blob is None:
            continue
        indexed = files_by_path.get((selection.repository.id, path))
        if indexed is None:
            continue
        indexed_files.append(indexed)
        if indexed.content_hash is None:
            continue
        if blob not in blob_hashes:
            blob_hashes[blob] = _git_blob_sha256(selection.repository.root, blob)
        blob_hash = blob_hashes[blob]
        if blob_hash is None:
            blob_unavailable = True
        elif indexed.content_hash == blob_hash:
            return _ExactIndexedSide(side=side, file=indexed, ranges=ranges), ""

    if not indexed_files:
        return None, "unindexed_file"
    if all(not file.content_hash for file in indexed_files):
        return None, "missing_index_hash"
    if blob_unavailable:
        return None, "comparison_blob_unavailable"
    return None, "indexed_content_mismatch"


def _git_blob_sha256(repository: str, blob: str) -> str | None:
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", blob],
            cwd=repository,
            env=environment,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def _source_ranges(
    side: MappingSide,
    ranges: tuple[ChangedLineRange, ...],
) -> tuple[ChangedSourceRange, ...]:
    return tuple(
        ChangedSourceRange(
            side=side,
            start_line=item.start_line,
            line_count=item.line_count,
            end_line=item.end_line,
        )
        for item in ranges
    )


def _map_symbols(
    repository: GitRepositoryIdentity,
    exact: _ExactIndexedSide,
    symbols: tuple[Symbol, ...],
) -> tuple[ChangedSymbolSeed, ...]:
    ordered = sorted(
        symbols,
        # Nested/overlapping symbols are all retained, most specific first.
        key=lambda symbol: (
            (symbol.end_line or 0) - (symbol.start_line or 0),
            symbol.start_line or 0,
            symbol.end_line or 0,
            symbol.id,
        ),
    )
    mapped: list[ChangedSymbolSeed] = []
    for symbol in ordered:
        assert symbol.start_line is not None and symbol.end_line is not None
        overlaps = tuple(
            changed_range
            for changed_range in exact.ranges
            if _ranges_overlap(symbol.start_line, symbol.end_line, changed_range)
        )
        if not overlaps:
            continue
        mapped.append(
            ChangedSymbolSeed(
                repository=repository,
                path=exact.file.path,
                file_id=exact.file.id,
                symbol_id=symbol.id,
                name=symbol.name,
                kind=symbol.kind,
                qualified_name=symbol.qualified_name,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                changed_ranges=_source_ranges(exact.side, overlaps),
                mapping_status=MappingStatus.MAPPED,
                mapping_side=exact.side,
                provenance=symbol.evidence,
            )
        )
    return tuple(mapped)


def _has_usable_symbol_range(symbol: Symbol) -> bool:
    return (
        symbol.start_line is not None
        and symbol.end_line is not None
        and symbol.start_line >= 1
        and symbol.end_line >= symbol.start_line
    )


def _ranges_overlap(
    symbol_start: int,
    symbol_end: int,
    changed: ChangedLineRange,
) -> bool:
    if changed.line_count == 0:
        anchor = max(1, changed.start_line)
        return symbol_start <= anchor <= symbol_end
    assert changed.end_line is not None
    return symbol_start <= changed.end_line and changed.start_line <= symbol_end


def _unmapped_exact_reason(file: File, status: WorkspaceStatusReport) -> str:
    if file.language is None:
        return "unsupported_language"
    grammar = next(
        (
            item
            for item in status.coverage.get("grammar_support", ())
            if item.get("language") == file.language
        ),
        None,
    )
    if grammar is not None and grammar.get("state") in {"disabled", "unavailable"}:
        return "symbol_extraction_unavailable"
    extractor_states = {
        item.get("name"): item.get("state")
        for item in status.coverage.get("extractors", {}).get("states", ())
    }
    if extractor_states.get("symbol_extraction") in {"disabled", "unavailable"}:
        return "symbol_extraction_unavailable"
    return "no_overlapping_symbols"


def _gap(
    code: str,
    message: str,
    selection: GitChangeSelection,
    path: str,
) -> CoverageGap:
    return CoverageGap(
        code=code,
        message=message,
        repository_id=selection.repository.id,
        path=path,
    )


def _reason_message(reason: str) -> str:
    return {
        "index_unavailable": (
            "The resolved index is unavailable; no symbol mapping was attempted."
        ),
        "unindexed_file": "Neither comparison path is present in the resolved index.",
        "missing_index_hash": (
            "The indexed file has no content hash for exact comparison."
        ),
        "comparison_blob_unavailable": (
            "The selected Git blob could not be read for exact comparison."
        ),
        "indexed_content_mismatch": (
            "Indexed content matches neither selected Git comparison side."
        ),
        "binary_change": (
            "Binary changes remain file-level evidence and have no line mapping."
        ),
        "no_changed_text_ranges": (
            "The file change has no text ranges to map to symbols."
        ),
        "unsupported_language": "The indexed file has no supported language mapping.",
        "symbol_extraction_unavailable": (
            "Symbol extraction is unavailable for the indexed file."
        ),
        "no_overlapping_symbols": (
            "No ranged indexed symbol overlaps the exact changed ranges."
        ),
    }[reason]


def _dedupe_gaps(gaps: list[CoverageGap]) -> tuple[CoverageGap, ...]:
    return tuple(
        {
            (gap.code, gap.repository_id, gap.path, gap.message): gap for gap in gaps
        }.values()
    )


__all__ = [
    "ChangeSeedResult",
    "ChangedFileSeed",
    "ChangedSourceRange",
    "ChangedSymbolSeed",
    "CoverageGap",
    "MappingSide",
    "MappingStatus",
    "map_changed_ranges_to_symbols",
    "map_checkpoint_ranges_to_symbols",
]
