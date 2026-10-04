from __future__ import annotations

import pytest

from code_scout.core.errors import ExtractionError
from code_scout.discovery import DiscoveredFile
from code_scout.extractors.lsp import (
    DEFAULT_TYPESCRIPT_LSP_COMMAND,
    LSPDocumentSymbol,
    LSPRelationship,
    TypeScriptLSPProcessAdapter,
    TypeScriptLSPRun,
    extract_typescript_lsp,
    typescript_lsp_command,
    typescript_lsp_validation_edges,
)
from code_scout.models.graph import Edge, Evidence, Symbol
from code_scout.models.workspace import RepositoryConfig, Workspace
from code_scout.workspace.config import ExtractorEntryConfig


class RecordingAdapter:
    def __init__(self) -> None:
        self.starts: list[tuple[str, str, tuple[str, ...]]] = []
        self.connects: list[tuple[str, str, str]] = []

    def start(
        self,
        *,
        repository_id: str,
        repository_root: str,
        command: tuple[str, ...],
    ) -> TypeScriptLSPRun:
        self.starts.append((repository_id, repository_root, command))
        return TypeScriptLSPRun(
            repository_id=repository_id,
            repository_root=repository_root,
            mode="stdio",
            command=command,
        )

    def connect(
        self,
        *,
        repository_id: str,
        repository_root: str,
        address: str,
    ) -> TypeScriptLSPRun:
        self.connects.append((repository_id, repository_root, address))
        return TypeScriptLSPRun(
            repository_id=repository_id,
            repository_root=repository_root,
            mode="tcp",
            connect=address,
        )

    def collect(
        self,
        *,
        run: TypeScriptLSPRun,
        discovered_files: tuple[DiscoveredFile, ...],
    ) -> TypeScriptLSPRun:
        return run


def test_typescript_lsp_uses_default_command_when_config_omits_command() -> None:
    assert typescript_lsp_command(ExtractorEntryConfig()) == (
        DEFAULT_TYPESCRIPT_LSP_COMMAND
    )


def test_typescript_lsp_uses_configured_command() -> None:
    config = ExtractorEntryConfig(
        command=("node", "./node_modules/.bin/typescript-language-server", "--stdio"),
    )

    assert typescript_lsp_command(config) == (
        "node",
        "./node_modules/.bin/typescript-language-server",
        "--stdio",
    )


def test_typescript_lsp_starts_adapter_for_repository_with_typescript_file(
    tmp_path,
) -> None:
    source_file = tmp_path / "src" / "app.ts"
    source_file.parent.mkdir()
    source_file.write_text("export const app = true;\n", encoding="utf-8")
    workspace = Workspace(
        id="workspace",
        name="Workspace",
        root_path=str(tmp_path),
        repositories=(
            RepositoryConfig(
                id="workspace",
                display_name="workspace",
                path=".",
            ),
        ),
    )
    discovered_file = DiscoveredFile(
        repository_id="workspace",
        path="src/app.ts",
        absolute_path=str(source_file),
        kind="source",
        extension=".ts",
        content_hash="hash",
        modified_at="2026-06-20T10:00:00Z",
        language="TypeScript",
    )
    adapter = RecordingAdapter()

    extraction = extract_typescript_lsp(
        workspace,
        (discovered_file,),
        ExtractorEntryConfig(command=("ts-ls", "--stdio")),
        adapter=adapter,
    )

    assert adapter.starts == [
        ("workspace", str(tmp_path.resolve()), ("ts-ls", "--stdio"))
    ]
    assert adapter.connects == []
    assert extraction.runs == (
        TypeScriptLSPRun(
            repository_id="workspace",
            repository_root=str(tmp_path.resolve()),
            mode="stdio",
            command=("ts-ls", "--stdio"),
        ),
    )


def test_typescript_lsp_connects_when_configured(tmp_path) -> None:
    source_file = tmp_path / "src" / "app.tsx"
    source_file.parent.mkdir()
    source_file.write_text("export function App() { return null; }\n", encoding="utf-8")
    workspace = Workspace(
        id="workspace",
        name="Workspace",
        root_path=str(tmp_path),
        repositories=(
            RepositoryConfig(
                id="workspace",
                display_name="workspace",
                path=".",
            ),
        ),
    )
    discovered_file = DiscoveredFile(
        repository_id="workspace",
        path="src/app.tsx",
        absolute_path=str(source_file),
        kind="source",
        extension=".tsx",
        content_hash="hash",
        modified_at="2026-06-20T10:00:00Z",
    )
    adapter = RecordingAdapter()

    extraction = extract_typescript_lsp(
        workspace,
        (discovered_file,),
        ExtractorEntryConfig(connect="127.0.0.1:2089"),
        adapter=adapter,
    )

    assert adapter.starts == []
    assert adapter.connects == [
        ("workspace", str(tmp_path.resolve()), "127.0.0.1:2089")
    ]
    assert extraction.runs[0].mode == "tcp"


