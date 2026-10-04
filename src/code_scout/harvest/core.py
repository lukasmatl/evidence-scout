from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

from code_scout.core.errors import ConfigError
from code_scout.discovery import DiscoveredFile
from code_scout.domain.normalize import NormalizedGraphFacts
from code_scout.extractors import (
    RepositoryFrameworkHint,
    SymbolExtraction,
)
from code_scout.extractors.frontend_api import (
    frontend_api_contracts,
    frontend_api_edges,
)
from code_scout.extractors.graphify import (
    GraphifyAdapter,
    GraphifyExtraction,
    GraphifyWarning,
    GraphifyWatchAdapter,
)
from code_scout.extractors.lsp import (
    TypeScriptLSPAdapter,
    typescript_lsp_validation_edges,
)
from code_scout.extractors.package_json import (
    RepositoryPackageFacts,
)
from code_scout.models.facts import ActiveFact
from code_scout.models.graph import (
    Edge,
    Evidence,
    File,
    Repository,
    Service,
    Symbol,
)
from code_scout.models.workspace import Workspace
from code_scout.workspace.config import ExtractorEntryConfig, WorkspaceConfig
from code_scout.workspace.paths import (
    GRAPHIFY_INPUT_PATH,
    GRAPHIFY_JSON_PATH,
    GRAPHIFY_OUTPUT_DIRECTORY,
)

from .adapters import (
    harvest_adr_adapter,
    harvest_asyncapi_adapter,
    harvest_docs_adapter,
    harvest_graphify_adapter,
    harvest_infra_adapter,
    harvest_lsp_adapter,
    harvest_openapi_adapter,
    harvest_package_metadata_adapter,
    harvest_symbol_extraction_adapter,
)
from .assembly import (
    HarvestAssembly,
    active_facts_from_incremental_assembly,
    active_facts_from_scan_assembly,
    adapter_active_facts,
    adapter_warnings,
    evidence_harvest_result,
    graph_fragment_from_parts,
)
from .assembly import (
    package_fact_counts as package_fact_counts,
)
from .facts import (
    concepts_with_inferred_evidence,
    edges_with_normalized_contract_ids,
    symbols_with_inferred_evidence,
)
from .model import EvidenceHarvestResult, HarvestGraphFragment

CONVENTIONAL_GRAPHIFY_JSON_PATHS = (
    GRAPHIFY_JSON_PATH,
    GRAPHIFY_INPUT_PATH,
    "graphify-out/graph.json",
    "graphify.json",
)

MODULE_HINT_ORDER = (
    "backend",
    "frontend",
    "functions",
    "domain",
    "infrastructure",
    "tests",
)


def graphify_source_configured(
    workspace: Workspace,
    config: WorkspaceConfig,
) -> bool:
    graphify_config = config.extractors.graphify
    return (
        graphify_config.json_path is not None
        or bool(graphify_config.command)
        or graphify_config.watch is not None
        or _conventional_graphify_json_path(workspace) is not None
        or _default_graphify_command_available()
    )


