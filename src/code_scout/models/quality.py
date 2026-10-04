from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from code_scout.exporters.output_graph import OutputGraphData

QualitySeverity = Literal["info", "warning", "error"]

SUPPORTED_QUALITY_SEVERITIES: tuple[QualitySeverity, ...] = (
    "info",
    "warning",
    "error",
)

_SEVERITY_SORT_RANK: dict[QualitySeverity, int] = {
    "error": 0,
    "warning": 1,
    "info": 2,
}


@dataclass(frozen=True)
class QualityFinding:
    code: str
    severity: QualitySeverity
    entity_type: str
    entity_id: str
    message: str
    evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.severity not in SUPPORTED_QUALITY_SEVERITIES:
            supported = ", ".join(SUPPORTED_QUALITY_SEVERITIES)
            msg = (
                f"Unsupported quality severity {self.severity!r}; expected {supported}."
            )
            raise ValueError(msg)

    @property
    def sort_key(self) -> tuple[int, str, str, str, str]:
        return (
            _SEVERITY_SORT_RANK[self.severity],
            self.code,
            self.entity_type,
            self.entity_id,
            self.message,
        )

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "message": self.message,
        }
        if self.evidence:
            value["evidence"] = [
                {key: evidence[key] for key in sorted(evidence)}
                for evidence in self.evidence
            ]
        return value


def sorted_findings(
    findings: tuple[QualityFinding, ...],
) -> tuple[QualityFinding, ...]:
    return tuple(sorted(findings, key=lambda finding: finding.sort_key))


PRODUCER_CONTRACT_RELATIONS = frozenset({"produces", "exposes"})
CONSUMER_CONTRACT_RELATIONS = frozenset({"consumes"})


def analyze_graph_quality(graph_data: OutputGraphData) -> tuple[QualityFinding, ...]:
    findings: list[QualityFinding] = list(_resolver_conflict_findings(graph_data))

    related_node_ids = _related_node_ids(graph_data.edges)

    for service in graph_data.services:
        service_id = service["id"]
        if (
            service_id not in related_node_ids.get("service", frozenset())
            and not service.get("description")
            and not service.get("evidence")
        ):
            findings.append(
                QualityFinding(
                    code="hollow_service",
                    severity="warning",
                    entity_type="service",
                    entity_id=service_id,
                    message=(
                        "Service has no graph links, description, or direct evidence."
                    ),
                ),
            )

    contract_ids_with_producers = _contract_ids_with_relation(
        graph_data.edges,
        PRODUCER_CONTRACT_RELATIONS,
    )
    contract_ids_with_consumers = _contract_ids_with_relation(
        graph_data.edges,
        CONSUMER_CONTRACT_RELATIONS,
    )
    linked_contract_ids = related_node_ids.get("contract", frozenset())
    for contract in graph_data.contracts:
        contract_id = contract["id"]
        if contract_id not in linked_contract_ids:
            findings.append(
                QualityFinding(
                    code="unlinked_contract",
                    severity="warning",
                    entity_type="contract",
                    entity_id=contract_id,
                    message="Contract has no incoming or outgoing graph links.",
                ),
            )
        if contract_id not in contract_ids_with_producers:
            findings.append(
                QualityFinding(
                    code="contract_without_producer",
                    severity="warning",
                    entity_type="contract",
                    entity_id=contract_id,
                    message="Contract has no producing or exposing service.",
                ),
            )
        if contract_id not in contract_ids_with_consumers:
            findings.append(
                QualityFinding(
                    code="contract_without_consumer",
                    severity="warning",
                    entity_type="contract",
                    entity_id=contract_id,
                    message="Contract has no consuming graph link.",
                ),
            )

    for concept in graph_data.concepts:
        concept_id = concept["id"]
        if concept_id not in related_node_ids.get("concept", frozenset()):
            findings.append(
                QualityFinding(
                    code="concept_without_relationships",
                    severity="info",
                    entity_type="concept",
                    entity_id=concept_id,
                    message="Concept has no graph relationships.",
                ),
            )

    for decision in graph_data.decisions:
        decision_id = decision["id"]
        if decision_id not in related_node_ids.get("decision", frozenset()):
            findings.append(
                QualityFinding(
                    code="decision_without_graph_links",
                    severity="info",
                    entity_type="decision",
                    entity_id=decision_id,
                    message="Decision has no graph links.",
                ),
            )

    return sorted_findings(tuple(findings))


def _resolver_conflict_findings(
    graph_data: OutputGraphData,
) -> tuple[QualityFinding, ...]:
    return tuple(
        QualityFinding(
            code="resolver_conflict",
            severity="error",
            entity_type=warning.owner_type,
            entity_id=warning.owner_id,
            message=(
                "Resolver conflict for "
                f"{warning.owner_type} {warning.owner_id} field {warning.field}."
            ),
            evidence=(
                {
                    "owner_type": warning.owner_type,
                    "owner_id": warning.owner_id,
                    "field": warning.field,
                    "extractor_name": warning.extractor_name,
                    "source_file": warning.source_file,
                    "resolved_value": warning.resolved_value,
                    "conflicting_value": warning.conflicting_value,
                },
            ),
        )
        for warning in graph_data.warnings
    )


def _related_node_ids(
    edges: tuple[dict[str, Any], ...],
) -> dict[str, frozenset[str]]:
    related: dict[str, set[str]] = {}
    for edge in edges:
        for type_key, id_key in (
            ("source_type", "source_id"),
            ("target_type", "target_id"),
        ):
            node_type = edge.get(type_key)
            node_id = edge.get(id_key)
            if isinstance(node_type, str) and isinstance(node_id, str):
                related.setdefault(node_type, set()).add(node_id)
    return {node_type: frozenset(node_ids) for node_type, node_ids in related.items()}


def _contract_ids_with_relation(
    edges: tuple[dict[str, Any], ...],
    relation_types: frozenset[str],
) -> frozenset[str]:
    return frozenset(
        edge["target_id"]
        for edge in edges
        if edge.get("target_type") == "contract"
        and edge.get("relation_type") in relation_types
        and isinstance(edge.get("target_id"), str)
    )
