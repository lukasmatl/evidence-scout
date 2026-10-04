from __future__ import annotations

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.graphify import GraphifyExtraction
from code_scout.extractors.lsp import TypeScriptLSPRun
from code_scout.extractors.package_json import RepositoryPackageFacts
from code_scout.harvest import (
    EvidenceHarvestResult,
    HarvestGraphFragment,
    LSPValidationInputs,
    PackageFactCounts,
    harvest_incremental_evidence,
    package_fact_counts,
)
from code_scout.harvest.adapters import (
    harvest_graphify_result,
    harvest_package_metadata_adapter,
)
from code_scout.models.graph import Edge, Evidence, Repository, Service, Symbol
from code_scout.models.workspace import default_single_repository_workspace
from code_scout.workspace.config import WorkspaceConfig


def test_package_fact_counts_summarizes_repository_package_facts() -> None:
    counts = package_fact_counts(
        (
            RepositoryPackageFacts(
                repository_id="repository:api",
                package_files_analyzed=2,
            ),
            RepositoryPackageFacts(
                repository_id="repository:web",
                package_files_analyzed=3,
            ),
        ),
    )

    assert counts == PackageFactCounts(
        repositories_detected=2,
        package_files=5,
    )


def test_evidence_harvest_result_carries_graph_and_lsp_validation_inputs() -> None:
    evidence = Evidence(file_path="src/api.ts", extractor_name="graphify")
    symbol = Symbol(
        id="symbol:workspace:src/api.ts:handler",
        file_id="file:workspace:src/api.ts",
        name="handler",
        kind="function",
        evidence=(evidence,),
    )
    edge = Edge(
        source_type="symbol",
        source_id=symbol.id,
        target_type="service",
        target_id="service:api",
        relation_type="implements",
        confidence=0.9,
        evidence=(evidence,),
    )
    lsp_run = TypeScriptLSPRun(
        repository_id="workspace",
        repository_root=".",
        mode="stdio",
    )

    result = EvidenceHarvestResult(
        graph_fragment=HarvestGraphFragment(
            repositories=(Repository(id="workspace", name="Workspace", path="."),),
            symbols=(symbol,),
            services=(Service(id="service:api", name="API"),),
            edges=(edge,),
        ),
        package_counts=PackageFactCounts(
            repositories_detected=1,
            package_files=1,
        ),
        lsp_validation_inputs=LSPValidationInputs(
            runs=(lsp_run,),
            authoritative_symbols=(symbol,),
            authoritative_edges=(edge,),
        ),
    )

    assert result.graph_fragment.symbols == (symbol,)
    assert result.graph_fragment.edges == (edge,)
    assert result.package_counts.package_files == 1
    assert result.lsp_validation_inputs == LSPValidationInputs(
        runs=(lsp_run,),
        authoritative_symbols=(symbol,),
        authoritative_edges=(edge,),
    )


def test_package_metadata_adapter_emits_direct_active_facts(tmp_path) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text('{"name":"orders","dependencies":{"fastify":"latest"}}\n')

    result = harvest_package_metadata_adapter(
        (_discovered("package.json", package_file, language="JSON"),)
    )

    assert result.active_facts
    assert result.package_facts[0].repository_id == "workspace"
    assert result.package_facts[0].package_files_analyzed == 1


def test_graphify_adapter_result_emits_graph_fragment_data() -> None:
    evidence = Evidence(file_path="src/api.ts", extractor_name="graphify")
    service = Service(id="service:api", name="API", evidence=(evidence,))
    symbol = Symbol(
        id="symbol:workspace:src/api.ts:handler",
        file_id="file:workspace:src/api.ts",
        name="handler",
        kind="function",
        evidence=(evidence,),
    )

    result = harvest_graphify_result(
        GraphifyExtraction(
            symbols=(symbol,),
            services=(service,),
        ),
    )

    assert result.graph_fragment.symbols == (symbol,)
    assert result.graph_fragment.services == (service,)


def test_incremental_harvest_uses_all_files_for_context_and_changed_files_for_facts(
    tmp_path,
) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text('{"name":"orders","dependencies":{"fastify":"latest"}}\n')
    app_file = tmp_path / "src" / "app.py"
    web_file = tmp_path / "src" / "web.ts"
    app_file.parent.mkdir()
    app_file.write_text("def app():\n    pass\n")
    web_file.write_text("export const web = true;\n")
    workspace = default_single_repository_workspace(
        tmp_path,
        config=WorkspaceConfig(),
    )
    discovered_files = (
        _discovered("package.json", package_file, language="JSON"),
        _discovered("src/app.py", app_file, language="Python"),
        _discovered("src/web.ts", web_file, language="TypeScript"),
    )

    harvest = harvest_incremental_evidence(
        workspace=workspace,
        config=WorkspaceConfig(),
        discovered_files=discovered_files,
        changed_discovered_files=(discovered_files[0],),
        selected_extractors=("package_metadata",),
    )

    assert {file.path for file in harvest.graph_fragment.files} == {"package.json"}
    assert harvest.package_counts == PackageFactCounts(
        repositories_detected=1,
        package_files=1,
    )
    assert {"Python", "TypeScript"}.issubset(
        harvest.graph_fragment.repositories[0].language_hints,
    )


def _discovered(
    path: str,
    absolute_path,
    *,
    language: str | None,
) -> DiscoveredFile:
    return DiscoveredFile(
        repository_id="workspace",
        path=path,
        absolute_path=str(absolute_path),
        kind="metadata" if path == "package.json" else "source",
        extension=absolute_path.suffix,
        content_hash="hash",
        modified_at="2026-06-22T00:00:00Z",
        language=language,
    )
