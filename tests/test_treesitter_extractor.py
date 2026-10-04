from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_scout.discovery import DiscoveredFile
from code_scout.domain.normalize import normalize_graph_facts
from code_scout.extractors import SymbolExtraction, treesitter
from code_scout.extractors.treesitter import (
    GRAMMAR_PACKAGES,
    TreeSitterExtractor,
    _iter_declaration_nodes,
    _name_from_node,
    _symbol_from_node,
)
from code_scout.models.graph import Contract, Evidence


def test_treesitter_module_imports_without_optional_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter", None)
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter_python", None)

    result = treesitter.load_grammar("Python")

    assert result.available is False
    assert result.grammar is None
    assert result.missing_reason == "Missing optional package tree-sitter"


def test_treesitter_grammar_package_map_covers_tree_sitter_languages() -> None:
    assert {
        "Bash",
        "C",
        "C#",
        "C++",
        "Dart",
        "Elixir",
        "Go",
        "Java",
        "JavaScript",
        "Julia",
        "Kotlin",
        "Lua",
        "PHP",
        "Python",
        "Ruby",
        "Rust",
        "SQL",
        "Scala",
        "Swift",
        "TSX",
        "TypeScript",
        "Zig",
    } <= set(GRAMMAR_PACKAGES)
    assert GRAMMAR_PACKAGES["C#"].module == "tree_sitter_c_sharp"
    assert GRAMMAR_PACKAGES["PHP"].function == "language_php"
    assert GRAMMAR_PACKAGES["TSX"].function == "language_tsx"


def test_treesitter_loads_grammar_and_extracts_symbols(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    root_node = FakeNode(
        type="module",
        children=(
            FakeNode(
                type="class_definition",
                start_point=(1, 0),
                end_point=(3, 0),
                children=(FakeNode(type="identifier", text=b"UserService"),),
            ),
            FakeNode(
                type="function_definition",
                start_point=(5, 0),
                end_point=(7, 0),
                children=(FakeNode(type="identifier", text=b"load_user"),),
            ),
        ),
    )
    parser = FakeParser(root_node)
    language_values: list[object] = []
    tree_sitter_module = SimpleNamespace(
        Language=lambda value: f"Language({value})",
        Parser=lambda language: parser.with_language(language),
    )
    grammar_module = SimpleNamespace(
        language=lambda: language_values.append("python-grammar") or "python-grammar",
    )
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter", tree_sitter_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "tree_sitter_python",
        grammar_module,
    )
    source_path = tmp_path / "app.py"
    source_path.write_text("class UserService:\n    pass\n", encoding="utf-8")

    extraction = TreeSitterExtractor().extract(
        _discovered_file(
            path="src/app.py",
            absolute_path=str(source_path),
            extension=".py",
            language="Python",
        ),
    )

    assert language_values == ["python-grammar"]
    assert parser.language == "Language(python-grammar)"
    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "UserService",
        "load_user",
    )
    assert tuple(symbol.kind for symbol in extraction.symbols) == (
        "class",
        "function",
    )
    assert extraction.symbols[0].id == ("symbol:repo:src/app.py:python:userservice")
    assert extraction.edges[0].relation_type == "declares_symbol"
    assert extraction.edges[0].confidence == 1.0
    assert extraction.edges[0].evidence[0].extractor_name == "tree_sitter"
    assert parser.parsed_sources == [b"class UserService:\n    pass\n"]


def test_treesitter_extracts_nextjs_route_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    parser = FakeParser(FakeNode(type="program"))
    tree_sitter_module = SimpleNamespace(
        Language=lambda value: value,
        Parser=lambda language: parser.with_language(language),
    )
    grammar_module = SimpleNamespace(language_typescript=lambda: "typescript-grammar")
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter", tree_sitter_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "tree_sitter_typescript",
        grammar_module,
    )
    source_path = tmp_path / "route.ts"
    source_path.write_text(
        "export async function POST(request: CreateUserRequest) {}\n",
        encoding="utf-8",
    )

    extraction = TreeSitterExtractor().extract(
        _discovered_file(
            path="src/app/api/users/[userId]/route.ts",
            absolute_path=str(source_path),
            extension=".ts",
            language="TypeScript",
        ),
    )

    assert [(contract.method, contract.path) for contract in extraction.contracts] == [
        ("POST", "/api/users/{userId}")
    ]
    assert extraction.contracts[0].id == "contract:repo:http:post:api-users-userid"
    assert extraction.contracts[0].request_dtos == ("CreateUserRequest",)
    assert extraction.edges[0].relation_type == "declares_contract"
    assert extraction.contracts[0].evidence[0].metadata["framework"] == "nextjs"


