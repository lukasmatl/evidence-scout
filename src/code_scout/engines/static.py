from __future__ import annotations

import re
from time import monotonic

from code_scout.core.errors import CodeScoutError
from code_scout.engines.availability import AvailabilityState, ProviderAvailability
from code_scout.engines.contracts import (
    Citation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    SupportingEvidence,
    TimingMetadata,
)
from code_scout.evidence.query import (
    DEFAULT_SEMANTIC_QUERY_LIMIT,
    SemanticEvidenceMatch,
    semantic_evidence_search,
)
from code_scout.workspace.paths import DATABASE_PATH, workspace_state_path

STATIC_ENGINE_PROVIDER = "local-index"
_MAX_MATCH_TEXT_CHARACTERS = 320


class StaticIndexError(CodeScoutError):
    """The workspace-local index cannot satisfy a static query."""


class StaticEngine:
    """Query scan-backed evidence deterministically, without an LLM."""

    metadata = EngineMetadata(name="static", provider=STATIC_ENGINE_PROVIDER)

    def __init__(self, *, limit: int = DEFAULT_SEMANTIC_QUERY_LIMIT) -> None:
        if limit <= 0:
            raise ValueError("limit must be greater than zero.")
        self._limit = limit

    def availability(self, *, probe_endpoint: bool = False) -> ProviderAvailability:
        del probe_endpoint
        return ProviderAvailability(
            AvailabilityState.READY,
            "The deterministic workspace-local index engine is ready.",
        )

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        database = workspace_state_path(request.workspace, DATABASE_PATH)
        if not database.is_file():
            raise StaticIndexError(
                f"Static index not found for workspace: {request.workspace}.",
                "Run `code-scout scan --workspace <path>` before querying "
                "with `--engine static`.",
            )

        started = monotonic()
        matches = semantic_evidence_search(
            str(request.workspace),
            question=request.query,
            limit=self._limit,
        )
        return CodeScoutResult(
            answer=_render_matches(request.query, matches),
            citations=_citations(matches),
            warnings=(),
            engine=self.metadata,
            timing=TimingMetadata(duration_seconds=monotonic() - started),
            metadata=_result_metadata(request.query, matches),
        )


def _render_matches(
    query: str,
    matches: tuple[SemanticEvidenceMatch, ...],
) -> str:
    if not matches:
        return f'No static evidence found for: "{query}"'

    lines = [f'Static index matches for: "{query}"']
    for index, match in enumerate(matches, start=1):
        location = _location(match)
        text = _bounded_text(match.text)
        reasons = ", ".join(match.scoring_reasons)
        lines.extend(
            (
                "",
                f"{index}. {location} — {text}",
                f"   Evidence: {', '.join(match.assessment.labels)}",
                f"   Match: {reasons}; score={match.score:.3f}",
            )
        )
    return "\n".join(lines)


def _location(match: SemanticEvidenceMatch) -> str:
    if not match.source_file:
        return match.owner_entity_id
    if match.start_line is None:
        return match.source_file
    if match.end_line is None or match.end_line == match.start_line:
        return f"{match.source_file}:{match.start_line}"
    return f"{match.source_file}:{match.start_line}-{match.end_line}"


def _bounded_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= _MAX_MATCH_TEXT_CHARACTERS:
        return normalized
    return f"{normalized[: _MAX_MATCH_TEXT_CHARACTERS - 1].rstrip()}…"


def _citations(
    matches: tuple[SemanticEvidenceMatch, ...],
) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    seen: set[Citation] = set()
    for match in matches:
        if not match.source_file or match.start_line is None:
            continue
        citation = Citation(
            match.source_file,
            match.start_line,
            match.end_line or match.start_line,
            (SupportingEvidence(match.evidence_unit_id, match.assessment),),
            match.repository_id,
            match.source_revision_id,
            match.content_sha256,
        )
        if citation not in seen:
            seen.add(citation)
            citations.append(citation)
    return tuple(citations)


def _match_metadata(
    matches: tuple[SemanticEvidenceMatch, ...],
) -> list[dict[str, object]]:
    return [
        {
            "rank": index,
            "evidence_id": match.evidence_unit_id,
            "assessment": match.assessment.to_dict(),
            "score": match.score,
            "source_file": match.source_file,
            "line_range": {
                "start": match.start_line,
                "end": match.end_line,
            },
            "owner_entity": {
                "type": match.owner_entity_type,
                "id": match.owner_entity_id,
            },
            "text": _bounded_text(match.text),
            "scoring_reasons": list(match.scoring_reasons),
        }
        for index, match in enumerate(matches, start=1)
    ]


def _result_metadata(
    query: str,
    matches: tuple[SemanticEvidenceMatch, ...],
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "index_path": DATABASE_PATH,
        "match_count": len(matches),
        "matches": _match_metadata(matches),
        "query": query,
        "query_mode": "deterministic-hybrid",
        "result_kind": "matched" if matches else "no_match",
    }
    if not matches:
        metadata["gap_reason"] = "no_static_evidence_found"
        metadata["needs_review"] = [
            {
                "code": "no-static-evidence-found",
                "message": (
                    "No static evidence was found for this query in the scanned "
                    "workspace."
                ),
            }
        ]
    return metadata


__all__ = [
    "STATIC_ENGINE_PROVIDER",
    "StaticEngine",
    "StaticIndexError",
]
