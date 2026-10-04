from __future__ import annotations

from dataclasses import dataclass

from code_scout.extractors.package_json import RepositoryPackageFacts
from code_scout.models.facts import ActiveFact
from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    File,
    Repository,
    Service,
    Symbol,
)

from .adapters import HarvestAdapterResult
from .facts import (
    active_facts_from_harvest_graph,
    harvest_graph_with_validation_edges,
)
from .model import (
    EvidenceHarvestResult,
    HarvestGraphFragment,
    HarvestWarning,
    LSPValidationInputs,
    PackageFactCounts,
)


@dataclass(frozen=True)
class HarvestAssembly:
    graph_fragment: HarvestGraphFragment = HarvestGraphFragment()
    direct_active_facts: tuple[ActiveFact, ...] = ()
    warnings: tuple[HarvestWarning, ...] = ()
    package_facts: tuple[RepositoryPackageFacts, ...] = ()
    lsp_validation_inputs: LSPValidationInputs | None = None


def package_fact_counts(
    package_facts: tuple[RepositoryPackageFacts, ...],
) -> PackageFactCounts:
    return PackageFactCounts(
        repositories_detected=len(package_facts),
        package_files=sum(fact.package_files_analyzed for fact in package_facts),
    )


def adapter_active_facts(
    *results: HarvestAdapterResult | None,
) -> tuple[ActiveFact, ...]:
    return tuple(
        fact for result in results if result is not None for fact in result.active_facts
    )


def adapter_warnings(
    *results: HarvestAdapterResult | None,
    extra: tuple[HarvestWarning, ...] = (),
) -> tuple[HarvestWarning, ...]:
    return (
        *(
            warning
            for result in results
            if result is not None
            for warning in result.warnings
        ),
        *extra,
    )


def graph_fragment_from_parts(
    *,
    repositories: tuple[Repository, ...] = (),
    files: tuple[File, ...] = (),
    symbols: tuple[Symbol, ...] = (),
    services: tuple[Service, ...] = (),
    contracts: tuple[Contract, ...] = (),
    concepts: tuple[Concept, ...] = (),
    decisions: tuple[Decision, ...] = (),
    edges: tuple[Edge, ...] = (),
) -> HarvestGraphFragment:
    return HarvestGraphFragment(
        repositories=repositories,
        files=files,
        symbols=symbols,
        services=services,
        contracts=contracts,
        concepts=concepts,
        decisions=decisions,
        edges=edges,
    )


def evidence_harvest_result(
    assembly: HarvestAssembly,
    *,
    active_facts: tuple[ActiveFact, ...] | None = None,
) -> EvidenceHarvestResult:
    return EvidenceHarvestResult(
        active_facts=(
            assembly.direct_active_facts if active_facts is None else active_facts
        ),
        graph_fragment=assembly.graph_fragment,
        warnings=assembly.warnings,
        package_counts=package_fact_counts(assembly.package_facts),
        package_facts=assembly.package_facts,
        lsp_validation_inputs=assembly.lsp_validation_inputs,
    )


def active_facts_from_scan_assembly(
    harvest: EvidenceHarvestResult,
    *,
    validation_edges: tuple[Edge, ...],
) -> tuple[ActiveFact, ...]:
    graph_fragment = harvest_graph_with_validation_edges(harvest, validation_edges)
    return active_facts_from_harvest_graph(
        graph_fragment=graph_fragment,
        package_facts=harvest.package_facts,
        direct_active_facts=harvest.active_facts,
    )


def active_facts_from_incremental_assembly(
    assembly: HarvestAssembly,
    *,
    validation_edges: tuple[Edge, ...] = (),
) -> tuple[ActiveFact, ...]:
    graph_fragment = (
        harvest_graph_with_validation_edges(
            EvidenceHarvestResult(
                active_facts=assembly.direct_active_facts,
                graph_fragment=assembly.graph_fragment,
                package_facts=assembly.package_facts,
            ),
            validation_edges,
        )
        if validation_edges
        else assembly.graph_fragment
    )
    return active_facts_from_harvest_graph(
        graph_fragment=graph_fragment,
        package_facts=assembly.package_facts,
        direct_active_facts=assembly.direct_active_facts,
    )
