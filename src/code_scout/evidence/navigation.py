from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from code_scout.evidence.pack import (
    EvidencePack,
    PackedRelationship,
    PackedSourceProvenance,
    RepositoryMapEntry,
)

NavigationOperation = Literal[
    "definitions", "implementations", "references", "callers", "callees", "overview"
]
NavigationStatus = Literal["confirmed", "inferred", "ambiguous", "unavailable"]

__all__ = [
    "CodeScoutSymbolNavigator",
    "NavigationCandidate",
    "NavigationOperation",
    "NavigationResult",
    "NavigationStatus",
    "SymbolNavigator",
]


@dataclass(frozen=True)
class NavigationCandidate:
    id: str
    symbol_id: str
    name: str
    kind: str
    path: str
    start_line: int
    end_line: int
    extractor: str
    confidence: float
    confidence_class: NavigationStatus
    provenance: tuple[PackedSourceProvenance, ...]
    relationship_id: str | None = None
    source_id: str | None = None
    relation: str | None = None
    target_id: str | None = None
    neighbor_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["provenance"] = {
            "extractors": _ordered_extractors(self.provenance),
            "sources": [
                {
                    key: value
                    for key, value in asdict(source).items()
                    if value is not None
                }
                for source in self.provenance
            ],
        }
        if self.relationship_id is not None:
            result["id"] = self.relationship_id
            result["source"] = {"type": "symbol", "id": self.source_id}
            result["target"] = {"type": "symbol", "id": self.target_id}
            result["hop"] = 1
        return result


@dataclass(frozen=True)
class NavigationResult:
    operation: NavigationOperation
    requested_symbol: str
    status: NavigationStatus
    candidates: tuple[NavigationCandidate, ...]
    coverage: dict[str, str]
    warnings: tuple[dict[str, str], ...] = ()
    provider: str = "code-scout"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "requested_symbol": self.requested_symbol,
            "status": self.status,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "coverage": dict(self.coverage),
            "warnings": list(self.warnings),
            "provider": self.provider,
        }


class SymbolNavigator(Protocol):
    """Provider-neutral, read-only symbol navigation used by verify tools."""

    def definitions(self, symbol: str) -> NavigationResult: ...

    def implementations(self, symbol: str) -> NavigationResult: ...

    def references(self, symbol: str) -> NavigationResult: ...

    def callers(self, symbol: str) -> NavigationResult: ...

    def callees(self, symbol: str) -> NavigationResult: ...

    def overview(self, path: str) -> NavigationResult: ...