def harvest_scan_evidence(
    *,
    workspace: Workspace,
    config: WorkspaceConfig,
    discovered_files: tuple[DiscoveredFile, ...],
    selected_extractors: tuple[str, ...],
    lsp_adapter: TypeScriptLSPAdapter | None = None,
    graphify_adapter: GraphifyAdapter | None = None,
    graphify_watch_adapter: GraphifyWatchAdapter | None = None,
    graphify_required: bool = False,
) -> EvidenceHarvestResult:
    package_result = (
        harvest_package_metadata_adapter(discovered_files)
        if "package_metadata" in selected_extractors
        else None
    )
    package_facts = package_result.package_facts if package_result is not None else ()
    package_facts_by_repository_id = {
        fact.repository_id: fact for fact in package_facts
    }
    openapi_result = (
        harvest_openapi_adapter(
            discovered_files,
            configured_paths=workspace.openapi_paths,
            include_active_fact_warnings=False,
        )
        if "openapi" in selected_extractors
        else None
    )
    asyncapi_result = (
        harvest_asyncapi_adapter(
            discovered_files,
            configured_paths=workspace.asyncapi_paths,
        )
        if "asyncapi" in selected_extractors
        else None
    )
    frontend_api_contract_nodes = (
        frontend_api_contracts(discovered_files)
        if "symbol_extraction" in selected_extractors
        else ()
    )
    frontend_api_graph_edges = (
        frontend_api_edges(discovered_files)
        if "symbol_extraction" in selected_extractors
        else ()
    )
    adr_edges = (
        _adr_decision_edges(discovered_files) if "adr" in selected_extractors else ()
    )
    adr_result = (
        harvest_adr_adapter(discovered_files, decision_edges=adr_edges)
        if "adr" in selected_extractors
        else None
    )
    docs_result = (
        harvest_docs_adapter(discovered_files)
        if "docs" in selected_extractors
        else None
    )
    infra_result = (
        harvest_infra_adapter(
            discovered_files,
            configured_paths=workspace.infra_paths,
        )
        if "infra" in selected_extractors
        else None
    )
    graphify_extraction, graphify_warnings = _graphify_extraction_for_scan(
        workspace,
        config,
        selected_extractors,
        discovered_files=discovered_files,
        graphify_required=graphify_required,
        graphify_adapter=graphify_adapter,
        graphify_watch_adapter=graphify_watch_adapter,
    )
    graphify_extraction = _filter_graphify_extraction_to_discovered_files(
        graphify_extraction,
        discovered_files,
    )
    symbol_extraction = _symbol_extraction(discovered_files, selected_extractors)
    symbol_extraction = _suppress_tree_sitter_facts_covered_by_graphify(
        symbol_extraction,
        discovered_files=discovered_files,
        graphify_symbols=(
            graphify_extraction.symbols if graphify_extraction is not None else ()
        ),
        graphify_services=(
            graphify_extraction.services if graphify_extraction is not None else ()
        ),
        graphify_edges=(
            graphify_extraction.edges if graphify_extraction is not None else ()
        ),
    )
    symbol_framework_hints_by_repository_id = _framework_hints_by_repository_id(
        symbol_extraction.framework_hints,
    )
    concepts = (
        *(openapi_result.graph_fragment.concepts if openapi_result is not None else ()),
        *(adr_result.graph_fragment.concepts if adr_result is not None else ()),
    )
    edges = (
        *(openapi_result.graph_fragment.edges if openapi_result is not None else ()),
        *frontend_api_graph_edges,
        *symbol_extraction.edges,
        *(graphify_extraction.edges if graphify_extraction is not None else ()),
        *(adr_result.graph_fragment.edges if adr_result is not None else ()),
    )
    symbols = (
        *symbol_extraction.symbols,
        *(graphify_extraction.symbols if graphify_extraction is not None else ()),
    )
    services = (
        *symbol_extraction.services,
        *(graphify_extraction.services if graphify_extraction is not None else ()),
    )
    contracts = (
        *(
            openapi_result.graph_fragment.contracts
            if openapi_result is not None
            else ()
        ),
        *frontend_api_contract_nodes,
        *symbol_extraction.contracts,
        *(graphify_extraction.contracts if graphify_extraction is not None else ()),
    )
    graph_concepts = (
        *concepts,
        *(graphify_extraction.concepts if graphify_extraction is not None else ()),
    )
    decisions = (
        *(graphify_extraction.decisions if graphify_extraction is not None else ()),
    )
    graph_fragment = graph_fragment_from_parts(
        repositories=_graph_repositories(
            workspace=workspace,
            discovered_files=discovered_files,
            package_facts_by_repository_id=package_facts_by_repository_id,
            symbol_framework_hints_by_repository_id=(
                symbol_framework_hints_by_repository_id
            ),
        ),
        files=tuple(
            _graph_file(discovered_file) for discovered_file in discovered_files
        ),
        symbols=symbols,
        services=services,
        contracts=contracts,
        concepts=graph_concepts,
        decisions=decisions,
        edges=edges,
    )
    lsp_validation_inputs = (
        harvest_lsp_adapter(
            workspace,
            discovered_files,
            config,
            lsp_adapter=lsp_adapter,
            authoritative_symbols=symbols,
            authoritative_edges=edges,
        ).lsp_validation_inputs
        if "lsp" in selected_extractors
        else None
    )
    return evidence_harvest_result(
        HarvestAssembly(
            graph_fragment=graph_fragment,
            direct_active_facts=adapter_active_facts(
                package_result,
                openapi_result,
                asyncapi_result,
                adr_result,
                docs_result,
                infra_result,
            ),
            warnings=adapter_warnings(
                openapi_result,
                asyncapi_result,
                extra=graphify_warnings,
            ),
            package_facts=package_facts,
            lsp_validation_inputs=lsp_validation_inputs,
        ),
    )