def test_typescript_lsp_collects_mocked_symbols_without_broad_graph_facts(
    tmp_path,
) -> None:
    class CollectingAdapter(RecordingAdapter):
        def collect(
            self,
            *,
            run: TypeScriptLSPRun,
            discovered_files: tuple[DiscoveredFile, ...],
        ) -> TypeScriptLSPRun:
            assert [file.path for file in discovered_files] == [
                "src/users.controller.ts",
                "src/users.dto.ts",
                "src/users.module.ts",
                "src/users.service.ts",
            ]
            return TypeScriptLSPRun(
                repository_id=run.repository_id,
                repository_root=run.repository_root,
                mode=run.mode,
                command=run.command,
                document_symbols=(
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/users.controller.ts",
                        name="UsersController",
                        kind="class",
                        qualified_name="UsersController",
                        start_line=3,
                        end_line=8,
                        metadata={"framework": "nestjs"},
                    ),
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/users.service.ts",
                        name="UsersService",
                        kind="class",
                        qualified_name="UsersService",
                        start_line=2,
                        end_line=5,
                        metadata={"framework": "nestjs"},
                    ),
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/users.dto.ts",
                        name="UserDto",
                        kind="class",
                        qualified_name="UserDto",
                        start_line=1,
                        end_line=3,
                        metadata={"framework": "nestjs"},
                    ),
                    LSPDocumentSymbol(
                        repository_id="workspace",
                        file_path="src/users.module.ts",
                        name="UsersModule",
                        kind="class",
                        qualified_name="UsersModule",
                        start_line=4,
                        end_line=8,
                        metadata={"framework": "nestjs"},
                    ),
                ),
                relationships=(
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/users.controller.ts",
                        source_line=6,
                        target_path="src/users.service.ts",
                        target_line=2,
                        kind="definition",
                        metadata={"operation": "textDocument/definition"},
                    ),
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/users.controller.ts",
                        source_line=7,
                        target_path="src/users.dto.ts",
                        target_line=1,
                        kind="definition",
                        metadata={"operation": "textDocument/definition"},
                    ),
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/users.service.ts",
                        source_line=2,
                        target_path="src/users.controller.ts",
                        target_line=6,
                        kind="reference",
                        metadata={"operation": "textDocument/references"},
                    ),
                    LSPRelationship(
                        repository_id="workspace",
                        source_path="src/users.module.ts",
                        source_line=6,
                        target_path="src/users.service.ts",
                        target_line=2,
                        kind="implementation",
                        metadata={"operation": "textDocument/implementation"},
                    ),
                ),
            )

    controller_file = tmp_path / "src" / "users.controller.ts"
    dto_file = tmp_path / "src" / "users.dto.ts"
    module_file = tmp_path / "src" / "users.module.ts"
    service_file = tmp_path / "src" / "users.service.ts"
    controller_file.parent.mkdir()
    controller_file.write_text("controller\n", encoding="utf-8")
    dto_file.write_text("dto\n", encoding="utf-8")
    module_file.write_text("module\n", encoding="utf-8")
    service_file.write_text("service\n", encoding="utf-8")
    workspace = Workspace(
        id="workspace",
        name="Workspace",
        root_path=str(tmp_path),
        repositories=(
            RepositoryConfig(
                id="workspace",
                display_name="workspace",
                path=".",
            ),
        ),
    )
    discovered_files = (
        DiscoveredFile(
            repository_id="workspace",
            path="src/users.controller.ts",
            absolute_path=str(controller_file),
            kind="source",
            extension=".ts",
            content_hash="controller-hash",
            modified_at="2026-06-20T10:00:00Z",
            language="TypeScript",
        ),
        DiscoveredFile(
            repository_id="workspace",
            path="src/users.dto.ts",
            absolute_path=str(dto_file),
            kind="source",
            extension=".ts",
            content_hash="dto-hash",
            modified_at="2026-06-20T10:00:00Z",
            language="TypeScript",
        ),
        DiscoveredFile(
            repository_id="workspace",
            path="src/users.module.ts",
            absolute_path=str(module_file),
            kind="source",
            extension=".ts",
            content_hash="module-hash",
            modified_at="2026-06-20T10:00:00Z",
            language="TypeScript",
        ),
        DiscoveredFile(
            repository_id="workspace",
            path="src/users.service.ts",
            absolute_path=str(service_file),
            kind="source",
            extension=".ts",
            content_hash="service-hash",
            modified_at="2026-06-20T10:00:00Z",
            language="TypeScript",
        ),
    )

    extraction = extract_typescript_lsp(
        workspace,
        discovered_files,
        ExtractorEntryConfig(command=("ts-ls", "--stdio")),
        adapter=CollectingAdapter(),
    )

    assert extraction.symbols == ()
    assert extraction.edges == ()
    assert {symbol.name for symbol in extraction.runs[0].document_symbols} == {
        "UserDto",
        "UsersController",
        "UsersModule",
        "UsersService",
    }
    assert {relationship.kind for relationship in extraction.runs[0].relationships} == {
        "definition",
        "implementation",
        "reference",
    }