class CodeScoutSymbolNavigator:
    """Navigate only graph and extractor facts already frozen into an evidence pack."""

    def __init__(self, pack: EvidencePack) -> None:
        self._pack = pack

    def definitions(self, symbol: str) -> NavigationResult:
        entries = _symbol_entries(self._pack.repository_map, symbol)
        candidates: list[NavigationCandidate] = []
        missing_ranges: list[str] = []
        for entry in entries:
            sources = _entry_sources(self._pack, entry)
            candidate = _definition_candidate(entry, sources)
            if candidate is None:
                missing_ranges.append(entry.id)
            else:
                candidates.append(candidate)
        return self._result(
            "definitions",
            symbol,
            candidates,
            ambiguous=len(entries) > 1,
            missing_ranges=missing_ranges,
        )

    def implementations(self, symbol: str) -> NavigationResult:
        return self._relationships(
            "implementations",
            symbol,
            relation=lambda value: "implement" in value.casefold(),
            direction="both",
        )

    def references(self, symbol: str) -> NavigationResult:
        return self._relationships(
            "references",
            symbol,
            relation=lambda value: value.casefold() != "declares_symbol",
            direction="both",
        )

    def callers(self, symbol: str) -> NavigationResult:
        return self._relationships(
            "callers",
            symbol,
            relation=lambda value: value.casefold() == "calls",
            direction="incoming",
        )

    def callees(self, symbol: str) -> NavigationResult:
        return self._relationships(
            "callees",
            symbol,
            relation=lambda value: value.casefold() == "calls",
            direction="outgoing",
        )

    def overview(self, path: str) -> NavigationResult:
        needle = path.strip().replace("\\", "/")
        if not needle:
            raise ValueError("path must not be empty")
        candidates: list[NavigationCandidate] = []
        missing_ranges: list[str] = []
        for entry in self._pack.repository_map:
            if entry.kind != "symbol" or entry.path != needle:
                continue
            candidate = _definition_candidate(entry, _entry_sources(self._pack, entry))
            if candidate is None:
                missing_ranges.append(entry.id)
            else:
                candidates.append(candidate)
        return self._result(
            "overview",
            needle,
            candidates,
            ambiguous=False,
            missing_ranges=missing_ranges,
        )

    def _relationships(
        self,
        operation: NavigationOperation,
        symbol: str,
        *,
        relation: Callable[[str], bool],
        direction: Literal["incoming", "outgoing", "both"],
    ) -> NavigationResult:
        entries = _symbol_entries(self._pack.repository_map, symbol)
        symbol_ids = {entry.id for entry in entries}
        candidates: list[NavigationCandidate] = []
        missing_ranges: list[str] = []
        for edge in self._pack.relationships:
            neighbor = _relationship_neighbor(edge, symbol_ids, direction)
            if neighbor is None or not relation(edge.relation_type):
                continue
            candidate = _relationship_candidate(self._pack, edge, neighbor)
            if candidate is None:
                missing_ranges.append(edge.id)
            else:
                candidates.append(candidate)
        return self._result(
            operation,
            symbol,
            candidates,
            ambiguous=len(entries) > 1,
            missing_ranges=missing_ranges,
        )

    def _result(
        self,
        operation: NavigationOperation,
        symbol: str,
        candidates: list[NavigationCandidate],
        *,
        ambiguous: bool,
        missing_ranges: list[str],
    ) -> NavigationResult:
        candidates.sort(key=_candidate_sort_key)
        extractors = {
            source.extractor.casefold().replace("-", "_")
            for candidate in candidates
            for source in candidate.provenance
        }
        warnings: list[dict[str, str]] = []
        if candidates and "lsp" not in extractors:
            warnings.append(
                {
                    "code": "language-server-evidence-unavailable",
                    "message": (
                        "No stored LSP evidence covers this result; existing graph "
                        "evidence was used without starting a language server."
                    ),
                }
            )
        if missing_ranges:
            warnings.append(
                {
                    "code": "navigation-range-unavailable",
                    "message": "Exact source ranges are unavailable for: "
                    + ", ".join(sorted(missing_ranges)),
                }
            )
        if not candidates:
            status: NavigationStatus = "unavailable"
        elif ambiguous:
            status = "ambiguous"
        elif any(item.confidence_class == "confirmed" for item in candidates):
            status = "confirmed"
        elif any(item.confidence_class == "inferred" for item in candidates):
            status = "inferred"
        elif any(item.confidence_class == "ambiguous" for item in candidates):
            status = "ambiguous"
        else:
            status = "unavailable"
        return NavigationResult(
            operation=operation,
            requested_symbol=symbol,
            status=status,
            candidates=tuple(candidates),
            coverage={
                provider: "available" if provider in extractors else "unavailable"
                for provider in ("lsp", "graphify", "tree_sitter", "lexical")
            },
            warnings=tuple(warnings),
        )


def _symbol_entries(
    entries: tuple[RepositoryMapEntry, ...], query: str
) -> tuple[RepositoryMapEntry, ...]:
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("symbol must not be empty")
    symbols = tuple(entry for entry in entries if entry.kind == "symbol")
    exact = tuple(
        entry
        for entry in symbols
        if needle in {entry.id.casefold(), entry.name.casefold()}
    )
    matches = exact or tuple(
        entry
        for entry in symbols
        if entry.name.casefold().endswith(f".{needle}")
        or needle in entry.name.casefold()
    )
    return tuple(sorted(matches, key=lambda entry: (entry.path or "", entry.id)))


def _entry_sources(
    pack: EvidencePack, entry: RepositoryMapEntry
) -> tuple[PackedSourceProvenance, ...]:
    sources = list(entry.source_provenance)
    for evidence in pack.evidence:
        if evidence.owner_type != "symbol" or evidence.owner_id != entry.id:
            continue
        sources.append(
            PackedSourceProvenance(
                source_file=evidence.source_file,
                start_line=evidence.start_line,
                end_line=evidence.end_line,
                extractor=evidence.provenance,
                confidence=evidence.confidence,
            )
        )
    return _dedupe_sources(sources)