def active_facts_from_scan_harvest(
    harvest: EvidenceHarvestResult,
    *,
    existing_resolved_graph_facts: NormalizedGraphFacts,
) -> tuple[ActiveFact, ...]:
    lsp_validation_edges = (
        typescript_lsp_validation_edges(
            harvest.lsp_validation_inputs.runs,
            authoritative_symbols=(
                *harvest.lsp_validation_inputs.authoritative_symbols,
                *existing_resolved_graph_facts.symbols,
            ),
            authoritative_edges=(
                *harvest.lsp_validation_inputs.authoritative_edges,
                *existing_resolved_graph_facts.edges,
            ),
        )
        if harvest.lsp_validation_inputs is not None
        else ()
    )
    return active_facts_from_scan_assembly(
        harvest,
        validation_edges=lsp_validation_edges,
    )


def harvest_incremental_evidence(
    *,
    workspace: Workspace,
    config: WorkspaceConfig,
    discovered_files: tuple[DiscoveredFile, ...],
    changed_discovered_files: tuple[DiscoveredFile, ...],
    selected_extractors: tuple[str, ...],
    lsp_adapter: TypeScriptLSPAdapter | None = None,
    graphify_adapter: GraphifyAdapter | None = None,
    graphify_watch_adapter: GraphifyWatchAdapter | None = None,
) -> EvidenceHarvestResult:
    package_result = (
        harvest_package_metadata_adapter(changed_discovered_files)
        if "package_metadata" in selected_extractors
        else None
    )
    package_facts = package_result.package_facts if package_result is not None else ()
    openapi_result = (
        harvest_openapi_adapter(
            changed_discovered_files,
            configured_paths=workspace.openapi_paths,
            include_active_fact_warnings=True,
        )
        if "openapi" in selected_extractors
        else None
    )
    asyncapi_result = (
        harvest_asyncapi_adapter(
            changed_discovered_files,
            configured_paths=workspace.asyncapi_paths,
        )
        if "asyncapi" in selected_extractors
        else None
    )
    adr_edges = (
        _adr_decision_edges(changed_discovered_files)
        if "adr" in selected_extractors
        else ()
    )
    adr_result = (
        harvest_adr_adapter(changed_discovered_files, decision_edges=adr_edges)
        if "adr" in selected_extractors
        else None
    )
    docs_result = (
        harvest_docs_adapter(changed_discovered_files)
        if "docs" in selected_extractors
        else None
    )
    infra_result = (
        harvest_infra_adapter(
            changed_discovered_files,
            configured_paths=workspace.infra_paths,
        )
        if "infra" in selected_extractors
        else None
    )
    graphify_extraction, graphify_warnings = _graphify_extraction_for_scan(
        workspace,
        config,
        selected_extractors,
        discovered_files=discovered_files,
        graphify_required=False,
        graphify_adapter=graphify_adapter,
        graphify_watch_adapter=graphify_watch_adapter,
    )
    graphify_extraction = _filter_graphify_extraction_to_discovered_files(
        graphify_extraction,
        discovered_files,
    )
    symbol_extraction = _symbol_extraction(
        changed_discovered_files,
        selected_extractors,
    )
    symbol_extraction = _suppress_tree_sitter_facts_covered_by_graphify(
        symbol_extraction,
        discovered_files=discovered_files,
        graphify_symbols=(
            graphify_extraction.symbols if graphify_extraction is not None else ()
        ),
        graphify_services=(
            graphify_extraction.services if graphify_extraction is not None else ()
        ),
        graphify_edges=(
            graphify_extraction.edges if graphify_extraction is not None else ()
        ),
    )
    lsp_result = (
        harvest_lsp_adapter(
            workspace,
            changed_discovered_files,
            config,
            lsp_adapter=lsp_adapter,
            authoritative_symbols=(
                *symbol_extraction.symbols,
                *(graphify_extraction.symbols if graphify_extraction else ()),
            ),
            authoritative_edges=(
                *symbol_extraction.edges,
                *(graphify_extraction.edges if graphify_extraction else ()),
            ),
        )
        if "lsp" in selected_extractors
        else None
    )
    lsp_validation_edges = (
        typescript_lsp_validation_edges(
            lsp_result.lsp_validation_inputs.runs,
            authoritative_symbols=(
                *lsp_result.lsp_validation_inputs.authoritative_symbols,
            ),
            authoritative_edges=(
                *lsp_result.lsp_validation_inputs.authoritative_edges,
            ),
        )
        if lsp_result is not None and lsp_result.lsp_validation_inputs is not None
        else ()
    )
    frontend_contract_nodes = (
        frontend_api_contracts(changed_discovered_files)
        if "symbol_extraction" in selected_extractors
        else ()
    )
    frontend_graph_edges = (
        frontend_api_edges(changed_discovered_files)
        if "symbol_extraction" in selected_extractors
        else ()
    )
    contracts = (
        *(
            openapi_result.graph_fragment.contracts
            if openapi_result is not None
            else ()
        ),
        *frontend_contract_nodes,
        *symbol_extraction.contracts,
        *(graphify_extraction.contracts if graphify_extraction is not None else ()),
    )
    edges = edges_with_normalized_contract_ids(
        (
            *(
                openapi_result.graph_fragment.edges
                if openapi_result is not None
                else ()
            ),
            *frontend_graph_edges,
            *symbol_extraction.edges,
            *(graphify_extraction.edges if graphify_extraction is not None else ()),
            *(adr_result.graph_fragment.edges if adr_result is not None else ()),
            *lsp_validation_edges,
        ),
        contracts,
    )
    symbols = symbols_with_inferred_evidence(
        (
            *symbol_extraction.symbols,
            *(graphify_extraction.symbols if graphify_extraction is not None else ()),
        ),
        edges,
    )
    concepts = concepts_with_inferred_evidence(
        (
            *(
                openapi_result.graph_fragment.concepts
                if openapi_result is not None
                else ()
            ),
            *(adr_result.graph_fragment.concepts if adr_result is not None else ()),
            *(graphify_extraction.concepts if graphify_extraction is not None else ()),
        ),
        edges,
    )
    graph_repositories = _graph_repositories(
        workspace=workspace,
        discovered_files=discovered_files,
        package_facts_by_repository_id={
            fact.repository_id: fact for fact in package_facts
        },
        symbol_framework_hints_by_repository_id={},
    )
    graph_fragment = graph_fragment_from_parts(
        repositories=graph_repositories,
        files=tuple(_graph_file(file) for file in changed_discovered_files),
        symbols=symbols,
        services=(
            *symbol_extraction.services,
            *(graphify_extraction.services if graphify_extraction is not None else ()),
        ),
        contracts=contracts,
        concepts=concepts,
        decisions=(
            *(graphify_extraction.decisions if graphify_extraction is not None else ()),
        ),
        edges=edges,
    )
    assembly = HarvestAssembly(
        graph_fragment=graph_fragment,
        direct_active_facts=adapter_active_facts(
            package_result,
            openapi_result,
            asyncapi_result,
            adr_result,
            docs_result,
            infra_result,
        ),
        warnings=adapter_warnings(
            openapi_result,
            asyncapi_result,
            extra=graphify_warnings,
        ),
        package_facts=package_facts,
    )
    return evidence_harvest_result(
        assembly,
        active_facts=active_facts_from_incremental_assembly(assembly),
    )