def test_treesitter_links_nextjs_route_to_produced_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = FakeParser(FakeNode(type="program"))
    tree_sitter_module = SimpleNamespace(
        Language=lambda value: value,
        Parser=lambda language: parser.with_language(language),
    )
    grammar_module = SimpleNamespace(language_typescript=lambda: "typescript-grammar")
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter", tree_sitter_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "tree_sitter_typescript",
        grammar_module,
    )
    source_path = (
        Path(__file__).parent
        / "fixtures"
        / "typescript-monorepo"
        / "apps"
        / "web"
        / "app"
        / "api"
        / "users"
        / "route.ts"
    )

    extraction = TreeSitterExtractor().extract(
        _discovered_file(
            path="apps/web/app/api/users/route.ts",
            absolute_path=str(source_path),
            extension=".ts",
            language="TypeScript",
        ),
    )

    assert [(service.id, service.name) for service in extraction.services] == [
        ("service:repo:web", "web")
    ]
    assert [
        (contract.id, contract.method, contract.path)
        for contract in extraction.contracts
    ] == [("contract:repo:http:get:api-users", "GET", "/api/users")]
    assert [
        (edge.source_type, edge.source_id, edge.relation_type, edge.target_id)
        for edge in extraction.edges
    ] == [
        (
            "file",
            "file:repo:apps/web/app/api/users/route.ts",
            "declares_contract",
            "contract:repo:http:get:api-users",
        ),
        (
            "service",
            "service:repo:web",
            "produces",
            "contract:repo:http:get:api-users",
        ),
    ]
    producer_edge = extraction.edges[1]
    assert producer_edge.confidence == 1.0
    assert producer_edge.evidence[0].extractor_name == "tree_sitter"
    assert producer_edge.evidence[0].metadata == {
        "repository_id": "repo",
        "framework": "nextjs",
        "service_id": "service:repo:web",
        "service_name": "web",
        "route": "/api/users",
        "method": "GET",
    }


def test_treesitter_route_producer_merges_with_matching_openapi_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    parser = FakeParser(FakeNode(type="program"))
    tree_sitter_module = SimpleNamespace(
        Language=lambda value: value,
        Parser=lambda language: parser.with_language(language),
    )
    grammar_module = SimpleNamespace(language_typescript=lambda: "typescript-grammar")
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter", tree_sitter_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "tree_sitter_typescript",
        grammar_module,
    )
    source_path = tmp_path / "route.ts"
    source_path.write_text("export async function GET() {}\n", encoding="utf-8")
    extraction = TreeSitterExtractor().extract(
        _discovered_file(
            path="apps/web/app/api/users/route.ts",
            absolute_path=str(source_path),
            extension=".ts",
            language="TypeScript",
        ),
    )
    openapi_contract = Contract(
        id="contract:repo:contracts/openapi.yaml:get:api-users",
        name="listUsers",
        kind="http",
        path="/api/users",
        method="GET",
        operation_id="listUsers",
        evidence=(
            Evidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
                metadata={"repository_id": "repo", "operation_id": "listUsers"},
            ),
        ),
    )

    normalized = normalize_graph_facts(
        services=extraction.services,
        contracts=(*extraction.contracts, openapi_contract),
        edges=extraction.edges,
    )

    assert len(normalized.contracts) == 1
    assert normalized.contracts[0].id == "contract:repo:http:get:api-users"
    assert normalized.contracts[0].operation_id == "listUsers"
    assert {e.extractor_name for e in normalized.contracts[0].evidence} == {
        "openapi",
        "tree_sitter",
    }
    producer_edges = [
        edge for edge in normalized.edges if edge.relation_type == "produces"
    ]
    assert len(producer_edges) == 1
    assert producer_edges[0].source_id == "service:repo:web"
    assert producer_edges[0].target_id == "contract:repo:http:get:api-users"
    assert producer_edges[0].evidence[0].extractor_name == "tree_sitter"


