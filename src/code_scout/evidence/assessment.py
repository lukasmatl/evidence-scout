"""Independent, conservative assessment of an evidence-backed claim.

Confidence and citations describe how an extractor or resolver produced a
record.  They are deliberately not inputs to ``validation=corroborated``:
that state requires an explicit corroboration marker for this particular fact.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class EvidenceOrigin(StrEnum):
    EXTRACTED = "extracted"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class EvidenceValidation(StrEnum):
    CORROBORATED = "corroborated"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


class EvidenceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class AssessmentReasonCode(StrEnum):
    ORIGIN_EXPLICIT = "assessment.origin.explicit"
    ORIGIN_EXTRACTOR_EXTRACTED = "assessment.origin.extractor_extracted"
    ORIGIN_EXTRACTOR_INFERRED = "assessment.origin.extractor_inferred"
    ORIGIN_LEGACY_CERTAINTY_INFERRED = "assessment.origin.legacy_certainty_inferred"
    ORIGIN_LEGACY_CERTAINTY_NON_ORIGIN = "assessment.origin.legacy_certainty_not_origin"
    ORIGIN_UNKNOWN = "assessment.origin.unknown"
    VALIDATION_EXPLICIT = "assessment.validation.explicit"
    VALIDATION_LEGACY_UNRESOLVED = "assessment.validation.legacy_unresolved"
    VALIDATION_LEGACY_CERTAINTY_UNRESOLVED = (
        "assessment.validation.legacy_certainty_unresolved"
    )
    VALIDATION_UNKNOWN = "assessment.validation.unknown"
    CONFLICT_EXPLICIT = "assessment.conflict.explicit"
    CONFLICT_RESOLVER = "assessment.conflict.resolver_conflicts"
    FRESHNESS_EXPLICIT = "assessment.freshness.explicit"
    FRESHNESS_UNKNOWN = "assessment.freshness.unknown"


_EXTRACTED_EXTRACTORS = frozenset(
    {
        "adr",
        "adr_markdown",
        "asyncapi",
        "dotnet",
        "openapi",
        "package_json",
        "tree_sitter",
        "typescript",
    }
)
_INFERRED_EXTRACTORS = frozenset(
    {"docs", "graphify", "heuristic", "lexical", "lsp", "serena", "dede"}
)
_LABEL_ORDER = (
    "extracted",
    "inferred",
    "validated",
    "unresolved",
    "conflicted",
    "stale",
    "unknown",
)


@dataclass(frozen=True)
class EvidenceAssessment:
    """Typed claim assessment, independent of extractor confidence."""

    origin: EvidenceOrigin
    validation: EvidenceValidation
    conflicted: bool
    freshness: EvidenceFreshness
    reason_codes: tuple[AssessmentReasonCode, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        selected = {
            self.origin.value,
            "validated"
            if self.validation is EvidenceValidation.CORROBORATED
            else self.validation.value,
            "conflicted" if self.conflicted else "",
            "stale"
            if self.freshness is EvidenceFreshness.STALE
            else self.freshness.value,
        }
        return tuple(label for label in _LABEL_ORDER if label in selected)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "labels": list(self.labels)}


def assess_legacy_evidence(
    metadata: Mapping[str, Any] | None,
    *,
    extractor_provenance: Iterable[str] = (),
    certainty: str | None = None,
    unresolved: bool = False,
    freshness: str | None = None,
) -> EvidenceAssessment:
    """Map legacy metadata without promoting confidence or citations to proof."""
    values = metadata or {}
    nested = values.get("assessment")
    assessment = nested if isinstance(nested, Mapping) else {}
    origin, origin_reason = _origin(assessment, values, extractor_provenance, certainty)
    validation, validation_reason = _validation(
        assessment, values, unresolved, certainty
    )
    conflicted, conflict_reason = _conflict(assessment, values)
    current_freshness, freshness_reason = _freshness(assessment, values, freshness)
    return EvidenceAssessment(
        origin=origin,
        validation=validation,
        conflicted=conflicted,
        freshness=current_freshness,
        reason_codes=(
            origin_reason,
            validation_reason,
            *conflict_reason,
            freshness_reason,
        ),
    )


def combine_assessments(
    assessments: Iterable[EvidenceAssessment], *, freshness: str | None = None
) -> EvidenceAssessment:
    """Combine alternative observations that describe the same fact.

    One corroborated provider can corroborate that fact even when another
    provider has no validation marker.  This must not be used for dependency
    paths, where every edge is required; use :func:`combine_path_assessments`.
    """
    values = tuple(assessments)
    if not values:
        return assess_legacy_evidence({}, freshness=freshness)
    origins = {item.origin for item in values}
    origin = (
        EvidenceOrigin.INFERRED
        if EvidenceOrigin.INFERRED in origins
        else next(iter(origins))
        if len(origins) == 1
        else EvidenceOrigin.UNKNOWN
    )
    validations = {item.validation for item in values}
    validation = (
        EvidenceValidation.UNRESOLVED
        if EvidenceValidation.UNRESOLVED in validations
        else EvidenceValidation.CORROBORATED
        if EvidenceValidation.CORROBORATED in validations
        else EvidenceValidation.UNKNOWN
    )
    stale = any(item.freshness is EvidenceFreshness.STALE for item in values)
    fresh = all(item.freshness is EvidenceFreshness.FRESH for item in values)
    combined_freshness = (
        EvidenceFreshness.STALE
        if stale
        else EvidenceFreshness.FRESH
        if fresh
        else EvidenceFreshness.UNKNOWN
    )
    override_freshness = _enum_value(EvidenceFreshness, freshness)
    if override_freshness is not None:
        combined_freshness = override_freshness
    reasons = tuple(
        sorted(
            set(
                (
                    *(code for item in values for code in item.reason_codes),
                    *(
                        (AssessmentReasonCode.FRESHNESS_EXPLICIT,)
                        if override_freshness is not None
                        else ()
                    ),
                )
            )
        )
    )
    return EvidenceAssessment(
        origin,
        validation,
        any(item.conflicted for item in values),
        combined_freshness,
        reasons,
    )


def combine_path_assessments(
    assessments: Iterable[EvidenceAssessment], *, freshness: str | None = None
) -> EvidenceAssessment:
    """Combine assessments for required dependency hops in a single path.

    A path is corroborated only when every required edge is corroborated.
    Unresolved evidence remains dominant so callers retain its explicit review
    signal instead of reducing it to an unknown path.
    """
    values = tuple(assessments)
    combined = combine_assessments(values, freshness=freshness)
    if not values or EvidenceValidation.UNRESOLVED in {
        item.validation for item in values
    }:
        return combined
    validation = (
        EvidenceValidation.CORROBORATED
        if all(item.validation is EvidenceValidation.CORROBORATED for item in values)
        else EvidenceValidation.UNKNOWN
    )
    return EvidenceAssessment(
        combined.origin,
        validation,
        combined.conflicted,
        combined.freshness,
        combined.reason_codes,
    )


def _origin(
    assessment: Mapping[str, Any],
    metadata: Mapping[str, Any],
    extractors: Iterable[str],
    certainty: str | None,
) -> tuple[EvidenceOrigin, AssessmentReasonCode]:
    explicit = _enum_value(
        EvidenceOrigin,
        assessment.get(
            "origin", metadata.get("evidence_origin", metadata.get("origin"))
        ),
    )
    if explicit is not None:
        return explicit, AssessmentReasonCode.ORIGIN_EXPLICIT
    normalized = {
        value.casefold().replace("-", "_")
        for value in extractors
        if isinstance(value, str)
    }
    if normalized & _INFERRED_EXTRACTORS:
        return EvidenceOrigin.INFERRED, AssessmentReasonCode.ORIGIN_EXTRACTOR_INFERRED
    if normalized & _EXTRACTED_EXTRACTORS:
        return EvidenceOrigin.EXTRACTED, AssessmentReasonCode.ORIGIN_EXTRACTOR_EXTRACTED
    if isinstance(certainty, str) and certainty.casefold() == "inferred":
        return (
            EvidenceOrigin.INFERRED,
            AssessmentReasonCode.ORIGIN_LEGACY_CERTAINTY_INFERRED,
        )
    if isinstance(certainty, str) and certainty.casefold() in {"confirmed", "certain"}:
        return (
            EvidenceOrigin.UNKNOWN,
            AssessmentReasonCode.ORIGIN_LEGACY_CERTAINTY_NON_ORIGIN,
        )
    return EvidenceOrigin.UNKNOWN, AssessmentReasonCode.ORIGIN_UNKNOWN


def _validation(
    assessment: Mapping[str, Any],
    metadata: Mapping[str, Any],
    unresolved: bool,
    certainty: str | None,
) -> tuple[EvidenceValidation, AssessmentReasonCode]:
    explicit = _enum_value(
        EvidenceValidation, assessment.get("validation", metadata.get("validation"))
    )
    if unresolved or metadata.get("unresolved") is True:
        return (
            EvidenceValidation.UNRESOLVED,
            AssessmentReasonCode.VALIDATION_LEGACY_UNRESOLVED,
        )
    if isinstance(certainty, str) and certainty.casefold() == "unresolved":
        return (
            EvidenceValidation.UNRESOLVED,
            AssessmentReasonCode.VALIDATION_LEGACY_CERTAINTY_UNRESOLVED,
        )
    if explicit is not None:
        return explicit, AssessmentReasonCode.VALIDATION_EXPLICIT
    return EvidenceValidation.UNKNOWN, AssessmentReasonCode.VALIDATION_UNKNOWN


def _conflict(
    assessment: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[bool, tuple[AssessmentReasonCode, ...]]:
    explicit = assessment.get("conflicted", metadata.get("conflicted"))
    if bool(
        metadata.get("resolver_conflicts")
        or metadata.get("conflict")
        or metadata.get("ambiguous")
    ):
        return True, (AssessmentReasonCode.CONFLICT_RESOLVER,)
    if isinstance(explicit, bool):
        return explicit, (AssessmentReasonCode.CONFLICT_EXPLICIT,) if explicit else ()
    return False, ()


def _freshness(
    assessment: Mapping[str, Any], metadata: Mapping[str, Any], fallback: str | None
) -> tuple[EvidenceFreshness, AssessmentReasonCode]:
    raw = assessment.get(
        "freshness",
        metadata.get("freshness", metadata.get("freshness_state", fallback)),
    )
    if isinstance(raw, Mapping):
        raw = raw.get("state")
    value = _enum_value(EvidenceFreshness, raw)
    if value is not None:
        return value, AssessmentReasonCode.FRESHNESS_EXPLICIT
    return EvidenceFreshness.UNKNOWN, AssessmentReasonCode.FRESHNESS_UNKNOWN


def _enum_value(enum: type[StrEnum], value: object) -> Any | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold().replace("-", "_")
    try:
        return enum(normalized)
    except ValueError:
        return None


def assess_raw_evidence(
    metadata: Mapping[str, Any], provenance: str
) -> EvidenceAssessment:
    """Project stored raw-observation metadata, including fact-level uncertainty."""
    values = {**metadata, **metadata.get("evidence_metadata", {})}
    return assess_legacy_evidence(
        values, extractor_provenance=(provenance,), certainty=values.get("certainty")
    )


def assess_graph_evidence(records: Iterable[Mapping[str, Any]]) -> EvidenceAssessment:
    """Assess local graph observations without treating a source range as proof."""
    return combine_assessments(
        assess_legacy_evidence(
            {
                **record.get("metadata", {}),
                **(
                    {"assessment": record["assessment"]}
                    if record.get("assessment")
                    else {}
                ),
            },
            extractor_provenance=(record.get("extractor_name", ""),),
            certainty=record.get("metadata", {}).get("certainty"),
        )
        for record in records
    )