def _graphify_extraction_for_scan(
    workspace: Workspace,
    config: WorkspaceConfig,
    selected_extractors: tuple[str, ...],
    *,
    discovered_files: tuple[DiscoveredFile, ...],
    graphify_required: bool,
    graphify_adapter: GraphifyAdapter | None,
    graphify_watch_adapter: GraphifyWatchAdapter | None,
) -> tuple[GraphifyExtraction | None, tuple[GraphifyWarning, ...]]:
    if "graphify" not in selected_extractors:
        return None, ()
    graphify_config = _graphify_config_for_scan(workspace, config)
    if (
        graphify_config.json_path is None
        and not graphify_config.command
        and graphify_config.watch is None
        and not graphify_required
    ):
        return (
            None,
            _graphify_unavailable_source_warnings(workspace, discovered_files),
        )
    if graphify_config.watch is None:
        result = harvest_graphify_adapter(
            workspace,
            graphify_config,
            graphify_adapter=graphify_adapter,
            graphify_watch_adapter=graphify_watch_adapter,
        )
        return _graphify_extraction_from_fragment(result.graph_fragment), ()
    result = harvest_graphify_adapter(
        workspace,
        graphify_config,
        graphify_adapter=graphify_adapter,
        graphify_watch_adapter=graphify_watch_adapter,
    )
    return _graphify_extraction_from_fragment(result.graph_fragment), result.warnings