def test_typescript_lsp_validation_edges_reference_confirmed_facts() -> None:
    route_symbol = Symbol(
        id="symbol:workspace:src/routes.ts:tree_sitter:GET",
        file_id="file:workspace:src/routes.ts",
        name="GET",
        kind="function",
        qualified_name="GET",
        start_line=3,
        end_line=5,
    )
    service_symbol = Symbol(
        id="symbol:workspace:src/users.ts:graphify:listUsers",
        file_id="file:workspace:src/users.ts",
        name="listUsers",
        kind="function",
        qualified_name="listUsers",
        start_line=1,
        end_line=3,
    )
    dto_symbol = Symbol(
        id="symbol:workspace:src/dto.ts:graphify:UserDto",
        file_id="file:workspace:src/dto.ts",
        name="UserDto",
        kind="class",
        qualified_name="UserDto",
        start_line=1,
        end_line=4,
    )
    declares_edge = Edge(
        source_type="file",
        source_id="file:workspace:src/routes.ts",
        target_type="symbol",
        target_id=route_symbol.id,
        relation_type="declares_symbol",
        confidence=0.6,
        evidence=(
            Evidence(
                file_path="src/routes.ts",
                extractor_name="tree_sitter",
                confidence=0.6,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )
    calls_edge = Edge(
        source_type="symbol",
        source_id=route_symbol.id,
        target_type="symbol",
        target_id=service_symbol.id,
        relation_type="calls",
        confidence=0.6,
        evidence=(
            Evidence(
                file_path="src/routes.ts",
                extractor_name="tree_sitter",
                confidence=0.6,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )
    type_edge = Edge(
        source_type="symbol",
        source_id=route_symbol.id,
        target_type="symbol",
        target_id=dto_symbol.id,
        relation_type="typed_as",
        confidence=0.6,
        evidence=(
            Evidence(
                file_path="src/routes.ts",
                extractor_name="graphify",
                confidence=0.6,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )
    run = TypeScriptLSPRun(
        repository_id="workspace",
        repository_root="/workspace",
        mode="stdio",
        document_symbols=(
            LSPDocumentSymbol(
                repository_id="workspace",
                file_path="src/routes.ts",
                name="GET",
                kind="function",
                qualified_name="GET",
                start_line=3,
                end_line=5,
                confidence=0.92,
            ),
        ),
        relationships=(
            LSPRelationship(
                repository_id="workspace",
                source_path="src/routes.ts",
                source_line=4,
                target_path="src/users.ts",
                target_line=1,
                kind="definition",
                confidence=0.94,
                metadata={"operation": "textDocument/definition"},
            ),
            LSPRelationship(
                repository_id="workspace",
                source_path="src/routes.ts",
                source_line=4,
                target_path="src/dto.ts",
                target_line=1,
                kind="type_definition",
                confidence=0.93,
                metadata={"operation": "textDocument/typeDefinition"},
            ),
        ),
    )

    edges = typescript_lsp_validation_edges(
        (run,),
        authoritative_symbols=(route_symbol, service_symbol, dto_symbol),
        authoritative_edges=(declares_edge, calls_edge, type_edge),
    )

    assert [(edge.relation_type, edge.confidence) for edge in edges] == [
        ("declares_symbol", 0.92),
        ("calls", 0.94),
        ("typed_as", 0.93),
    ]
    assert [edge.evidence[0].extractor_name for edge in edges] == [
        "lsp",
        "lsp",
        "lsp",
    ]
    assert edges[0].evidence[0].metadata["validation_kind"] == "declaration"
    assert edges[1].evidence[0].metadata["validation_kind"] == "definition"
    assert edges[1].evidence[0].metadata["validated_relation"] == "calls"
    assert edges[2].evidence[0].metadata["validation_kind"] == "type_definition"
    assert edges[2].evidence[0].metadata["validated_relation"] == "typed_as"
    assert "validated_fact_id" in edges[1].evidence[0].metadata


def test_unavailable_typescript_lsp_returns_actionable_setup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fail_popen(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("missing")

    monkeypatch.setattr("subprocess.Popen", fail_popen)

    with pytest.raises(ExtractionError) as error:
        TypeScriptLSPProcessAdapter().start(
            repository_id="workspace",
            repository_root=str(tmp_path),
            command=("missing-language-server", "--stdio"),
        )

    assert "TypeScript language server is unavailable" in error.value.message
    assert "extractors.lsp.command" in (error.value.hint or "")
