from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from code_scout.core.errors import ExtractionError
from code_scout.discovery import DiscoveredFile

SUPPORTED_TYPESCRIPT_EXTENSIONS = frozenset({".cts", ".mts", ".ts", ".tsx"})
TREE_SITTER_SETUP_MESSAGE = (
    "TypeScript parsing requires the optional `tree-sitter` and "
    "`tree-sitter-typescript` packages."
)
TREE_SITTER_SETUP_HINT = (
    "Install parser support with `uv add tree-sitter tree-sitter-typescript` "
    "when network access is available, then rerun the scan."
)


@dataclass(frozen=True)
class TypeScriptSyntaxNode:
    type: str
    named: bool
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    text: str
    children: tuple[TypeScriptSyntaxNode, ...] = ()


@dataclass(frozen=True)
class TypeScriptParseResult:
    repository_id: str
    path: str
    language: str
    has_errors: bool
    root: TypeScriptSyntaxNode


class TypeScriptParserAdapter(Protocol):
    def parse(
        self,
        *,
        repository_id: str,
        path: str,
        source: bytes,
        language: str,
    ) -> TypeScriptParseResult: ...


class TreeSitterTypeScriptAdapter:
    def parse(
        self,
        *,
        repository_id: str,
        path: str,
        source: bytes,
        language: str,
    ) -> TypeScriptParseResult:
        tree_sitter, tree_sitter_typescript = _tree_sitter_modules()
        parser = _parser_for_language(tree_sitter, tree_sitter_typescript, language)
        tree = parser.parse(source)
        return TypeScriptParseResult(
            repository_id=repository_id,
            path=path,
            language=language,
            has_errors=bool(tree.root_node.has_error),
            root=_normalize_node(tree.root_node, source),
        )


def parse_typescript_file(
    discovered_file: DiscoveredFile,
    *,
    adapter: TypeScriptParserAdapter | None = None,
) -> TypeScriptParseResult | None:
    if discovered_file.extension not in SUPPORTED_TYPESCRIPT_EXTENSIONS:
        return None

    source = Path(discovered_file.absolute_path).read_bytes()
    parser = TreeSitterTypeScriptAdapter() if adapter is None else adapter
    return parser.parse(
        repository_id=discovered_file.repository_id,
        path=discovered_file.path,
        source=source,
        language=_language_for_extension(discovered_file.extension),
    )


def _tree_sitter_modules() -> tuple[ModuleType, ModuleType]:
    try:
        return (
            importlib.import_module("tree_sitter"),
            importlib.import_module("tree_sitter_typescript"),
        )
    except ImportError as error:
        raise ExtractionError(
            TREE_SITTER_SETUP_MESSAGE,
            hint=TREE_SITTER_SETUP_HINT,
        ) from error


def _parser_for_language(
    tree_sitter: ModuleType,
    tree_sitter_typescript: ModuleType,
    language: str,
) -> Any:
    language_function_name = (
        "language_tsx" if language == "tsx" else "language_typescript"
    )
    language_function = getattr(tree_sitter_typescript, language_function_name)
    language_value = _tree_sitter_language(tree_sitter, language_function())
    parser_class = tree_sitter.Parser

    try:
        return parser_class(language_value)
    except TypeError:
        parser = parser_class()

    if hasattr(parser, "set_language"):
        parser.set_language(language_value)
    else:
        parser.language = language_value
    return parser


def _tree_sitter_language(tree_sitter: ModuleType, language_value: Any) -> Any:
    language_class = getattr(tree_sitter, "Language", None)
    if language_class is None:
        return language_value

    try:
        return language_class(language_value)
    except TypeError:
        return language_value


def _normalize_node(node: Any, source: bytes) -> TypeScriptSyntaxNode:
    start_line, start_column = node.start_point
    end_line, end_column = node.end_point
    return TypeScriptSyntaxNode(
        type=node.type,
        named=bool(node.is_named),
        start_line=start_line + 1,
        start_column=start_column + 1,
        end_line=end_line + 1,
        end_column=end_column + 1,
        text=source[node.start_byte : node.end_byte].decode(
            "utf-8",
            errors="replace",
        ),
        children=tuple(_normalize_node(child, source) for child in node.children),
    )


def _language_for_extension(extension: str) -> str:
    return "tsx" if extension == ".tsx" else "typescript"
