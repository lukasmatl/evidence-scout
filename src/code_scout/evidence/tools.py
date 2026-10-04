from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.evidence.navigation import SymbolNavigator
from code_scout.evidence.pack import (
    EvidencePack,
    PackedEvidence,
    PackedRelationship,
    PackedSourceProvenance,
    RepositoryMapEntry,
)
from code_scout.evidence.serena import configured_symbol_navigator

RelationshipConfidence = Literal["confirmed", "inferred", "ambiguous", "unavailable"]
TraversalDirection = Literal["incoming", "outgoing", "both"]

__all__ = [
    "EvidenceToolLimits",
    "EvidenceTools",
    "RelationshipConfidence",
    "TraversalDirection",
]


@dataclass(frozen=True)
class EvidenceToolLimits:
    """Per-call limits for model-facing evidence tools."""

    max_matches: int = 20
    max_lines: int = 80
    max_characters: int = 4_000
    max_hops: int = 2

    def __post_init__(self) -> None:
        ceilings = {
            "max_matches": (self.max_matches, 100),
            "max_lines": (self.max_lines, 500),
            "max_characters": (self.max_characters, 20_000),
            "max_hops": (self.max_hops, 5),
        }
        for name, (value, ceiling) in ceilings.items():
            if not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be between 1 and {ceiling}")


class _CharacterBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.truncated = False

    def text(self, value: str) -> str:
        remaining = self.limit - self.used
        if len(value) > remaining:
            self.truncated = True
            value = value[: max(0, remaining)]
        self.used += len(value)
        return value


