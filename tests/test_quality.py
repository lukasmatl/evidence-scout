from __future__ import annotations

import pytest

from code_scout.exporters.output_graph import OutputGraphData
from code_scout.models.quality import (
    SUPPORTED_QUALITY_SEVERITIES,
    QualityFinding,
    analyze_graph_quality,
    sorted_findings,
)


def test_quality_finding_serializes_to_deterministic_dict() -> None:
    finding = QualityFinding(
        code="hollow_service",
        severity="warning",
        entity_type="service",
        entity_id="service:workspace:api",
        message="Service has no relationships.",
        evidence=(
            {
                "source_file": "src/api.py",
                "extractor": "graphify",
                "line": 12,
            },
        ),
    )

    assert finding.to_dict() == {
        "code": "hollow_service",
        "severity": "warning",
        "entity_type": "service",
        "entity_id": "service:workspace:api",
        "message": "Service has no relationships.",
        "evidence": [
            {
                "extractor": "graphify",
                "line": 12,
                "source_file": "src/api.py",
            },
        ],
    }


def test_quality_finding_omits_empty_evidence() -> None:
    finding = QualityFinding(
        code="concept_without_relationships",
        severity="info",
        entity_type="concept",
        entity_id="concept:billing",
        message="Concept has no graph relationships.",
    )

    assert finding.to_dict() == {
        "code": "concept_without_relationships",
        "severity": "info",
        "entity_type": "concept",
        "entity_id": "concept:billing",
        "message": "Concept has no graph relationships.",
    }


def test_quality_finding_rejects_unsupported_severity() -> None:
    assert SUPPORTED_QUALITY_SEVERITIES == ("info", "warning", "error")

    with pytest.raises(ValueError, match="Unsupported quality severity"):
        QualityFinding(
            code="unknown",
            severity="critical",  # type: ignore[arg-type]
            entity_type="service",
            entity_id="service:workspace:api",
            message="Unsupported severity.",
        )


def test_sorted_findings_orders_by_severity_then_stable_fields() -> None:
    info = QualityFinding(
        code="concept_without_relationships",
        severity="info",
        entity_type="concept",
        entity_id="concept:billing",
        message="Concept has no graph relationships.",
    )
    warning_b = QualityFinding(
        code="hollow_service",
        severity="warning",
        entity_type="service",
        entity_id="service:workspace:web",
        message="Service has no relationships.",
    )
    error = QualityFinding(
        code="resolver_conflict",
        severity="error",
        entity_type="service",
        entity_id="service:workspace:api",
        message="Conflicting resolver evidence needs review.",
    )
    warning_a = QualityFinding(
        code="hollow_service",
        severity="warning",
        entity_type="service",
        entity_id="service:workspace:api",
        message="Service has no relationships.",
    )

    assert sorted_findings((info, warning_b, error, warning_a)) == (
        error,
        warning_a,
        warning_b,
        info,
    )


def test_analyze_graph_quality_handles_empty_graph() -> None:
    assert analyze_graph_quality(_graph()) == ()


def test_analyze_graph_quality_returns_no_findings_for_healthy_graph() -> None:
    graph = _graph(
        services=(
            {
                "id": "service:workspace:api",
                "name": "API",
                "description": "Serves user requests.",
                "evidence": ({"file_path": "src/api.py"},),
            },
        ),
        contracts=(
            {
                "id": "contract:workspace:get-users",
                "name": "GET /users",
            },
        ),
        concepts=(
            {
                "id": "concept:workspace:users",
                "name": "Users",
            },
        ),
        decisions=(
            {
                "id": "decision:workspace:adr-0001",
                "title": "Use HTTP",
            },
        ),
        edges=(
            _edge(
                source_type="service",
                source_id="service:workspace:api",
                relation_type="exposes",
                target_type="contract",
                target_id="contract:workspace:get-users",
            ),
            _edge(
                source_type="service",
                source_id="service:workspace:web",
                relation_type="consumes",
                target_type="contract",
                target_id="contract:workspace:get-users",
            ),
            _edge(
                source_type="decision",
                source_id="decision:workspace:adr-0001",
                relation_type="affects",
                target_type="concept",
                target_id="concept:workspace:users",
            ),
        ),
    )

    assert analyze_graph_quality(graph) == ()


def test_analyze_graph_quality_includes_resolver_conflict_findings() -> None:
    graph = _graph(
        services=(
            {
                "id": "service:workspace:payments",
                "name": "Payments",
                "description": "Handles payment workflows.",
                "evidence": ({"file_path": "src/payments.py"},),
            },
        ),
        contracts=(
            {
                "id": "contract:workspace:get-payments",
                "name": "GET /payments",
                "operation_id": "listPayments",
                "evidence": (
                    {
                        "file_path": "contracts/openapi.yaml",
                        "extractor_name": "openapi",
                        "metadata": {
                            "resolver_conflicts": (
                                {
                                    "field": "operation_id",
                                    "conflicting_value": "listLegacyPayments",
                                    "resolved_value": "listPayments",
                                },
                            ),
                        },
                    },
                ),
            },
        ),
        edges=(
            _edge(
                source_type="service",
                source_id="service:workspace:payments",
                relation_type="exposes",
                target_type="contract",
                target_id="contract:workspace:get-payments",
            ),
            _edge(
                source_type="service",
                source_id="service:workspace:web",
                relation_type="consumes",
                target_type="contract",
                target_id="contract:workspace:get-payments",
            ),
        ),
    )

    assert [finding.to_dict() for finding in analyze_graph_quality(graph)] == [
        {
            "code": "resolver_conflict",
            "severity": "error",
            "entity_type": "contract",
            "entity_id": "contract:workspace:get-payments",
            "message": (
                "Resolver conflict for contract "
                "contract:workspace:get-payments field operation_id."
            ),
            "evidence": [
                {
                    "conflicting_value": "listLegacyPayments",
                    "extractor_name": "openapi",
                    "field": "operation_id",
                    "owner_id": "contract:workspace:get-payments",
                    "owner_type": "contract",
                    "resolved_value": "listPayments",
                    "source_file": "contracts/openapi.yaml",
                },
            ],
        },
    ]


