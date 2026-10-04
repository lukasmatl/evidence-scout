from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class OutputReviewWarning:
    code: str
    owner_type: str
    owner_id: str
    field: str
    conflicting_value: Any
    resolved_value: Any
    source_file: str
    extractor_name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolver_conflict_warnings(
    *,
    node_rows: tuple[tuple[str, tuple[dict[str, Any], ...]], ...],
    edges: tuple[dict[str, Any], ...],
) -> tuple[OutputReviewWarning, ...]:
    warnings: list[OutputReviewWarning] = []
    for node_type, rows in node_rows:
        for row in rows:
            warnings.extend(
                _warnings_for_evidence(
                    owner_type=node_type,
                    owner_id=str(row["id"]),
                    evidence_values=row.get("evidence", ()),
                )
            )

    for edge in edges:
        warnings.extend(
            _warnings_for_evidence(
                owner_type="edge",
                owner_id=_edge_owner_id(edge),
                evidence_values=edge.get("evidence", ()),
            )
        )

    return tuple(
        sorted(
            warnings,
            key=lambda warning: (
                warning.owner_type,
                warning.owner_id,
                warning.field,
                str(warning.conflicting_value),
                warning.source_file,
                warning.extractor_name,
            ),
        )
    )


def _warnings_for_evidence(
    *,
    owner_type: str,
    owner_id: str,
    evidence_values: Any,
) -> list[OutputReviewWarning]:
    warnings: list[OutputReviewWarning] = []
    if not isinstance(evidence_values, (list, tuple)):
        return warnings

    for evidence in evidence_values:
        if not isinstance(evidence, dict):
            continue
        metadata = evidence.get("metadata")
        if not isinstance(metadata, dict):
            continue
        conflicts = metadata.get("resolver_conflicts")
        if not isinstance(conflicts, (list, tuple)):
            continue
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                continue
            warnings.append(
                OutputReviewWarning(
                    code="resolver_conflict",
                    owner_type=owner_type,
                    owner_id=owner_id,
                    field=str(conflict.get("field", "")),
                    conflicting_value=conflict.get("conflicting_value"),
                    resolved_value=conflict.get("resolved_value"),
                    source_file=str(evidence.get("file_path", "")),
                    extractor_name=str(evidence.get("extractor_name", "")),
                )
            )
    return warnings


def _edge_owner_id(edge: dict[str, Any]) -> str:
    return (
        f"{edge['source_type']}:{edge['source_id']}:"
        f"{edge['relation_type']}:"
        f"{edge['target_type']}:{edge['target_id']}"
    )