class EvidenceTools:
    """Read-only exploration primitives confined to one compiled evidence pack."""

    def __init__(
        self,
        workspace_path: str | Path,
        pack: EvidencePack,
        *,
        symbol_navigator: SymbolNavigator | None = None,
    ) -> None:
        self._workspace = Path(workspace_path).resolve()
        self._pack = pack
        self._symbol_navigator = symbol_navigator or configured_symbol_navigator(
            self._workspace, pack
        )

    def search_evidence(
        self,
        query: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        selected = limits or EvidenceToolLimits()
        terms = tuple(part.casefold() for part in query.split() if part.strip())
        if not terms:
            raise ValueError("query must not be empty")
        matches = [
            item
            for item in self._pack.evidence
            if all(term in _evidence_search_text(item) for term in terms)
        ]
        matches.sort(key=lambda item: (item.rank, item.id))
        budget = _CharacterBudget(selected.max_characters)
        items = [
            _evidence_result(item, budget) for item in matches[: selected.max_matches]
        ]
        return _result(
            tool="search_evidence",
            status="ok" if items else "unavailable",
            items=items,
            budget=budget,
            truncated=len(matches) > len(items),
            provenance={"scope": "compiled_evidence_pack", "query": self._pack.query},
        )

    def read_source_range(
        self,
        evidence_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        selected = limits or EvidenceToolLimits()
        citation = next(
            (item for item in self._pack.evidence if item.id == evidence_id), None
        )
        if citation is None or citation.start_line is None or citation.end_line is None:
            return _unavailable("read_source_range", evidence_id)

        first = citation.start_line if start_line is None else start_line
        last = citation.end_line if end_line is None else end_line
        if first < citation.start_line or last > citation.end_line or first > last:
            raise ValueError("requested range must stay inside the cited source range")
        requested_count = last - first + 1
        last = min(last, first + selected.max_lines - 1)
        path = self._workspace_file(citation.source_file)
        if path is None:
            return _unavailable("read_source_range", evidence_id)

        try:
            source_lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return _unavailable("read_source_range", evidence_id)
        if first > len(source_lines) or last > len(source_lines):
            return _unavailable("read_source_range", evidence_id)

        budget = _CharacterBudget(selected.max_characters)
        lines: list[dict[str, Any]] = []
        for number in range(first, last + 1):
            if budget.used >= budget.limit:
                budget.truncated = True
                break
            lines.append(
                {"line": number, "text": budget.text(source_lines[number - 1])}
            )
        return _result(
            tool="read_source_range",
            status="ok",
            items=lines,
            budget=budget,
            truncated=requested_count > len(lines),
            provenance=_evidence_provenance(citation),
        )

    def inspect_symbol(
        self,
        symbol: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        selected = limits or EvidenceToolLimits()
        candidates = _symbol_candidates(self._pack.repository_map, symbol)
        budget = _CharacterBudget(selected.max_characters)
        items = [
            self._symbol_result(entry, budget)
            for entry in candidates[: selected.max_matches]
        ]
        if not items:
            status = "unavailable"
        elif len(candidates) == 1:
            status = "confirmed"
        else:
            status = "ambiguous"
        return _result(
            tool="inspect_symbol",
            status=status,
            items=items,
            budget=budget,
            truncated=len(candidates) > len(items),
            provenance={"scope": "compiled_evidence_pack"},
        )

    def traverse_neighbors(
        self,
        node_id: str,
        *,
        direction: TraversalDirection = "both",
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be incoming, outgoing, or both")
        selected = limits or EvidenceToolLimits()
        return self._traverse(
            node_id,
            direction=direction,
            relation_types=None,
            limits=selected,
            tool="traverse_neighbors",
        )

    def list_callers(
        self,
        symbol_id: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        return self._navigation_result(
            "list_callers",
            self._symbol_navigator.callers(symbol_id),
            limits or EvidenceToolLimits(),
        )

    def list_callees(
        self,
        symbol_id: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        return self._navigation_result(
            "list_callees",
            self._symbol_navigator.callees(symbol_id),
            limits or EvidenceToolLimits(),
        )

    def find_definitions(
        self,
        symbol: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        return self._navigation_result(
            "find_definitions",
            self._symbol_navigator.definitions(symbol),
            limits or EvidenceToolLimits(),
        )

    def find_implementations(
        self,
        symbol: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        return self._navigation_result(
            "find_implementations",
            self._symbol_navigator.implementations(symbol),
            limits or EvidenceToolLimits(),
        )

    def find_references(
        self,
        symbol: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        return self._navigation_result(
            "find_references",
            self._symbol_navigator.references(symbol),
            limits or EvidenceToolLimits(),
        )

    def symbol_overview(
        self,
        path: str,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        return self._navigation_result(
            "symbol_overview",
            self._symbol_navigator.overview(path),
            limits or EvidenceToolLimits(),
        )

    def inspect_review_items(
        self,
        *,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        selected = limits or EvidenceToolLimits()
        budget = _CharacterBudget(selected.max_characters)
        source = self._pack.needs_review
        items = [
            _budget_json_value(item, budget) for item in source[: selected.max_matches]
        ]
        return _result(
            tool="inspect_review_items",
            status="ok" if items else "unavailable",
            items=items,
            budget=budget,
            truncated=len(source) > len(items),
            provenance={"scope": "compiled_evidence_pack"},
        )

    def inspect_impact_paths(
        self,
        *,
        node_id: str | None = None,
        limits: EvidenceToolLimits | None = None,
    ) -> dict[str, Any]:
        selected = limits or EvidenceToolLimits()
        paths = tuple(
            path
            for path in self._pack.impact_paths
            if node_id is None or node_id in path.node_ids
        )
        budget = _CharacterBudget(selected.max_characters)
        items = [
            _budget_json_value(asdict(path), budget)
            for path in paths[: selected.max_matches]
        ]
        return _result(
            tool="inspect_impact_paths",
            status="ok" if items else "unavailable",
            items=items,
            budget=budget,
            truncated=len(paths) > len(items),
            provenance={"scope": "compiled_evidence_pack", "provider": "dede"},
        )

    def _traverse(
        self,
        node_id: str,
        *,
        direction: TraversalDirection,
        relation_types: set[str] | None,
        limits: EvidenceToolLimits,
        tool: str,
    ) -> dict[str, Any]:
        relationships = tuple(
            relationship
            for relationship in self._pack.relationships
            if relation_types is None
            or relationship.relation_type.casefold() in relation_types
        )
        graph_nodes = {
            endpoint
            for relationship in relationships
            for endpoint in (relationship.source_id, relationship.target_id)
        }
        if node_id not in graph_nodes:
            return _unavailable(tool, node_id)

        queue = [(node_id, 0)]
        expanded = {node_id}
        seen_edges: set[str] = set()
        found: list[tuple[PackedRelationship, int, str]] = []
        hit_hop_limit = False
        while queue:
            current, hop = queue.pop(0)
            if hop >= limits.max_hops:
                hit_hop_limit = hit_hop_limit or any(
                    _touches(edge, current, direction) for edge in relationships
                )
                continue
            for edge in relationships:
                neighbor = _neighbor(edge, current, direction)
                if neighbor is None or edge.id in seen_edges:
                    continue
                seen_edges.add(edge.id)
                found.append((edge, hop + 1, neighbor))
                if neighbor not in expanded:
                    expanded.add(neighbor)
                    queue.append((neighbor, hop + 1))

        found.sort(key=lambda value: (value[1], value[0].id, value[2]))
        budget = _CharacterBudget(limits.max_characters)
        selected_edges = found[: limits.max_matches]
        items = [
            _relationship_result(edge, hop, neighbor, budget)
            for edge, hop, neighbor in selected_edges
        ]
        return _result(
            tool=tool,
            status="ok" if items else "unavailable",
            items=items,
            budget=budget,
            truncated=hit_hop_limit or len(found) > len(items),
            provenance={"scope": "compiled_evidence_pack"},
        )

    def _navigation_result(
        self,
        tool: str,
        navigation: Any,
        limits: EvidenceToolLimits,
    ) -> dict[str, Any]:
        budget = _CharacterBudget(limits.max_characters)
        candidates = navigation.candidates[: limits.max_matches]
        items = [
            _budget_json_value(candidate.to_dict(), budget) for candidate in candidates
        ]
        result = _result(
            tool=tool,
            status=navigation.status,
            items=items,
            budget=budget,
            truncated=len(navigation.candidates) > len(candidates),
            provenance={
                "scope": "compiled_evidence_pack",
                "provider": navigation.provider,
                "coverage": dict(navigation.coverage),
            },
        )
        result["warnings"] = list(navigation.warnings)
        json.dumps(result)
        return result

    def _symbol_result(
        self,
        entry: RepositoryMapEntry,
        budget: _CharacterBudget,
    ) -> dict[str, Any]:
        sources = [
            _evidence_provenance(item)
            for item in self._pack.evidence
            if entry.path is not None and item.source_file == entry.path
        ]
        relationships = [
            _relationship_result(item, 0, entry.id, budget)
            for item in self._pack.relationships
            if entry.id in {item.source_id, item.target_id}
        ]
        return {
            "id": entry.id,
            "name": entry.name,
            "kind": entry.kind,
            "path": entry.path,
            "start_line": entry.start_line,
            "end_line": entry.end_line,
            "signature": budget.text(entry.signature or ""),
            "relationships": relationships,
            "provenance": sources,
        }

    def _workspace_file(self, value: str) -> Path | None:
        raw = value.strip().replace("\\", "/")
        parsed = PurePosixPath(raw)
        if not raw or parsed.is_absolute() or ".." in parsed.parts:
            return None
        candidate = self._workspace.joinpath(*parsed.parts)
        try:
            candidate.resolve(strict=True).relative_to(self._workspace)
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate if candidate.is_file() else None


def _evidence_search_text(item: PackedEvidence) -> str:
    return " ".join(
        (
            item.text,
            item.owner_type,
            item.owner_id,
            item.source_file,
            item.provenance,
            *item.related_symbols,
            *item.related_files,
        )
    ).casefold()


def _evidence_result(item: PackedEvidence, budget: _CharacterBudget) -> dict[str, Any]:
    return {
        "id": item.id,
        "rank": item.rank,
        "score": item.score,
        "owner_type": item.owner_type,
        "owner_id": item.owner_id,
        "text": budget.text(item.text),
        "provenance": _evidence_provenance(item),
    }


def _evidence_provenance(item: PackedEvidence) -> dict[str, Any]:
    return {
        "source_file": item.source_file,
        "start_line": item.start_line,
        "end_line": item.end_line,
        "extractor": item.provenance,
        "confidence": item.confidence,
        "evidence_id": item.id,
    }


def _symbol_candidates(
    entries: tuple[RepositoryMapEntry, ...], query: str
) -> list[RepositoryMapEntry]:
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("symbol must not be empty")
    symbols = [entry for entry in entries if entry.kind == "symbol"]
    exact = [
        entry
        for entry in symbols
        if needle in {entry.id.casefold(), entry.name.casefold()}
    ]
    candidates = exact or [
        entry for entry in symbols if needle in entry.name.casefold()
    ]
    return sorted(candidates, key=lambda entry: (entry.path or "", entry.id))


def _relationship_confidence(item: PackedRelationship) -> RelationshipConfidence:
    if not item.provenance:
        return "unavailable"
    if item.unresolved:
        return "ambiguous"
    if item.confidence >= 0.9:
        return "confirmed"
    if item.confidence >= 0.5:
        return "inferred"
    return "ambiguous"


def _relationship_result(
    item: PackedRelationship,
    hop: int,
    neighbor: str,
    budget: _CharacterBudget,
) -> dict[str, Any]:
    assessment = assess_legacy_evidence(
        {**dict(item.metadata), "assessment": item.assessment},
        extractor_provenance=item.provenance,
        certainty=item.certainty,
        unresolved=item.unresolved,
    )
    return {
        "id": item.id,
        "source": {"type": item.source_type, "id": item.source_id},
        "relation": item.relation_type,
        "target": {"type": item.target_type, "id": item.target_id},
        "neighbor_id": neighbor,
        "hop": hop,
        "confidence": item.confidence,
        "confidence_class": _relationship_confidence(item),
        "certainty": item.certainty,
        "unresolved": item.unresolved,
        "assessment": assessment.to_dict(),
        "labels": list(assessment.labels),
        "metadata": dict(item.metadata),
        "provenance": {
            "extractors": [budget.text(value) for value in item.provenance],
            "sources": [
                _source_provenance_dict(source) for source in item.source_provenance
            ],
        },
    }


def _source_provenance_dict(source: PackedSourceProvenance) -> dict[str, Any]:
    """Keep legacy tool payloads stable until a source has a revision identity."""
    values = {key: value for key, value in asdict(source).items() if value is not None}
    return json.loads(json.dumps(values))


def _neighbor(
    edge: PackedRelationship, current: str, direction: TraversalDirection
) -> str | None:
    if direction in {"outgoing", "both"} and edge.source_id == current:
        return edge.target_id
    if direction in {"incoming", "both"} and edge.target_id == current:
        return edge.source_id
    return None


def _touches(
    edge: PackedRelationship, current: str, direction: TraversalDirection
) -> bool:
    return _neighbor(edge, current, direction) is not None


def _budget_json_value(value: Any, budget: _CharacterBudget) -> Any:
    if isinstance(value, str):
        return budget.text(value)
    if isinstance(value, dict):
        return {
            str(key): _budget_json_value(item, budget) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_budget_json_value(item, budget) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return budget.text(str(value))


def _result(
    *,
    tool: str,
    status: str,
    items: list[Any],
    budget: _CharacterBudget,
    truncated: bool,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "tool": tool,
        "status": status,
        "items": items,
        "provenance": provenance,
        "limits": {
            "character_limit": budget.limit,
            "characters_used": budget.used,
        },
        "truncated": truncated or budget.truncated,
    }
    json.dumps(result)
    return result


def _unavailable(tool: str, requested_id: str) -> dict[str, Any]:
    return {
        "tool": tool,
        "status": "unavailable",
        "requested_id": requested_id,
        "items": [],
        "provenance": {"scope": "compiled_evidence_pack"},
        "limits": {"character_limit": 0, "characters_used": 0},
        "truncated": False,
    }