def test_treesitter_missing_grammar_returns_empty_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    tree_sitter_module = SimpleNamespace(
        Language=lambda value: value,
        Parser=lambda language: FakeParser(FakeNode(type="module")),
    )
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter", tree_sitter_module)
    monkeypatch.setitem(__import__("sys").modules, "tree_sitter_go", None)
    source_path = tmp_path / "main.go"
    source_path.write_text("package main\n", encoding="utf-8")

    extraction = TreeSitterExtractor().extract(
        _discovered_file(
            path="main.go",
            absolute_path=str(source_path),
            extension=".go",
            language="Go",
        ),
    )

    assert extraction == SymbolExtraction()


def test_treesitter_name_extraction_uses_identifier_name_or_type_identifier() -> None:
    assert (
        _name_from_node(
            FakeNode(
                type="function_declaration",
                children=(FakeNode(type="identifier", text=b"load_user"),),
            ),
        )
        == "load_user"
    )
    assert (
        _name_from_node(
            FakeNode(
                type="create_table_statement",
                children=(FakeNode(type="name", text=b"accounts"),),
            ),
        )
        == "accounts"
    )
    assert (
        _name_from_node(
            FakeNode(
                type="type_alias_declaration",
                children=(FakeNode(type="type_identifier", text=b"UserId"),),
            ),
        )
        == "UserId"
    )


def test_treesitter_anonymous_declarations_are_skipped() -> None:
    symbol = _symbol_from_node(
        _discovered_file(
            path="src/app.py",
            absolute_path="/tmp/app.py",
            extension=".py",
            language="Python",
        ),
        "Python",
        FakeNode(type="function_definition"),
        "function",
    )

    assert symbol is None


def test_treesitter_declaration_walk_respects_configured_depth() -> None:
    nested_declaration = FakeNode(
        type="function_definition",
        children=(FakeNode(type="identifier", text=b"nested"),),
    )
    root_node = FakeNode(
        type="module",
        children=(
            FakeNode(
                type="class_definition",
                children=(
                    FakeNode(type="identifier", text=b"Service"),
                    FakeNode(
                        type="block",
                        children=(nested_declaration,),
                    ),
                ),
            ),
        ),
    )
    declarations = {
        "class_definition": "class",
        "function_definition": "function",
    }

    assert [
        (node.type, kind)
        for node, kind in _iter_declaration_nodes(
            root_node,
            declarations,
            max_depth=2,
        )
    ] == [("class_definition", "class")]
    assert [
        (node.type, kind)
        for node, kind in _iter_declaration_nodes(
            root_node,
            declarations,
            max_depth=3,
        )
    ] == [
        ("class_definition", "class"),
        ("function_definition", "function"),
    ]


def test_treesitter_symbol_format_and_fields_match_graph_model() -> None:
    symbol = _symbol_from_node(
        _discovered_file(
            path="src/app.py",
            absolute_path="/tmp/app.py",
            extension=".py",
            language="Python",
        ),
        "TypeScript",
        FakeNode(
            type="class_declaration",
            start_point=(9, 0),
            end_point=(14, 1),
            children=(FakeNode(type="type_identifier", text=b"UserService"),),
        ),
        "class",
    )

    assert symbol is not None
    assert symbol.id == "symbol:repo:src/app.py:typescript:userservice"
    assert symbol.file_id == "file:repo:src/app.py"
    assert symbol.name == "UserService"
    assert symbol.kind == "class"
    assert symbol.qualified_name == "UserService"
    assert symbol.start_line == 10
    assert symbol.end_line == 15


@dataclass(frozen=True)
class FakeNode:
    type: str
    text: bytes = b""
    start_point: tuple[int, int] = (0, 0)
    end_point: tuple[int, int] = (0, 0)
    children: tuple[FakeNode, ...] = ()


@dataclass
class FakeParser:
    root_node: FakeNode
    language: object | None = None
    parsed_sources: list[bytes] = field(default_factory=list)

    def with_language(self, language: object) -> FakeParser:
        self.language = language
        return self

    def parse(self, source: bytes) -> SimpleNamespace:
        self.parsed_sources.append(source)
        return SimpleNamespace(root_node=self.root_node)


def _discovered_file(
    *,
    path: str,
    absolute_path: str,
    extension: str,
    language: str,
) -> DiscoveredFile:
    return DiscoveredFile(
        repository_id="repo",
        path=path,
        absolute_path=absolute_path,
        kind="source",
        extension=extension,
        language=language,
        content_hash="hash",
        modified_at="2026-06-20T00:00:00+00:00",
    )
