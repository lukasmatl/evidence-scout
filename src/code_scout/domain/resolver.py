from __future__ import annotations

from typing import Any

from code_scout.domain.normalize import NormalizedGraphFacts, normalize_graph_facts
from code_scout.models.facts import ActiveFact, FactEvidence, fact_evidence_metadata
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


def resolve_active_facts(facts: tuple[ActiveFact, ...]) -> NormalizedGraphFacts:
    """Resolve active facts into the normalized graph shape."""
    repositories: list[Repository] = []
    files: list[File] = []
    symbols: list[Symbol] = []
    services: list[Service] = []
    contracts: list[Contract] = []
    concepts: list[Concept] = []
    decisions: list[Decision] = []
    edges: list[Edge] = []

    for fact in facts:
        if _is_relation_fact(fact):
            edges.append(_edge_from_fact(fact))
            continue

        entity_type = _string_value(fact.identity, "entity_type")
        if entity_type == "repository":
            repositories.append(_repository_from_fact(fact))
        elif entity_type == "file":
            files.append(_file_from_fact(fact))
        elif entity_type == "symbol":
            symbols.append(_symbol_from_fact(fact))
        elif entity_type == "service":
            services.append(_service_from_fact(fact))
        elif entity_type == "contract":
            contracts.append(_contract_from_fact(fact))
        elif entity_type == "concept":
            concepts.append(_concept_from_fact(fact))
        elif entity_type == "decision":
            decisions.append(_decision_from_fact(fact))

    return normalize_graph_facts(
        repositories=tuple(repositories),
        files=tuple(files),
        symbols=tuple(symbols),
        services=tuple(services),
        contracts=tuple(contracts),
        concepts=tuple(concepts),
        decisions=tuple(decisions),
        edges=tuple(edges),
    )


def _repository_from_fact(fact: ActiveFact) -> Repository:
    return Repository(
        id=_required_identity(fact, "entity_id"),
        name=_string_property(fact, "name") or _required_identity(fact, "entity_id"),
        path=_string_property(fact, "path") or "",
        remote_url=_string_property(fact, "remote_url"),
        default_branch=_string_property(fact, "default_branch"),
        language_hints=_string_tuple_property(fact, "language_hints"),
        framework_hints=_string_tuple_property(fact, "framework_hints"),
        module_hints=_string_tuple_property(fact, "module_hints"),
        evidence=_evidence_from_fact(fact),
    )


def _file_from_fact(fact: ActiveFact) -> File:
    return File(
        id=_required_identity(fact, "entity_id"),
        repository_id=_string_property(fact, "repository_id") or "",
        path=_string_property(fact, "path") or _required_identity(fact, "entity_id"),
        kind=_string_property(fact, "kind") or "source",
        extension=_string_property(fact, "extension") or "",
        language=_string_property(fact, "language"),
        content_hash=_string_property(fact, "content_hash"),
        modified_at=_string_property(fact, "modified_at"),
        evidence=_evidence_from_fact(fact),
    )


def _symbol_from_fact(fact: ActiveFact) -> Symbol:
    return Symbol(
        id=_required_identity(fact, "entity_id"),
        file_id=_string_property(fact, "file_id") or "",
        name=_string_property(fact, "name") or _required_identity(fact, "entity_id"),
        kind=_string_property(fact, "kind") or "",
        qualified_name=_string_property(fact, "qualified_name"),
        start_line=_int_property(fact, "start_line"),
        end_line=_int_property(fact, "end_line"),
        evidence=_evidence_from_fact(fact),
    )


def _service_from_fact(fact: ActiveFact) -> Service:
    return Service(
        id=_required_identity(fact, "entity_id"),
        name=_string_property(fact, "name") or _required_identity(fact, "entity_id"),
        repository_id=_string_property(fact, "repository_id"),
        description=_string_property(fact, "description"),
        aliases=_string_tuple_property(fact, "aliases"),
        evidence=_evidence_from_fact(fact),
    )


def _contract_from_fact(fact: ActiveFact) -> Contract:
    return Contract(
        id=_required_identity(fact, "entity_id"),
        name=_string_property(fact, "name") or _required_identity(fact, "entity_id"),
        kind=_string_property(fact, "kind") or "unknown",
        path=_string_property(fact, "path"),
        version=_string_property(fact, "version"),
        method=_string_property(fact, "method"),
        producer_hint=_string_property(fact, "producer_hint"),
        operation_id=_string_property(fact, "operation_id"),
        request_dtos=_string_tuple_property(fact, "request_dtos"),
        response_dtos=_string_tuple_property(fact, "response_dtos"),
        evidence=_evidence_from_fact(fact),
    )


def _concept_from_fact(fact: ActiveFact) -> Concept:
    return Concept(
        id=_required_identity(fact, "entity_id"),
        name=_string_property(fact, "name") or _required_identity(fact, "entity_id"),
        description=_string_property(fact, "description"),
        evidence=_evidence_from_fact(fact),
    )


def _decision_from_fact(fact: ActiveFact) -> Decision:
    return Decision(
        id=_required_identity(fact, "entity_id"),
        title=_string_property(fact, "title") or _required_identity(fact, "entity_id"),
        status=_string_property(fact, "status") or "unknown",
        adr_id=_string_property(fact, "adr_id"),
        path=_string_property(fact, "path"),
        decided_at=_string_property(fact, "decided_at"),
        context=_string_property(fact, "context"),
        decision=_string_property(fact, "decision"),
        consequences=_string_property(fact, "consequences"),
        evidence=_evidence_from_fact(fact),
    )


def _edge_from_fact(fact: ActiveFact) -> Edge:
    subject = fact.identity["subject"]
    object_ = fact.identity["object"]
    return Edge(
        source_type=subject["type"],
        source_id=subject["id"],
        target_type=object_["type"],
        target_id=object_["id"],
        relation_type=_required_identity(fact, "relation"),
        confidence=fact.confidence,
        evidence=_evidence_from_fact(fact),
    )


def _is_relation_fact(fact: ActiveFact) -> bool:
    return {"subject", "relation", "object"} <= fact.identity.keys()


def _evidence_from_fact(fact: ActiveFact) -> tuple[Evidence, ...]:
    evidence = fact.evidence or (
        FactEvidence(
            fact.source_file,
            fact.extractor,
            fact.confidence,
        ),
    )
    return tuple(_evidence_from_fact_evidence(item, fact) for item in evidence)


def _evidence_from_fact_evidence(evidence: FactEvidence, fact: ActiveFact) -> Evidence:
    return Evidence(
        file_path=evidence.file_path,
        extractor_name=evidence.extractor_name,
        confidence=evidence.confidence,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        metadata=fact_evidence_metadata(fact, evidence),
    )


def _required_identity(fact: ActiveFact, key: str) -> str:
    value = _string_value(fact.identity, key)
    if value is None:
        msg = f"Active fact {fact.id!r} is missing string identity field {key!r}"
        raise ValueError(msg)
    return value


def _string_property(fact: ActiveFact, key: str) -> str | None:
    return _string_value(fact.properties, key)


def _int_property(fact: ActiveFact, key: str) -> int | None:
    value = fact.properties.get(key)
    return value if isinstance(value, int) else None


def _string_tuple_property(fact: ActiveFact, key: str) -> tuple[str, ...]:
    value = fact.properties.get(key)
    if isinstance(value, tuple):
        return tuple(item for item in value if isinstance(item, str))
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def _string_value(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None