def _graphify_extraction_from_fragment(
    graph_fragment: HarvestGraphFragment,
) -> GraphifyExtraction:
    return GraphifyExtraction(
        symbols=graph_fragment.symbols,
        services=graph_fragment.services,
        contracts=graph_fragment.contracts,
        concepts=graph_fragment.concepts,
        decisions=graph_fragment.decisions,
        edges=graph_fragment.edges,
    )


def _graphify_unavailable_source_warnings(
    workspace: Workspace,
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[GraphifyWarning, ...]:
    if not any(
        discovered_file.kind == "source" for discovered_file in discovered_files
    ):
        return ()

    repository_id = (
        workspace.repositories[0].id if workspace.repositories else workspace.id
    )
    return (
        GraphifyWarning(
            repository_id=repository_id,
            path=GRAPHIFY_JSON_PATH,
            message=(
                "Graphify is enabled but no graph source was found. "
                "Run `graphify update .` and place the generated graph at "
                "`.code-scout/graphify-out/graph.json`, or configure "
                "`extractors.graphify.json_path`."
            ),
        ),
    )


def _filter_graphify_extraction_to_discovered_files(
    graphify_extraction: GraphifyExtraction | None,
    discovered_files: tuple[DiscoveredFile, ...],
) -> GraphifyExtraction | None:
    if graphify_extraction is None:
        return None

    discovered_file_ids = {
        _file_id(discovered_file.repository_id, discovered_file.path)
        for discovered_file in discovered_files
    }
    symbols = tuple(
        symbol
        for symbol in graphify_extraction.symbols
        if symbol.file_id in discovered_file_ids
    )
    removed_symbol_ids = {
        symbol.id
        for symbol in graphify_extraction.symbols
        if symbol.file_id not in discovered_file_ids
    }
    if not removed_symbol_ids:
        return graphify_extraction

    return replace(
        graphify_extraction,
        symbols=symbols,
        edges=tuple(
            edge
            for edge in graphify_extraction.edges
            if not (
                (edge.source_type == "symbol" and edge.source_id in removed_symbol_ids)
                or (
                    edge.target_type == "symbol"
                    and edge.target_id in removed_symbol_ids
                )
            )
        ),
    )


def _graphify_config_for_scan(
    workspace: Workspace,
    config: WorkspaceConfig,
) -> ExtractorEntryConfig:
    graphify_config = config.extractors.graphify
    if (
        graphify_config.json_path is not None
        or graphify_config.command
        or graphify_config.watch is not None
    ):
        return graphify_config

    conventional_path = _conventional_graphify_json_path(workspace)
    if conventional_path is not None:
        return replace(graphify_config, json_path=conventional_path)

    generated_path = _generate_default_graphify_json(workspace)
    if generated_path is None:
        return graphify_config
    return replace(graphify_config, json_path=generated_path)


def _conventional_graphify_json_path(workspace: Workspace) -> str | None:
    workspace_root = Path(workspace.root_path)
    for json_path in CONVENTIONAL_GRAPHIFY_JSON_PATHS:
        if (workspace_root / json_path).is_file():
            return json_path
    return None


def _default_graphify_command_available() -> bool:
    return shutil.which("graphify") is not None


def _generate_default_graphify_json(workspace: Workspace) -> str | None:
    if not _default_graphify_command_available():
        return None

    workspace_root = Path(workspace.root_path)
    try:
        subprocess.run(
            ("graphify", "update", "."),
            cwd=workspace.root_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip()
        raise ConfigError(
            "Graphify update failed during scan.",
            detail or "Run `graphify update .` manually to inspect the failure.",
        ) from error

    generated_output = workspace_root / "graphify-out"
    preferred_output = workspace_root / GRAPHIFY_OUTPUT_DIRECTORY
    if (generated_output / "graph.json").is_file():
        preferred_output.parent.mkdir(parents=True, exist_ok=True)
        if preferred_output.exists():
            shutil.rmtree(preferred_output)
        shutil.move(str(generated_output), str(preferred_output))

    return _conventional_graphify_json_path(workspace)


def _symbol_extraction(
    discovered_files: tuple[DiscoveredFile, ...],
    selected_extractors: tuple[str, ...],
) -> SymbolExtraction:
    if "symbol_extraction" not in selected_extractors:
        return SymbolExtraction()

    result = harvest_symbol_extraction_adapter(
        discovered_files,
        selected_extractors=selected_extractors,
    )
    return SymbolExtraction(
        symbols=result.graph_fragment.symbols,
        services=result.graph_fragment.services,
        contracts=result.graph_fragment.contracts,
        framework_hints=result.framework_hints,
        edges=result.graph_fragment.edges,
    )


GRAPHIFY_AUTHORITATIVE_TREE_SITTER_RELATIONS = frozenset(
    {
        "calls",
        "calls_symbol",
        "declares_module",
        "declares_symbol",
        "imports",
        "imports_symbol",
    },
)


def _suppress_tree_sitter_facts_covered_by_graphify(
    symbol_extraction: SymbolExtraction,
    *,
    discovered_files: tuple[DiscoveredFile, ...],
    graphify_symbols: tuple[Symbol, ...],
    graphify_services: tuple[Service, ...],
    graphify_edges: tuple[Edge, ...],
) -> SymbolExtraction:
    graphify_file_ids = _graphify_covered_file_ids(
        discovered_files,
        graphify_symbols=graphify_symbols,
        graphify_services=graphify_services,
        graphify_edges=graphify_edges,
    )
    if not graphify_file_ids:
        return symbol_extraction

    suppressed_symbol_ids = {
        symbol.id
        for symbol in symbol_extraction.symbols
        if symbol.file_id in graphify_file_ids
        and _symbol_has_extractor(symbol, "tree_sitter")
    }
    if not suppressed_symbol_ids:
        suppressed_symbol_ids = set()

    return replace(
        symbol_extraction,
        symbols=tuple(
            symbol
            for symbol in symbol_extraction.symbols
            if symbol.id not in suppressed_symbol_ids
        ),
        services=tuple(
            service
            for service in symbol_extraction.services
            if not _tree_sitter_service_covered_by_graphify(
                service,
                graphify_file_ids,
            )
        ),
        edges=tuple(
            edge
            for edge in symbol_extraction.edges
            if not _tree_sitter_edge_covered_by_graphify(
                edge,
                suppressed_symbol_ids,
                graphify_file_ids,
            )
        ),
    )


def _graphify_covered_file_ids(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    graphify_symbols: tuple[Symbol, ...],
    graphify_services: tuple[Service, ...],
    graphify_edges: tuple[Edge, ...],
) -> set[str]:
    file_ids = {symbol.file_id for symbol in graphify_symbols}
    file_ids_by_path = {
        discovered_file.path: _file_id(
            discovered_file.repository_id,
            discovered_file.path,
        )
        for discovered_file in discovered_files
    }
    for evidence in (
        *(evidence for symbol in graphify_symbols for evidence in symbol.evidence),
        *(evidence for service in graphify_services for evidence in service.evidence),
        *(evidence for edge in graphify_edges for evidence in edge.evidence),
    ):
        file_id = file_ids_by_path.get(evidence.file_path)
        if file_id is not None:
            file_ids.add(file_id)
    return file_ids


def _symbol_has_extractor(symbol: Symbol, extractor_name: str) -> bool:
    if any(evidence.extractor_name == extractor_name for evidence in symbol.evidence):
        return True
    return _extractor_name_from_symbol_id(symbol.id) == extractor_name


def _tree_sitter_service_covered_by_graphify(
    service: Service,
    graphify_file_ids: set[str],
) -> bool:
    if any(
        evidence.extractor_name == "tree_sitter"
        and evidence.metadata.get("framework") == "nextjs"
        for evidence in service.evidence
    ):
        return False
    return any(
        evidence.extractor_name == "tree_sitter"
        and _evidence_file_id(evidence) in graphify_file_ids
        for evidence in service.evidence
    )


def _tree_sitter_edge_covered_by_graphify(
    edge: Edge,
    suppressed_symbol_ids: set[str],
    graphify_file_ids: set[str],
) -> bool:
    if not _edge_has_extractor(edge, "tree_sitter"):
        return False
    if (
        edge.relation_type == "declares_symbol"
        and edge.target_type == "symbol"
        and edge.target_id in suppressed_symbol_ids
    ):
        return True
    return edge.relation_type in GRAPHIFY_AUTHORITATIVE_TREE_SITTER_RELATIONS and any(
        _evidence_file_id(evidence) in graphify_file_ids for evidence in edge.evidence
    )


def _edge_has_extractor(edge: Edge, extractor_name: str) -> bool:
    return any(evidence.extractor_name == extractor_name for evidence in edge.evidence)


def _evidence_file_id(evidence: Evidence) -> str:
    repository_id = evidence.metadata.get("repository_id")
    if isinstance(repository_id, str):
        return _file_id(repository_id, evidence.file_path)
    return _file_id("workspace", evidence.file_path)


def _graph_repositories(
    *,
    workspace: Workspace,
    discovered_files: tuple[DiscoveredFile, ...],
    package_facts_by_repository_id: dict[str, RepositoryPackageFacts],
    symbol_framework_hints_by_repository_id: dict[str, tuple[str, ...]],
) -> tuple[Repository, ...]:
    repositories: list[Repository] = []
    for repository in workspace.repositories:
        package_fact = package_facts_by_repository_id.get(repository.id)
        package_framework_hints = (
            () if package_fact is None else package_fact.framework_hints
        )
        inferred_language_hints = _infer_language_hints(
            discovered_files,
            repository.id,
        )
        symbol_framework_hints = symbol_framework_hints_by_repository_id.get(
            repository.id,
            (),
        )
        inferred_module_hints = _infer_module_hints(
            discovered_files,
            repository.id,
        )
        repositories.append(
            Repository(
                id=repository.id,
                name=repository.display_name,
                path=repository.path,
                language_hints=_merge_hints(
                    repository.language_hints,
                    (
                        *inferred_language_hints,
                        *(() if package_fact is None else package_fact.language_hints),
                    ),
                ),
                framework_hints=_merge_hints(
                    repository.framework_hints,
                    (*package_framework_hints, *symbol_framework_hints),
                ),
                module_hints=_merge_module_hints(
                    repository.module_hints,
                    inferred_module_hints,
                ),
                evidence=(
                    Evidence(
                        file_path=repository.path,
                        extractor_name="workspace_config",
                        confidence=1.0,
                        metadata={"workspace_id": workspace.id},
                    ),
                ),
            ),
        )
    return tuple(repositories)


def _extractor_name_from_symbol_id(symbol_id: str) -> str:
    parts = symbol_id.split(":")
    if len(parts) >= 5 and parts[-2] in {"graphify", "lsp", "tree_sitter"}:
        return parts[-2]
    return "symbol_extraction"


def _graph_file(discovered_file: DiscoveredFile) -> File:
    return File(
        id=_file_id(discovered_file.repository_id, discovered_file.path),
        repository_id=discovered_file.repository_id,
        path=discovered_file.path,
        kind=discovered_file.kind,
        extension=discovered_file.extension,
        language=discovered_file.language,
        content_hash=discovered_file.content_hash,
        modified_at=discovered_file.modified_at,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name="file_discovery",
                confidence=1.0,
                metadata={"repository_id": discovered_file.repository_id},
            ),
        ),
    )


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _adr_decision_edges(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[Edge, ...]:
    return tuple(
        Edge(
            source_type="file",
            source_id=_file_id(discovered_file.repository_id, discovered_file.path),
            target_type="decision",
            target_id=_decision_id(discovered_file.repository_id, discovered_file.path),
            relation_type="documents_decision",
            confidence=1.0,
            evidence=(
                Evidence(
                    file_path=discovered_file.path,
                    extractor_name="adr_markdown",
                    confidence=1.0,
                    start_line=1,
                    end_line=_line_count(discovered_file.absolute_path),
                    metadata={"repository_id": discovered_file.repository_id},
                ),
            ),
        )
        for discovered_file in discovered_files
        if discovered_file.kind == "decision"
    )


def _decision_id(repository_id: str, path: str) -> str:
    return f"decision:{repository_id}:{path}"


def _merge_hints(
    configured_hints: tuple[str, ...],
    inferred_hints: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(sorted({*configured_hints, *inferred_hints}))


def _merge_module_hints(
    configured_hints: tuple[str, ...],
    inferred_hints: tuple[str, ...],
) -> tuple[str, ...]:
    known_hints = {hint for hint in (*configured_hints, *inferred_hints)}
    ordered_hints = [hint for hint in MODULE_HINT_ORDER if hint in known_hints]
    extra_hints = sorted(known_hints - set(MODULE_HINT_ORDER))
    return tuple((*ordered_hints, *extra_hints))


def _infer_language_hints(
    discovered_files: tuple[DiscoveredFile, ...],
    repository_id: str,
) -> tuple[str, ...]:
    language_counts = Counter(
        discovered_file.language
        for discovered_file in discovered_files
        if discovered_file.repository_id == repository_id
        and discovered_file.language is not None
    )
    language_file_count = language_counts.total()
    if language_file_count == 0:
        return ()

    qualifying_languages = (
        language
        for language, count in language_counts.items()
        if count / language_file_count >= 0.05
    )
    return tuple(
        sorted(
            qualifying_languages,
            key=lambda language: (-language_counts[language], language),
        )[:5],
    )


def _infer_module_hints(
    discovered_files: tuple[DiscoveredFile, ...],
    repository_id: str,
) -> tuple[str, ...]:
    hints: set[str] = set()
    for discovered_file in discovered_files:
        if discovered_file.repository_id != repository_id:
            continue
        path_tokens = _module_path_tokens(discovered_file.path)
        if path_tokens & {"backend", "api", "application", "server"}:
            hints.add("backend")
        if path_tokens & {"frontend", "client", "web", "ui"}:
            hints.add("frontend")
        if path_tokens & {"function", "functions"}:
            hints.add("functions")
        if "domain" in path_tokens:
            hints.add("domain")
        if path_tokens & {"infrastructure", "infra"} or _is_infrastructure_module_path(
            discovered_file.path,
        ):
            hints.add("infrastructure")
        if path_tokens & {"test", "tests", "spec", "specs"}:
            hints.add("tests")

    return tuple(hint for hint in MODULE_HINT_ORDER if hint in hints)


def _module_path_tokens(path: str) -> set[str]:
    tokens: set[str] = set()
    for part in path.replace("\\", "/").split("/"):
        for token in part.replace(".", "-").replace("_", "-").split("-"):
            normalized_token = token.strip().lower()
            if normalized_token:
                tokens.add(normalized_token)
    return tokens


def _is_infrastructure_module_path(path: str) -> bool:
    normalized_path = path.replace("\\", "/").lower()
    filename = normalized_path.rsplit("/", maxsplit=1)[-1]
    suffix = f".{filename.rsplit('.', maxsplit=1)[-1]}" if "." in filename else ""
    return (
        filename == "dockerfile"
        or filename.startswith("dockerfile.")
        or filename.startswith("docker-compose.")
        or filename == "host.json"
        or suffix in {".tf", ".tfvars", ".bicep"}
        or "/k8s/" in f"/{normalized_path}/"
        or "/kubernetes/" in f"/{normalized_path}/"
        or "/manifests/" in f"/{normalized_path}/"
    )


def _framework_hints_by_repository_id(
    hints: tuple[RepositoryFrameworkHint, ...],
) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, set[str]] = {}
    for hint in hints:
        grouped.setdefault(hint.repository_id, set()).add(hint.framework)
    return {
        repository_id: tuple(sorted(frameworks))
        for repository_id, frameworks in grouped.items()
    }


def _line_count(path: str) -> int:
    with open(path, encoding="utf-8") as file:
        return sum(1 for _line in file)
