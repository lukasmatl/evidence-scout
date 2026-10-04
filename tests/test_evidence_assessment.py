from __future__ import annotations

import pytest

from code_scout.evidence.assessment import (
    AssessmentReasonCode,
    EvidenceFreshness,
    EvidenceOrigin,
    EvidenceValidation,
    assess_legacy_evidence,
    combine_assessments,
    combine_path_assessments,
)


@pytest.mark.parametrize(
    ("metadata", "extractors", "certainty", "unresolved", "expected"),
    [
        (
            {"freshness": "stale"},
            ("graphify",),
            "confirmed",
            False,
            (
                EvidenceOrigin.INFERRED,
                EvidenceValidation.UNKNOWN,
                False,
                EvidenceFreshness.STALE,
                ("inferred", "stale", "unknown"),
            ),
        ),
        (
            {"resolver_conflicts": ({"field": "target"},), "freshness": "fresh"},
            ("tree_sitter",),
            None,
            False,
            (
                EvidenceOrigin.EXTRACTED,
                EvidenceValidation.UNKNOWN,
                True,
                EvidenceFreshness.FRESH,
                ("extracted", "conflicted", "unknown"),
            ),
        ),
        (
            {"assessment": {"validation": "corroborated", "freshness": "fresh"}},
            ("tree_sitter",),
            "confirmed",
            False,
            (
                EvidenceOrigin.EXTRACTED,
                EvidenceValidation.CORROBORATED,
                False,
                EvidenceFreshness.FRESH,
                ("extracted", "validated"),
            ),
        ),
        (
            {"freshness": "fresh"},
            ("tree_sitter",),
            "unresolved",
            False,
            (
                EvidenceOrigin.EXTRACTED,
                EvidenceValidation.UNRESOLVED,
                False,
                EvidenceFreshness.FRESH,
                ("extracted", "unresolved"),
            ),
        ),
        (
            {},
            (),
            None,
            False,
            (
                EvidenceOrigin.UNKNOWN,
                EvidenceValidation.UNKNOWN,
                False,
                EvidenceFreshness.UNKNOWN,
                ("unknown",),
            ),
        ),
    ],
)
def test_legacy_assessment_mapping_is_independent_and_conservative(
    metadata: dict[str, object],
    extractors: tuple[str, ...],
    certainty: str | None,
    unresolved: bool,
    expected: tuple[
        EvidenceOrigin,
        EvidenceValidation,
        bool,
        EvidenceFreshness,
        tuple[str, ...],
    ],
) -> None:
    assessment = assess_legacy_evidence(
        metadata,
        extractor_provenance=extractors,
        certainty=certainty,
        unresolved=unresolved,
    )

    assert (
        assessment.origin,
        assessment.validation,
        assessment.conflicted,
        assessment.freshness,
        assessment.labels,
    ) == expected


def test_confidence_and_citation_metadata_do_not_validate_a_claim() -> None:
    assessment = assess_legacy_evidence(
        {"citation": {"path": "src/app.py", "start_line": 1, "end_line": 1}},
        extractor_provenance=("tree_sitter",),
        certainty="confirmed",
    )

    assert assessment.validation is EvidenceValidation.UNKNOWN
    assert AssessmentReasonCode.VALIDATION_UNKNOWN in assessment.reason_codes


def test_labels_have_a_fixed_order_when_dimensions_overlap() -> None:
    assessment = assess_legacy_evidence(
        {
            "assessment": {
                "origin": "extracted",
                "validation": "corroborated",
                "conflicted": True,
                "freshness": "stale",
            }
        }
    )

    assert assessment.labels == ("extracted", "validated", "conflicted", "stale")


def _assessment(
    validation: EvidenceValidation,
    *,
    conflicted: bool = False,
    freshness: EvidenceFreshness = EvidenceFreshness.FRESH,
):
    return assess_legacy_evidence(
        {
            "assessment": {
                "origin": "extracted",
                "validation": validation.value,
                "conflicted": conflicted,
                "freshness": freshness.value,
            }
        }
    )


def test_alternative_observations_can_corroborate_one_fact() -> None:
    assessment = combine_assessments(
        (
            _assessment(EvidenceValidation.CORROBORATED),
            _assessment(EvidenceValidation.UNKNOWN),
        )
    )

    assert assessment.validation is EvidenceValidation.CORROBORATED


@pytest.mark.parametrize(
    ("assessments", "expected"),
    [
        (
            (_assessment(EvidenceValidation.CORROBORATED),) * 2,
            EvidenceValidation.CORROBORATED,
        ),
        (
            (
                _assessment(EvidenceValidation.CORROBORATED),
                _assessment(EvidenceValidation.UNKNOWN),
            ),
            EvidenceValidation.UNKNOWN,
        ),
        (
            (
                _assessment(EvidenceValidation.UNRESOLVED),
                _assessment(EvidenceValidation.CORROBORATED),
            ),
            EvidenceValidation.UNRESOLVED,
        ),
    ],
)
def test_required_path_hops_require_complete_corroboration(
    assessments, expected
) -> None:
    assert combine_path_assessments(assessments).validation is expected


def test_path_composition_preserves_conflict_and_freshness() -> None:
    assessment = combine_path_assessments(
        (
            _assessment(EvidenceValidation.CORROBORATED, conflicted=True),
            _assessment(
                EvidenceValidation.CORROBORATED,
                freshness=EvidenceFreshness.STALE,
            ),
        )
    )

    assert assessment.validation is EvidenceValidation.CORROBORATED
    assert assessment.conflicted is True
    assert assessment.freshness is EvidenceFreshness.STALE
