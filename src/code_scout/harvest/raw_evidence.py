from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from code_scout.models.facts import ActiveFact, FactEvidence, fact_evidence_metadata
from code_scout.storage.raw_evidence import (
    RawEvidenceUnit,
    deterministic_raw_evidence_unit_id,
)


def raw_evidence_units_from_active_facts(
    facts: Iterable[ActiveFact],
) -> tuple[RawEvidenceUnit, ...]:
    return tuple(
        unit for fact in facts for unit in raw_evidence_units_from_active_fact(fact)
    )


def raw_evidence_units_from_active_fact(
    fact: ActiveFact,
) -> tuple[RawEvidenceUnit, ...]:
    owner = _owner_from_fact(fact)
    if owner is None:
        return ()
    owner_entity_type, owner_entity_id = owner
    return tuple(
        _raw_evidence_unit_from_fact_evidence(
            fact,
            evidence,
            owner_entity_type=owner_entity_type,
            owner_entity_id=owner_entity_id,
        )
        for evidence in fact.evidence
    )


def _raw_evidence_unit_from_fact_evidence(
    fact: ActiveFact,
    evidence: FactEvidence,
    *,
    owner_entity_type: str,
    owner_entity_id: str,
) -> RawEvidenceUnit:
    source_file = _canonical_source_file(evidence.file_path)
    provenance = evidence.extractor_name
    text = _canonical_evidence_text(fact, evidence)
    return RawEvidenceUnit(
        id=deterministic_raw_evidence_unit_id(
            owner_entity_type=owner_entity_type,
            owner_entity_id=owner_entity_id,
            source_file=source_file,
            start_line=evidence.start_line,
            end_line=evidence.end_line,
            text=text,
            provenance=provenance,
        ),
        owner_entity_type=owner_entity_type,
        owner_entity_id=owner_entity_id,
        source_file=source_file,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        text=text,
        provenance=provenance,
        confidence=evidence.confidence,
        metadata={
            "fact_id": fact.id,
            "fact_kind": fact.kind,
            "fact_extractor": fact.extractor,
            "fact_source_file": fact.source_file,
            "evidence_metadata": fact_evidence_metadata(fact, evidence),
            **{
                key: evidence.metadata[key]
                for key in (
                    "repository_id",
                    "source_revision_id",
                    "content_sha256",
                    "source_revision_unavailable_reason",
                )
                if key in evidence.metadata
            },
        },
    )


def _owner_from_fact(fact: ActiveFact) -> tuple[str, str] | None:
    entity_type = fact.identity.get("entity_type")
    entity_id = fact.identity.get("entity_id")
    if isinstance(entity_type, str) and isinstance(entity_id, str):
        return entity_type, entity_id

    subject = _fact_reference(fact.identity.get("subject"))
    relation = fact.identity.get("relation")
    object_ = _fact_reference(fact.identity.get("object"))
    if subject is None or not isinstance(relation, str) or object_ is None:
        return None
    return (
        "relationship",
        (
            f"{subject['type']}:{subject['id']}:"
            f"{relation}:{object_['type']}:{object_['id']}"
        ),
    )


def _fact_reference(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    type_ = value.get("type")
    id_ = value.get("id")
    if not isinstance(type_, str) or not isinstance(id_, str):
        return None
    return {"type": type_, "id": id_}


def _canonical_evidence_text(fact: ActiveFact, evidence: FactEvidence) -> str:
    payload: dict[str, Any] = {
        "kind": fact.kind,
        "identity": fact.identity,
        "properties": fact.properties,
        "evidence": asdict(evidence),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _canonical_source_file(source_file: str) -> str:
    return source_file.replace("\\", "/").removeprefix("./")