def _definition_candidate(
    entry: RepositoryMapEntry,
    sources: tuple[PackedSourceProvenance, ...],
) -> NavigationCandidate | None:
    exact = tuple(
        source
        for source in sources
        if source.source_file == entry.path
        and source.start_line is not None
        and source.end_line is not None
    )
    if (
        entry.path is None
        or entry.start_line is None
        or entry.end_line is None
        or not exact
    ):
        return None
    primary = min(exact, key=_source_sort_key)
    return NavigationCandidate(
        id=entry.id,
        symbol_id=entry.id,
        name=entry.name,
        kind=entry.kind,
        path=primary.source_file,
        start_line=primary.start_line,
        end_line=primary.end_line,
        extractor=primary.extractor,
        confidence=primary.confidence or 0.0,
        confidence_class=_confidence_class(primary),
        provenance=tuple(sorted(exact, key=_source_sort_key)),
    )


def _relationship_candidate(
    pack: EvidencePack,
    edge: PackedRelationship,
    neighbor: str,
) -> NavigationCandidate | None:
    exact = tuple(
        source
        for source in edge.source_provenance
        if source.start_line is not None and source.end_line is not None
    )
    if not exact:
        return None
    primary = min(exact, key=_source_sort_key)
    entry = next(
        (item for item in pack.repository_map if item.id == neighbor),
        None,
    )
    return NavigationCandidate(
        id=edge.id,
        symbol_id=neighbor,
        name=entry.name if entry is not None else neighbor,
        kind=entry.kind if entry is not None else "symbol",
        path=primary.source_file,
        start_line=primary.start_line,
        end_line=primary.end_line,
        extractor=primary.extractor,
        confidence=(
            primary.confidence if primary.confidence is not None else edge.confidence
        ),
        confidence_class=_confidence_class(primary, edge.confidence),
        provenance=tuple(sorted(exact, key=_source_sort_key)),
        relationship_id=edge.id,
        source_id=edge.source_id,
        relation=edge.relation_type,
        target_id=edge.target_id,
        neighbor_id=neighbor,
    )


def _relationship_neighbor(
    edge: PackedRelationship,
    symbol_ids: set[str],
    direction: Literal["incoming", "outgoing", "both"],
) -> str | None:
    if direction in {"incoming", "both"} and edge.target_id in symbol_ids:
        return edge.source_id
    if direction in {"outgoing", "both"} and edge.source_id in symbol_ids:
        return edge.target_id
    return None


def _confidence_class(
    source: PackedSourceProvenance, fallback: float = 0.0
) -> NavigationStatus:
    extractor = source.extractor.casefold().replace("-", "_")
    if not extractor:
        return "unavailable"
    if extractor in {"lsp", "graphify"}:
        return "confirmed"
    confidence = source.confidence if source.confidence is not None else fallback
    if extractor == "tree_sitter" or confidence >= 0.5:
        return "inferred"
    return "ambiguous"


def _source_priority(source: PackedSourceProvenance) -> int:
    return {
        "lsp": 0,
        "graphify": 1,
        "tree_sitter": 2,
        "heuristic": 3,
        "lexical": 3,
    }.get(source.extractor.casefold().replace("-", "_"), 2)


def _source_sort_key(source: PackedSourceProvenance) -> tuple[Any, ...]:
    return (
        _source_priority(source),
        -(source.confidence or 0.0),
        source.source_file,
        source.start_line or 0,
        source.end_line or 0,
    )


def _candidate_sort_key(candidate: NavigationCandidate) -> tuple[Any, ...]:
    return (
        _source_priority(candidate.provenance[0]),
        -candidate.confidence,
        candidate.path,
        candidate.start_line,
        candidate.id,
    )


def _dedupe_sources(
    sources: list[PackedSourceProvenance],
) -> tuple[PackedSourceProvenance, ...]:
    unique = {
        (
            source.source_file,
            source.start_line,
            source.end_line,
            source.extractor,
            source.confidence,
        ): source
        for source in sources
    }
    return tuple(sorted(unique.values(), key=_source_sort_key))


def _ordered_extractors(sources: tuple[PackedSourceProvenance, ...]) -> list[str]:
    return list(dict.fromkeys(source.extractor for source in sources))
