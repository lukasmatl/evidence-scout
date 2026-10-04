from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from code_scout.domain.normalize import normalized_contract_id
from code_scout.evidence.assessment import (
    assess_legacy_evidence,
    combine_assessments,
)
from code_scout.extractors.package_json import RepositoryPackageFacts
from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    Evidence,
    File,
    Repository,
    Service,
    Symbol,
)
from code_scout.utils.identifiers import slugify_identifier

DEFAULT_EXTRACTOR_CONFIDENCE: dict[str, float] = {
    "openapi": 1.0,
    "asyncapi": 1.0,
    "graphify": 0.85,
    "lsp": 0.85,
    "tree_sitter": 1.0,
    "adr": 1.0,
    "docs": 0.85,
    "infra": 0.8,
    "package_metadata": 1.0,
    "package_json": 1.0,
}


@dataclass(frozen=True)
class FactReference:
    type: str
    id: str


@dataclass(frozen=True)
class FactEvidence:
    file_path: str
    extractor_name: str
    confidence: float
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractorMetadata:
    name: str
    version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActiveFact:
    id: str
    extractor: str
    kind: str
    source_file: str
    identity: dict[str, Any]
    properties: dict[str, Any]
    evidence: tuple[FactEvidence, ...]
    confidence: float
    conflict_metadata: dict[str, Any] = field(default_factory=dict)
    extractor_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntityFact:
    entity_type: str
    entity_id: str
    properties: dict[str, Any]
    evidence: tuple[FactEvidence, ...]
    confidence: float
    extractor_metadata: ExtractorMetadata

    @property
    def reference(self) -> FactReference:
        return FactReference(type=self.entity_type, id=self.entity_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelationFact:
    subject: FactReference
    relation: str
    object: FactReference
    properties: dict[str, Any]
    evidence: tuple[FactEvidence, ...]
    confidence: float
    extractor_metadata: ExtractorMetadata

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def repository_to_entity_facts(repository: Repository) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="repository",
            entity_id=repository.id,
            properties={
                "name": repository.name,
                "path": repository.path,
                "remote_url": repository.remote_url,
                "default_branch": repository.default_branch,
                "language_hints": repository.language_hints,
                "framework_hints": repository.framework_hints,
                "module_hints": repository.module_hints,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in repository.evidence
    )


def file_to_entity_facts(file: File) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="file",
            entity_id=file.id,
            properties={
                "repository_id": file.repository_id,
                "path": file.path,
                "kind": file.kind,
                "extension": file.extension,
                "language": file.language,
                "content_hash": file.content_hash,
                "modified_at": file.modified_at,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in file.evidence
    )


def symbol_to_entity_facts(symbol: Symbol) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="symbol",
            entity_id=symbol.id,
            properties={
                "file_id": symbol.file_id,
                "name": symbol.name,
                "kind": symbol.kind,
                "qualified_name": symbol.qualified_name,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in symbol.evidence
    )


def service_to_entity_facts(service: Service) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="service",
            entity_id=service.id,
            properties={
                "name": service.name,
                "repository_id": service.repository_id,
                "description": service.description,
                "aliases": service.aliases,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in service.evidence
    )


def contract_to_entity_facts(contract: Contract) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="contract",
            entity_id=normalized_contract_id(contract),
            properties={
                "name": contract.name,
                "kind": contract.kind,
                "path": contract.path,
                "version": contract.version,
                "method": contract.method,
                "producer_hint": contract.producer_hint,
                "operation_id": contract.operation_id,
                "request_dtos": contract.request_dtos,
                "response_dtos": contract.response_dtos,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in contract.evidence
    )


def concept_to_entity_facts(concept: Concept) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="concept",
            entity_id=concept.id,
            properties={
                "name": concept.name,
                "description": concept.description,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in concept.evidence
    )


def decision_to_entity_facts(decision: Decision) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="decision",
            entity_id=decision.id,
            properties={
                "adr_id": decision.adr_id,
                "title": decision.title,
                "status": decision.status,
                "path": decision.path,
                "decided_at": decision.decided_at,
                "context": decision.context,
                "decision": decision.decision,
                "consequences": decision.consequences,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in decision.evidence
    )


def repository_package_facts_to_entity_facts(
    package_facts: RepositoryPackageFacts,
) -> tuple[EntityFact, ...]:
    return tuple(
        EntityFact(
            entity_type="package_metadata",
            entity_id=f"package-metadata:{package_facts.repository_id}",
            properties={
                "repository_id": package_facts.repository_id,
                "language_hints": package_facts.language_hints,
                "framework_hints": package_facts.framework_hints,
                "package_files_analyzed": package_facts.package_files_analyzed,
            },
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=_confidence_from_graph_evidence(evidence),
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in package_facts.evidence
    )


def edge_to_relation_facts(edge: Edge) -> tuple[RelationFact, ...]:
    return tuple(
        RelationFact(
            subject=FactReference(type=edge.source_type, id=edge.source_id),
            relation=edge.relation_type,
            object=FactReference(type=edge.target_type, id=edge.target_id),
            properties={},
            evidence=(_fact_evidence_from_graph_evidence(evidence),),
            confidence=edge.confidence,
            extractor_metadata=ExtractorMetadata(name=evidence.extractor_name),
        )
        for evidence in edge.evidence
    )


def entity_fact_to_active_fact(fact: EntityFact) -> ActiveFact:
    source_file = fact.evidence[0].file_path
    extractor_name = fact.extractor_metadata.name
    return ActiveFact(
        id=deterministic_entity_fact_id(
            extractor_name=extractor_name,
            source_file=source_file,
            entity_type=fact.entity_type,
            entity_id=fact.entity_id,
        ),
        extractor=extractor_name,
        kind=fact.entity_type,
        source_file=_canonical_source_file(source_file),
        identity={
            "entity_type": fact.entity_type,
            "entity_id": fact.entity_id,
        },
        properties=dict(fact.properties),
        evidence=fact.evidence,
        confidence=fact.confidence,
        extractor_version=fact.extractor_metadata.version,
    )


def relation_fact_to_active_fact(fact: RelationFact) -> ActiveFact:
    source_file = fact.evidence[0].file_path
    extractor_name = fact.extractor_metadata.name
    return ActiveFact(
        id=deterministic_relation_fact_id(
            extractor_name=extractor_name,
            source_file=source_file,
            relation=fact.relation,
            subject=fact.subject,
            object=fact.object,
        ),
        extractor=extractor_name,
        kind=fact.relation,
        source_file=_canonical_source_file(source_file),
        identity={
            "subject": asdict(fact.subject),
            "relation": fact.relation,
            "object": asdict(fact.object),
        },
        properties=dict(fact.properties),
        evidence=fact.evidence,
        confidence=fact.confidence,
        extractor_version=fact.extractor_metadata.version,
    )


def deterministic_entity_fact_id(
    *,
    extractor_name: str,
    source_file: str,
    entity_type: str,
    entity_id: str,
) -> str:
    source_file = _canonical_source_file(source_file)
    identity = {
        "entity_type": entity_type,
        "entity_id": entity_id,
    }
    return _deterministic_fact_id(
        category="entity",
        extractor_name=extractor_name,
        source_file=source_file,
        kind=entity_type,
        identity=identity,
        identity_label=entity_id,
    )


def deterministic_relation_fact_id(
    *,
    extractor_name: str,
    source_file: str,
    relation: str,
    subject: FactReference,
    object: FactReference,
) -> str:
    source_file = _canonical_source_file(source_file)
    identity = {
        "subject": asdict(subject),
        "relation": relation,
        "object": asdict(object),
    }
    identity_label = f"{subject.type}:{subject.id}:{relation}:{object.type}:{object.id}"
    return _deterministic_fact_id(
        category="relation",
        extractor_name=extractor_name,
        source_file=source_file,
        kind=relation,
        identity=identity,
        identity_label=identity_label,
    )


def _deterministic_fact_id(
    *,
    category: str,
    extractor_name: str,
    source_file: str,
    kind: str,
    identity: dict[str, Any],
    identity_label: str,
) -> str:
    payload = {
        "category": category,
        "extractor_name": extractor_name,
        "source_file": source_file,
        "kind": kind,
        "identity": identity,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()[:12]
    extractor_slug = slugify_identifier(extractor_name, fallback="extractor")
    source_slug = slugify_identifier(source_file, fallback="source")
    kind_slug = slugify_identifier(kind, fallback="kind")
    identity_slug = slugify_identifier(identity_label, fallback="identity")
    return (
        f"fact:{category}:{extractor_slug}:{source_slug}:"
        f"{kind_slug}:{identity_slug}:{digest}"
    )


def _canonical_source_file(source_file: str) -> str:
    return source_file.replace("\\", "/").removeprefix("./")


def _confidence_from_graph_evidence(evidence: Evidence) -> float:
    if evidence.confidence is not None:
        return evidence.confidence
    return DEFAULT_EXTRACTOR_CONFIDENCE.get(evidence.extractor_name, 1.0)


def _fact_evidence_from_graph_evidence(evidence: Evidence) -> FactEvidence:
    confidence = _confidence_from_graph_evidence(evidence)
    return FactEvidence(
        file_path=evidence.file_path,
        extractor_name=evidence.extractor_name,
        confidence=confidence,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        metadata=dict(evidence.metadata),
    )


def fact_evidence_metadata(fact: ActiveFact, evidence: FactEvidence) -> dict[str, Any]:
    """Retain fact-level uncertainty alongside this provider's evidence metadata."""
    fact_metadata = {
        key: value
        for key, value in fact.properties.items()
        if key
        in {
            "assessment",
            "certainty",
            "unresolved",
            "origin",
            "evidence_origin",
            "validation",
            "conflicted",
            "freshness",
        }
    }
    fact_metadata.update(fact.conflict_metadata)
    metadata = {**fact_metadata, **evidence.metadata}
    if fact_metadata:
        metadata["assessment"] = combine_assessments(
            assess_legacy_evidence(
                source,
                extractor_provenance=(evidence.extractor_name,),
                certainty=source.get("certainty"),
            )
            for source in (fact_metadata, evidence.metadata)
        ).to_dict()
    return metadata
