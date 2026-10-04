from __future__ import annotations

from dataclasses import dataclass

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import (
    ExtractorRegistry,
    RepositoryFrameworkHint,
    SymbolExtraction,
)
from code_scout.extractors.adr import adr_active_facts, adr_concept_links
from code_scout.extractors.asyncapi import asyncapi_active_facts
from code_scout.extractors.byond import ByondExtractor
from code_scout.extractors.docs import docs_active_facts
from code_scout.extractors.dotnet import DotnetExtractor
from code_scout.extractors.graphify import (
    GraphifyAdapter,
    GraphifyExtraction,
    GraphifyWarning,
    GraphifyWatchAdapter,
    extract_graphify,
    extract_graphify_watch,
)
from code_scout.extractors.heuristic import HeuristicExtractor
from code_scout.extractors.infra import infra_active_facts
from code_scout.extractors.lsp import (
    TypeScriptLSPAdapter,
    extract_typescript_lsp,
)
from code_scout.extractors.openapi import (
    openapi_active_facts,
    openapi_concept_links,
    openapi_contract_edges,
    openapi_contracts,
)
from code_scout.extractors.package_json import (
    RepositoryPackageFacts,
    infer_repository_package_facts,
    package_metadata_active_facts,
)
from code_scout.extractors.treesitter import TreeSitterExtractor
from code_scout.models.facts import ActiveFact
from code_scout.models.graph import Edge, Symbol
from code_scout.models.workspace import Workspace
from code_scout.workspace.config import ExtractorEntryConfig, WorkspaceConfig

from .model import HarvestGraphFragment, HarvestWarning, LSPValidationInputs


@dataclass(frozen=True)
class HarvestAdapterResult:
    active_facts: tuple[ActiveFact, ...] = ()
    graph_fragment: HarvestGraphFragment = HarvestGraphFragment()
    warnings: tuple[HarvestWarning, ...] = ()
    package_facts: tuple[RepositoryPackageFacts, ...] = ()
    lsp_validation_inputs: LSPValidationInputs | None = None
    framework_hints: tuple[RepositoryFrameworkHint, ...] = ()


def harvest_package_metadata_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
) -> HarvestAdapterResult:
    return HarvestAdapterResult(
        active_facts=package_metadata_active_facts(discovered_files),
        package_facts=infer_repository_package_facts(discovered_files),
    )


def harvest_openapi_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...],
    include_active_fact_warnings: bool,
) -> HarvestAdapterResult:
    contracts, contract_warnings = openapi_contracts(
        discovered_files,
        configured_paths=configured_paths,
    )
    active_facts, active_fact_warnings = openapi_active_facts(
        discovered_files,
        configured_paths=configured_paths,
    )
    links = openapi_concept_links(
        discovered_files,
        configured_paths=configured_paths,
    )
    return HarvestAdapterResult(
        active_facts=active_facts,
        graph_fragment=HarvestGraphFragment(
            contracts=contracts,
            concepts=tuple(link.concept for link in links),
            edges=(
                *openapi_contract_edges(
                    discovered_files,
                    configured_paths=configured_paths,
                ),
                *(link.edge for link in links),
            ),
        ),
        warnings=(
            *contract_warnings,
            *(active_fact_warnings if include_active_fact_warnings else ()),
        ),
    )


def harvest_asyncapi_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...],
) -> HarvestAdapterResult:
    active_facts, warnings = asyncapi_active_facts(
        discovered_files,
        configured_paths=configured_paths,
    )
    return HarvestAdapterResult(active_facts=active_facts, warnings=warnings)


def harvest_adr_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    decision_edges: tuple[Edge, ...],
) -> HarvestAdapterResult:
    links = adr_concept_links(discovered_files)
    return HarvestAdapterResult(
        active_facts=adr_active_facts(discovered_files),
        graph_fragment=HarvestGraphFragment(
            concepts=tuple(link.concept for link in links),
            edges=(*decision_edges, *(link.edge for link in links)),
        ),
    )


def harvest_docs_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
) -> HarvestAdapterResult:
    return HarvestAdapterResult(active_facts=docs_active_facts(discovered_files))


def harvest_infra_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...],
) -> HarvestAdapterResult:
    return HarvestAdapterResult(
        active_facts=infra_active_facts(
            discovered_files,
            configured_paths=configured_paths,
        ),
    )


def harvest_symbol_extraction_adapter(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    selected_extractors: tuple[str, ...],
) -> HarvestAdapterResult:
    if "symbol_extraction" not in selected_extractors:
        return HarvestAdapterResult()

    extraction = ExtractorRegistry(
        tree_sitter_enabled="tree_sitter" in selected_extractors,
        heuristic_enabled=True,
        dotnet_enabled=True,
        byond_enabled=True,
        tree_sitter_extractor=TreeSitterExtractor(),
        heuristic_extractor=HeuristicExtractor(),
        dotnet_extractor=DotnetExtractor(),
        byond_extractor=ByondExtractor(),
    ).extract(discovered_files)
    return harvest_symbol_extraction_result(extraction)


def harvest_symbol_extraction_result(
    extraction: SymbolExtraction,
) -> HarvestAdapterResult:
    return HarvestAdapterResult(
        graph_fragment=HarvestGraphFragment(
            symbols=extraction.symbols,
            services=extraction.services,
            contracts=extraction.contracts,
            edges=extraction.edges,
        ),
        framework_hints=extraction.framework_hints,
    )


def harvest_graphify_adapter(
    workspace: Workspace,
    config: ExtractorEntryConfig,
    *,
    graphify_adapter: GraphifyAdapter | None,
    graphify_watch_adapter: GraphifyWatchAdapter | None,
) -> HarvestAdapterResult:
    if config.watch is None:
        extraction = extract_graphify(
            workspace,
            config,
            adapter=graphify_adapter,
        )
        warnings: tuple[GraphifyWarning, ...] = ()
    else:
        extraction, warnings = extract_graphify_watch(
            workspace,
            config,
            adapter=graphify_watch_adapter,
        )
    return harvest_graphify_result(extraction, warnings=warnings)


def harvest_graphify_result(
    extraction: GraphifyExtraction | None,
    *,
    warnings: tuple[GraphifyWarning, ...] = (),
) -> HarvestAdapterResult:
    if extraction is None:
        return HarvestAdapterResult(warnings=warnings)
    return HarvestAdapterResult(
        graph_fragment=HarvestGraphFragment(
            symbols=extraction.symbols,
            services=extraction.services,
            contracts=extraction.contracts,
            concepts=extraction.concepts,
            decisions=extraction.decisions,
            edges=extraction.edges,
        ),
        warnings=warnings,
    )


def harvest_lsp_adapter(
    workspace: Workspace,
    discovered_files: tuple[DiscoveredFile, ...],
    config: WorkspaceConfig,
    *,
    lsp_adapter: TypeScriptLSPAdapter | None,
    authoritative_symbols: tuple[Symbol, ...],
    authoritative_edges: tuple[Edge, ...],
) -> HarvestAdapterResult:
    extraction = extract_typescript_lsp(
        workspace,
        discovered_files,
        config.extractors.lsp,
        adapter=lsp_adapter,
    )
    return HarvestAdapterResult(
        lsp_validation_inputs=LSPValidationInputs(
            runs=extraction.runs,
            authoritative_symbols=authoritative_symbols,
            authoritative_edges=authoritative_edges,
        ),
    )