def test_analyze_graph_quality_detects_multiple_quality_gaps() -> None:
    graph = _graph(
        services=(
            {
                "id": "service:workspace:hollow",
                "name": "Hollow",
                "description": "",
                "evidence": (),
            },
            {
                "id": "service:workspace:described",
                "name": "Described",
                "description": "Known service without links.",
                "evidence": (),
            },
            {
                "id": "service:workspace:evidenced",
                "name": "Evidenced",
                "description": "",
                "evidence": ({"file_path": "src/evidenced.py"},),
            },
            {
                "id": "service:workspace:linked",
                "name": "Linked",
                "description": "",
                "evidence": (),
            },
        ),
        contracts=(
            {
                "id": "contract:workspace:unlinked",
                "name": "Unlinked",
            },
            {
                "id": "contract:workspace:no-producer",
                "name": "No producer",
            },
            {
                "id": "contract:workspace:no-consumer",
                "name": "No consumer",
            },
        ),
        concepts=(
            {
                "id": "concept:workspace:orphan",
                "name": "Orphan",
            },
        ),
        decisions=(
            {
                "id": "decision:workspace:orphan",
                "title": "Orphan decision",
            },
        ),
        edges=(
            _edge(
                source_type="service",
                source_id="service:workspace:linked",
                relation_type="documents",
                target_type="file",
                target_id="file:workspace:src/linked.py",
            ),
            _edge(
                source_type="service",
                source_id="service:workspace:linked",
                relation_type="consumes",
                target_type="contract",
                target_id="contract:workspace:no-producer",
            ),
            _edge(
                source_type="service",
                source_id="service:workspace:linked",
                relation_type="produces",
                target_type="contract",
                target_id="contract:workspace:no-consumer",
            ),
        ),
    )

    assert [finding.to_dict() for finding in analyze_graph_quality(graph)] == [
        {
            "code": "contract_without_consumer",
            "severity": "warning",
            "entity_type": "contract",
            "entity_id": "contract:workspace:no-consumer",
            "message": "Contract has no consuming graph link.",
        },
        {
            "code": "contract_without_consumer",
            "severity": "warning",
            "entity_type": "contract",
            "entity_id": "contract:workspace:unlinked",
            "message": "Contract has no consuming graph link.",
        },
        {
            "code": "contract_without_producer",
            "severity": "warning",
            "entity_type": "contract",
            "entity_id": "contract:workspace:no-producer",
            "message": "Contract has no producing or exposing service.",
        },
        {
            "code": "contract_without_producer",
            "severity": "warning",
            "entity_type": "contract",
            "entity_id": "contract:workspace:unlinked",
            "message": "Contract has no producing or exposing service.",
        },
        {
            "code": "hollow_service",
            "severity": "warning",
            "entity_type": "service",
            "entity_id": "service:workspace:hollow",
            "message": "Service has no graph links, description, or direct evidence.",
        },
        {
            "code": "unlinked_contract",
            "severity": "warning",
            "entity_type": "contract",
            "entity_id": "contract:workspace:unlinked",
            "message": "Contract has no incoming or outgoing graph links.",
        },
        {
            "code": "concept_without_relationships",
            "severity": "info",
            "entity_type": "concept",
            "entity_id": "concept:workspace:orphan",
            "message": "Concept has no graph relationships.",
        },
        {
            "code": "decision_without_graph_links",
            "severity": "info",
            "entity_type": "decision",
            "entity_id": "decision:workspace:orphan",
            "message": "Decision has no graph links.",
        },
    ]


def _graph(
    *,
    repositories: tuple[dict[str, object], ...] = (),
    files: tuple[dict[str, object], ...] = (),
    services: tuple[dict[str, object], ...] = (),
    contracts: tuple[dict[str, object], ...] = (),
    concepts: tuple[dict[str, object], ...] = (),
    decisions: tuple[dict[str, object], ...] = (),
    edges: tuple[dict[str, object], ...] = (),
    symbols: tuple[dict[str, object], ...] = (),
) -> OutputGraphData:
    return OutputGraphData(
        repositories=repositories,
        files=files,
        services=services,
        contracts=contracts,
        concepts=concepts,
        decisions=decisions,
        edges=edges,
        symbols=symbols,
    )


def _edge(
    *,
    source_type: str,
    source_id: str,
    relation_type: str,
    target_type: str,
    target_id: str,
) -> dict[str, object]:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "relation_type": relation_type,
        "target_type": target_type,
        "target_id": target_id,
        "confidence": 0.9,
        "evidence": (),
    }
