from __future__ import annotations

from dataclasses import replace

from code_scout.domain.normalize import normalized_contract_id
from code_scout.extractors.package_json import RepositoryPackageFacts
from code_scout.models.facts import (
    ActiveFact,
    concept_to_entity_facts,
    contract_to_entity_facts,
    decision_to_entity_facts,
    edge_to_relation_facts,
    entity_fact_to_active_fact,
    file_to_entity_facts,
    relation_fact_to_active_fact,
    repository_package_facts_to_entity_facts,
    repository_to_entity_facts,
    service_to_entity_facts,
    symbol_to_entity_facts,
)
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
from code_scout.utils.active_facts import merge_active_facts_by_id

from .model import EvidenceHarvestResult, HarvestGraphFragment


def active_facts_from_harvest_graph(
    *,
    graph_fragment: HarvestGraphFragment,
    package_facts: tuple[RepositoryPackageFacts, ...],
    direct_active_facts: tuple[ActiveFact, ...] = (),
) -> tuple[ActiveFact, ...]:
    edges = edges_with_normalized_contract_ids(
        graph_fragment.edges,
        graph_fragment.contracts,
    )
    symbols = symbols_with_inferred_evidence(graph_fragment.symbols, edges)
    concepts = concepts_with_inferred_evidence(graph_fragment.concepts, edges)
    return active_facts_from_graph_objects(
        repositories=graph_fragment.repositories,
        files=graph_fragment.files,
        package_facts=package_facts,
        symbols=symbols,
        services=graph_fragment.services,
        contracts=graph_fragment.contracts,
        concepts=concepts,
        decisions=graph_fragment.decisions,
        edges=edges,
        direct_active_facts=direct_active_facts,
    )


def active_facts_from_graph_objects(
    *,
    repositories: tuple[Repository, ...] = (),
    files: tuple[File, ...] = (),
    package_facts: tuple[RepositoryPackageFacts, ...],
    symbols: tuple[Symbol, ...] = (),
    services: tuple[Service, ...] = (),
    contracts: tuple[Contract, ...],
    concepts: tuple[Concept, ...],
    decisions: tuple[Decision, ...],
    edges: tuple[Edge, ...],
    direct_active_facts: tuple[ActiveFact, ...] = (),
) -> tuple[ActiveFact, ...]:
    entity_facts = (
        *(
            fact
            for repository in repositories
            for fact in repository_to_entity_facts(repository)
        ),
        *(fact for file in files for fact in file_to_entity_facts(file)),
        *(
            fact
            for package_fact in package_facts
            for fact in repository_package_facts_to_entity_facts(package_fact)
        ),
        *(fact for symbol in symbols for fact in symbol_to_entity_facts(symbol)),
        *(fact for service in services for fact in service_to_entity_facts(service)),
        *(
            fact
            for contract in contracts
            for fact in contract_to_entity_facts(contract)
        ),
        *(fact for concept in concepts for fact in concept_to_entity_facts(concept)),
        *(
            fact
            for decision in decisions
            for fact in decision_to_entity_facts(decision)
        ),
    )
    relation_facts = tuple(
        fact for edge in edges for fact in edge_to_relation_facts(edge)
    )
    return merge_active_facts_by_id(
        *direct_active_facts,
        *(entity_fact_to_active_fact(fact) for fact in entity_facts),
        *(relation_fact_to_active_fact(fact) for fact in relation_facts),
    )


def harvest_graph_with_validation_edges(
    harvest: EvidenceHarvestResult,
    validation_edges: tuple[Edge, ...],
) -> HarvestGraphFragment:
    fragment = harvest.graph_fragment
    edges = edges_with_normalized_contract_ids(
        (*fragment.edges, *validation_edges),
        fragment.contracts,
    )
    return HarvestGraphFragment(
        repositories=fragment.repositories,
        files=fragment.files,
        symbols=symbols_with_inferred_evidence(fragment.symbols, edges),
        services=fragment.services,
        contracts=fragment.contracts,
        concepts=concepts_with_inferred_evidence(fragment.concepts, edges),
        decisions=fragment.decisions,
        edges=edges,
    )


def edges_with_normalized_contract_ids(
    edges: tuple[Edge, ...],
    contracts: tuple[Contract, ...],
) -> tuple[Edge, ...]:
    contract_id_map = {
        contract.id: normalized_contract_id(contract) for contract in contracts
    }
    return tuple(
        edge_with_normalized_contract_ids(edge, contract_id_map) for edge in edges
    )


def edge_with_normalized_contract_ids(
    edge: Edge,
    contract_id_map: dict[str, str],
) -> Edge:
    source_id = (
        contract_id_map.get(edge.source_id, edge.source_id)
        if edge.source_type == "contract"
        else edge.source_id
    )
    target_id = (
        contract_id_map.get(edge.target_id, edge.target_id)
        if edge.target_type == "contract"
        else edge.target_id
    )
    if source_id == edge.source_id and target_id == edge.target_id:
        return edge
    return replace(edge, source_id=source_id, target_id=target_id)


def symbols_with_inferred_evidence(
    symbols: tuple[Symbol, ...],
    edges: tuple[Edge, ...],
) -> tuple[Symbol, ...]:
    declares_evidence_by_symbol_id: dict[str, tuple[Evidence, ...]] = {}
    for edge in edges:
        if edge.relation_type != "declares_symbol" or edge.target_type != "symbol":
            continue
        declares_evidence_by_symbol_id[edge.target_id] = (
            *declares_evidence_by_symbol_id.get(edge.target_id, ()),
            *edge.evidence,
        )
    return tuple(
        symbol
        if symbol.evidence
        else replace(
            symbol,
            evidence=declares_evidence_by_symbol_id.get(symbol.id, ())
            or _symbol_compatibility_evidence(symbol),
        )
        for symbol in symbols
    )


def concepts_with_inferred_evidence(
    concepts: tuple[Concept, ...],
    edges: tuple[Edge, ...],
) -> tuple[Concept, ...]:
    evidence_by_concept_id: dict[str, tuple[Evidence, ...]] = {}
    for edge in edges:
        for node_type, node_id in (
            (edge.source_type, edge.source_id),
            (edge.target_type, edge.target_id),
        ):
            if node_type != "concept":
                continue
            evidence_by_concept_id[node_id] = (
                *evidence_by_concept_id.get(node_id, ()),
                *edge.evidence,
            )
    return tuple(
        concept
        if concept.evidence
        else replace(concept, evidence=evidence_by_concept_id.get(concept.id, ()))
        for concept in concepts
    )


def _symbol_compatibility_evidence(symbol: Symbol) -> tuple[Evidence, ...]:
    file_path = _file_path_from_file_id(symbol.file_id)
    if file_path is None:
        return ()
    return (
        Evidence(
            file_path=file_path,
            extractor_name=_extractor_name_from_symbol_id(symbol.id),
            confidence=0.85,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            metadata={"compatibility_source": "symbol_identity"},
        ),
    )


def _file_path_from_file_id(file_id: str) -> str | None:
    parts = file_id.split(":", maxsplit=2)
    if len(parts) != 3 or parts[0] != "file":
        return None
    return parts[2]


def _extractor_name_from_symbol_id(symbol_id: str) -> str:
    parts = symbol_id.split(":")
    if len(parts) >= 5 and parts[-2] in {"graphify", "lsp", "tree_sitter"}:
        return parts[-2]
    return "symbol_extraction"
