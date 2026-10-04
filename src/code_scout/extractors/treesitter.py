from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.models.graph import Contract, Edge, Evidence, Service, Symbol
from code_scout.utils.identifiers import canonical_http_contract_id, slugify_identifier

EXTRACTOR_NAME = "tree_sitter"
TREE_SITTER_CONFIDENCE = 1.0
HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
TYPESCRIPT_ROUTE_METHOD_PATTERN = re.compile(
    r"""
    \bexport\s+
    (?:async\s+)?
    function\s+
    (?P<method>DELETE|GET|HEAD|OPTIONS|PATCH|POST|PUT)
    \s*\((?P<parameters>[^)]*)\)
    """,
    re.VERBOSE,
)
_NAME_NODE_TYPES = frozenset(
    {
        "constant",
        "field_identifier",
        "identifier",
        "name",
        "namespace_identifier",
        "property_identifier",
        "simple_identifier",
        "table_name",
        "type_identifier",
        "variable_name",
        "word",
    },
)


@dataclass(frozen=True)
class TreeSitterGrammar:
    language: str
    package: str
    module: str
    function: str
    symbol_language: str | None = None


@dataclass(frozen=True)
class GrammarLoadResult:
    grammar: Any | None = None
    missing_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.grammar is not None


GRAMMAR_PACKAGES: dict[str, TreeSitterGrammar] = {
    "Python": TreeSitterGrammar(
        "Python", "tree-sitter-python", "tree_sitter_python", "language"
    ),
    "JavaScript": TreeSitterGrammar(
        "JavaScript",
        "tree-sitter-javascript",
        "tree_sitter_javascript",
        "language",
    ),
    "TypeScript": TreeSitterGrammar(
        "TypeScript",
        "tree-sitter-typescript",
        "tree_sitter_typescript",
        "language_typescript",
    ),
    "TSX": TreeSitterGrammar(
        "TSX",
        "tree-sitter-typescript",
        "tree_sitter_typescript",
        "language_tsx",
        symbol_language="TypeScript",
    ),
    "Go": TreeSitterGrammar("Go", "tree-sitter-go", "tree_sitter_go", "language"),
    "Rust": TreeSitterGrammar(
        "Rust", "tree-sitter-rust", "tree_sitter_rust", "language"
    ),
    "Java": TreeSitterGrammar(
        "Java", "tree-sitter-java", "tree_sitter_java", "language"
    ),
    "C": TreeSitterGrammar("C", "tree-sitter-c", "tree_sitter_c", "language"),
    "C++": TreeSitterGrammar("C++", "tree-sitter-cpp", "tree_sitter_cpp", "language"),
    "Ruby": TreeSitterGrammar(
        "Ruby", "tree-sitter-ruby", "tree_sitter_ruby", "language"
    ),
    "C#": TreeSitterGrammar(
        "C#", "tree-sitter-c-sharp", "tree_sitter_c_sharp", "language"
    ),
    "Kotlin": TreeSitterGrammar(
        "Kotlin",
        "tree-sitter-kotlin",
        "tree_sitter_kotlin",
        "language",
    ),
    "Scala": TreeSitterGrammar(
        "Scala",
        "tree-sitter-scala",
        "tree_sitter_scala",
        "language",
    ),
    "PHP": TreeSitterGrammar(
        "PHP", "tree-sitter-php", "tree_sitter_php", "language_php"
    ),
    "Swift": TreeSitterGrammar(
        "Swift",
        "tree-sitter-swift",
        "tree_sitter_swift",
        "language",
    ),
    "Lua": TreeSitterGrammar("Lua", "tree-sitter-lua", "tree_sitter_lua", "language"),
    "Zig": TreeSitterGrammar("Zig", "tree-sitter-zig", "tree_sitter_zig", "language"),
    "Dart": TreeSitterGrammar(
        "Dart", "tree-sitter-dart", "tree_sitter_dart", "language"
    ),
    "Elixir": TreeSitterGrammar(
        "Elixir",
        "tree-sitter-elixir",
        "tree_sitter_elixir",
        "language",
    ),
    "Julia": TreeSitterGrammar(
        "Julia",
        "tree-sitter-julia",
        "tree_sitter_julia",
        "language",
    ),
    "Bash": TreeSitterGrammar(
        "Bash", "tree-sitter-bash", "tree_sitter_bash", "language"
    ),
    "SQL": TreeSitterGrammar("SQL", "tree-sitter-sql", "tree_sitter_sql", "language"),
}

_DECLARATION_NODES: dict[str, dict[str, str]] = {
    "Python": {
        "class_definition": "class",
        "function_definition": "function",
        "decorated_definition": "function",
        "async_function_definition": "function",
    },
    "Go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
        "type_spec": "struct",
    },
    "Rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
        "type_alias_item": "type",
    },
    "Java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "record_declaration": "record",
    },
    "C#": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "struct_declaration": "struct",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "record_declaration": "record",
        "namespace_declaration": "namespace",
    },
    "Kotlin": {
        "class_declaration": "class",
        "function_declaration": "function",
        "object_declaration": "object",
        "interface_declaration": "interface",
    },
    "Scala": {
        "class_definition": "class",
        "object_definition": "object",
        "trait_definition": "trait",
        "def_definition": "function",
    },
    "Ruby": {
        "class": "class",
        "module": "module",
        "method": "method",
        "singleton_method": "method",
    },
    "PHP": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
        "function_definition": "function",
        "method_declaration": "method",
    },
    "Swift": {
        "class_declaration": "class",
        "struct_declaration": "struct",
        "protocol_declaration": "protocol",
        "function_declaration": "function",
        "extension_declaration": "extension",
    },
    "C": {
        "function_definition": "function",
        "struct_specifier": "struct",
        "enum_specifier": "enum",
        "type_definition": "type",
    },
    "C++": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
        "enum_specifier": "enum",
        "namespace_definition": "namespace",
    },
    "Lua": {
        "function_declaration": "function",
        "local_function": "function",
        "assignment_statement": "variable",
    },
    "Dart": {
        "class_definition": "class",
        "function_signature": "function",
        "method_signature": "method",
        "mixin_declaration": "mixin",
    },
    "Elixir": {
        "def": "function",
        "defp": "function",
        "defmodule": "module",
        "defstruct": "struct",
        "defprotocol": "protocol",
    },
    "Julia": {
        "function_definition": "function",
        "macro_definition": "macro",
        "struct_definition": "struct",
        "abstract_definition": "type",
        "module_definition": "module",
    },
    "Bash": {
        "function_definition": "function",
    },
    "SQL": {
        "create_table_statement": "table",
        "create_view_statement": "view",
        "create_function_statement": "function",
        "create_procedure_statement": "procedure",
        "create_index_statement": "index",
    },
    "Zig": {
        "fn_proto": "function",
        "container_decl": "struct",
    },
    "TypeScript": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "function_declaration": "function",
        "method_definition": "method",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    },
    "JavaScript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "lexical_declaration": "variable",
    },
}


class TreeSitterExtractor:
    """Extract top-level symbols with optional tree-sitter grammars."""

    def __init__(self, *, max_depth: int = 2) -> None:
        self.max_depth = max_depth

    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        language = _grammar_language_for_file(discovered_file)
        if language is None:
            return SymbolExtraction()

        grammar_result = load_grammar(language)
        if not grammar_result.available:
            return SymbolExtraction()

        tree_sitter = _import_tree_sitter()
        if tree_sitter is None:
            return SymbolExtraction()

        source = Path(discovered_file.absolute_path).read_bytes()
        parser = _parser_for_language(tree_sitter, grammar_result.grammar)
        tree = parser.parse(source)
        symbol_language = _symbol_language_for_grammar(language)
        declarations = _DECLARATION_NODES.get(symbol_language, {})
        symbols = tuple(
            _symbol
            for node, kind in _iter_declaration_nodes(
                tree.root_node,
                declarations,
                max_depth=self.max_depth,
            )
            for _symbol in (
                _symbol_from_node(discovered_file, symbol_language, node, kind),
            )
            if _symbol is not None
        )
        route_contracts = _typescript_route_contracts(discovered_file, source)
        route_services = _typescript_route_services(discovered_file, route_contracts)
        edges = (
            *tuple(_symbol_edge(discovered_file, symbol) for symbol in symbols),
            *tuple(
                _contract_edge(discovered_file, contract)
                for contract in route_contracts
            ),
            *tuple(
                _producer_edge(discovered_file, service, contract)
                for service in route_services
                for contract in route_contracts
            ),
        )
        return SymbolExtraction(
            symbols=symbols,
            services=route_services,
            contracts=route_contracts,
            edges=edges,
        )


def load_grammar(language: str) -> GrammarLoadResult:
    grammar = GRAMMAR_PACKAGES.get(language)
    if grammar is None:
        return GrammarLoadResult(missing_reason=f"No grammar configured for {language}")

    tree_sitter = _import_tree_sitter()
    if tree_sitter is None:
        return GrammarLoadResult(missing_reason="Missing optional package tree-sitter")

    try:
        grammar_module = importlib.import_module(grammar.module)
    except ImportError:
        return GrammarLoadResult(
            missing_reason=f"Missing optional package {grammar.package}",
        )

    language_function = getattr(grammar_module, grammar.function, None)
    if language_function is None:
        return GrammarLoadResult(
            missing_reason=(
                f"Grammar package {grammar.package} has no {grammar.function}()"
            ),
        )

    return GrammarLoadResult(
        grammar=_tree_sitter_language(tree_sitter, language_function()),
    )


def _import_tree_sitter() -> ModuleType | None:
    try:
        return importlib.import_module("tree_sitter")
    except ImportError:
        return None


def _grammar_language_for_file(discovered_file: DiscoveredFile) -> str | None:
    if discovered_file.language == "TypeScript" and discovered_file.extension == ".tsx":
        return "TSX"
    return discovered_file.language


def _symbol_language_for_grammar(language: str) -> str:
    grammar = GRAMMAR_PACKAGES[language]
    return grammar.symbol_language or language


def _parser_for_language(tree_sitter: ModuleType, language: Any) -> Any:
    parser_class = tree_sitter.Parser

    try:
        return parser_class(language)
    except TypeError:
        parser = parser_class()

    if hasattr(parser, "set_language"):
        parser.set_language(language)
    else:
        parser.language = language
    return parser


def _tree_sitter_language(tree_sitter: ModuleType, language_value: Any) -> Any:
    language_class = getattr(tree_sitter, "Language", None)
    if language_class is None:
        return language_value

    try:
        return language_class(language_value)
    except TypeError:
        return language_value


def _iter_declaration_nodes(
    node: Any,
    declarations: dict[str, str],
    *,
    max_depth: int,
    depth: int = 0,
) -> tuple[tuple[Any, str], ...]:
    if depth > max_depth:
        return ()

    matches: list[tuple[Any, str]] = []
    kind = declarations.get(node.type)
    if kind is not None:
        matches.append((node, kind))

    if depth == max_depth:
        return tuple(matches)

    for child in node.children:
        matches.extend(
            _iter_declaration_nodes(
                child,
                declarations,
                max_depth=max_depth,
                depth=depth + 1,
            ),
        )
    return tuple(matches)


def _symbol_from_node(
    discovered_file: DiscoveredFile,
    language: str,
    node: Any,
    kind: str,
) -> Symbol | None:
    name = _name_from_node(node)
    if name is None:
        return None

    return Symbol(
        id=_symbol_id(
            discovered_file.repository_id, discovered_file.path, language, name
        ),
        file_id=_file_id(discovered_file.repository_id, discovered_file.path),
        name=name,
        kind=kind,
        qualified_name=name,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
    )


def _name_from_node(node: Any) -> str | None:
    field_name = _name_from_field(node)
    if field_name is not None:
        return field_name

    descendant_name = _name_from_descendants(node, max_depth=3)
    if descendant_name is not None:
        return descendant_name

    return None


def _name_from_field(node: Any) -> str | None:
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if child_by_field_name is None:
        return None

    name_node = child_by_field_name("name")
    if name_node is None:
        return None

    if name_node.type in _NAME_NODE_TYPES:
        return _node_text(name_node)

    return _name_from_descendants(name_node, max_depth=2)


def _name_from_descendants(
    node: Any,
    *,
    max_depth: int,
    depth: int = 0,
) -> str | None:
    for child in node.children:
        if child.type in _NAME_NODE_TYPES:
            return _node_text(child)

        if depth < max_depth:
            nested_name = _name_from_descendants(
                child,
                max_depth=max_depth,
                depth=depth + 1,
            )
            if nested_name is not None:
                return nested_name
    return None


def _node_text(node: Any) -> str | None:
    text = getattr(node, "text", b"")
    if isinstance(text, bytes):
        return text.decode("utf-8", errors="replace")
    if text:
        return str(text)
    return None


def _symbol_edge(discovered_file: DiscoveredFile, symbol: Symbol) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="symbol",
        target_id=symbol.id,
        relation_type="declares_symbol",
        confidence=TREE_SITTER_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=TREE_SITTER_CONFIDENCE,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "symbol_name": symbol.name,
                    "symbol_kind": symbol.kind,
                },
            ),
        ),
    )


def _typescript_route_contracts(
    discovered_file: DiscoveredFile,
    source: bytes,
) -> tuple[Contract, ...]:
    if discovered_file.language not in {"JavaScript", "TypeScript"}:
        return ()

    route_path = _typescript_route_path(discovered_file.path)
    if route_path is None:
        return ()

    text = source.decode("utf-8", errors="replace")
    contracts: list[Contract] = []
    seen_methods: set[str] = set()
    for match in TYPESCRIPT_ROUTE_METHOD_PATTERN.finditer(text):
        method = match.group("method").upper()
        if method not in HTTP_METHODS or method in seen_methods:
            continue
        seen_methods.add(method)
        line_number = text.count("\n", 0, match.start()) + 1
        contracts.append(
            Contract(
                id=_route_contract_id(
                    discovered_file.repository_id, route_path, method
                ),
                name=f"{method} {route_path}",
                kind="http",
                path=route_path,
                method=method,
                operation_id=f"{method} {route_path}",
                request_dtos=_request_dtos_from_parameters(match.group("parameters")),
                evidence=(
                    Evidence(
                        file_path=discovered_file.path,
                        extractor_name=EXTRACTOR_NAME,
                        confidence=TREE_SITTER_CONFIDENCE,
                        start_line=line_number,
                        end_line=line_number,
                        metadata={
                            "repository_id": discovered_file.repository_id,
                            "framework": "nextjs",
                            "method": method,
                            "route": route_path,
                        },
                    ),
                ),
            ),
        )
    return tuple(contracts)


def _typescript_route_services(
    discovered_file: DiscoveredFile,
    contracts: tuple[Contract, ...],
) -> tuple[Service, ...]:
    if not contracts:
        return ()

    service_name = _typescript_route_service_name(discovered_file.path)
    if service_name is None:
        return ()

    return (
        Service(
            id=_service_id(discovered_file.repository_id, service_name),
            name=service_name,
            repository_id=discovered_file.repository_id,
            description="Next.js API route service",
            aliases=(f"{service_name} API",),
            evidence=(
                Evidence(
                    file_path=discovered_file.path,
                    extractor_name=EXTRACTOR_NAME,
                    confidence=TREE_SITTER_CONFIDENCE,
                    start_line=contracts[0].evidence[0].start_line
                    if contracts[0].evidence
                    else None,
                    end_line=contracts[0].evidence[0].end_line
                    if contracts[0].evidence
                    else None,
                    metadata={
                        "repository_id": discovered_file.repository_id,
                        "framework": "nextjs",
                        "service_name": service_name,
                    },
                ),
            ),
        ),
    )


def _typescript_route_path(file_path: str) -> str | None:
    parts = file_path.split("/")
    if len(parts) < 3 or parts[-1] not in {
        "route.js",
        "route.jsx",
        "route.ts",
        "route.tsx",
    }:
        return None

    try:
        app_index = parts.index("app")
        api_index = app_index + 1
        if parts[api_index] != "api":
            return None
    except (ValueError, IndexError):
        return None

    route_parts = [
        _route_segment(part)
        for part in parts[api_index:-1]
        if not (part.startswith("(") and part.endswith(")"))
    ]
    if not route_parts:
        return None
    return "/" + "/".join(route_parts)


def _typescript_route_service_name(file_path: str) -> str | None:
    parts = file_path.split("/")
    try:
        app_index = parts.index("app")
    except ValueError:
        return None

    if app_index == 0:
        return None

    service_name = parts[app_index - 1].strip()
    if service_name in {"src", "source"}:
        return None
    return service_name or None


def _route_segment(segment: str) -> str:
    if segment.startswith("[") and segment.endswith("]"):
        return "{" + segment[1:-1].removeprefix("...") + "}"
    return segment


def _request_dtos_from_parameters(parameters: str) -> tuple[str, ...]:
    first_parameter = parameters.split(",", maxsplit=1)[0].strip()
    if ":" not in first_parameter:
        return ()
    type_name = first_parameter.rsplit(":", maxsplit=1)[1].strip()
    type_name = type_name.split("=", maxsplit=1)[0].strip()
    return (
        (type_name,)
        if type_name and type_name not in {"Request", "NextRequest"}
        else ()
    )


def _contract_edge(discovered_file: DiscoveredFile, contract: Contract) -> Edge:
    evidence = contract.evidence[0] if contract.evidence else None
    line_number = None if evidence is None else evidence.start_line
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="contract",
        target_id=contract.id,
        relation_type="declares_contract",
        confidence=TREE_SITTER_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=TREE_SITTER_CONFIDENCE,
                start_line=line_number,
                end_line=line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "contract_kind": contract.kind,
                    "route": contract.path,
                    "method": contract.method,
                },
            ),
        ),
    )


def _producer_edge(
    discovered_file: DiscoveredFile,
    service: Service,
    contract: Contract,
) -> Edge:
    evidence = contract.evidence[0] if contract.evidence else None
    line_number = None if evidence is None else evidence.start_line
    return Edge(
        source_type="service",
        source_id=service.id,
        target_type="contract",
        target_id=contract.id,
        relation_type="produces",
        confidence=TREE_SITTER_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=TREE_SITTER_CONFIDENCE,
                start_line=line_number,
                end_line=line_number,
                metadata={
                    "repository_id": discovered_file.repository_id,
                    "framework": "nextjs",
                    "service_id": service.id,
                    "service_name": service.name,
                    "route": contract.path,
                    "method": contract.method,
                },
            ),
        ),
    )


def _symbol_id(
    repository_id: str,
    path: str,
    language: str,
    qualified_name: str,
) -> str:
    language_slug = slugify_identifier(language, fallback="language")
    symbol_slug = slugify_identifier(qualified_name, fallback="symbol")
    return f"symbol:{repository_id}:{path}:{language_slug}:{symbol_slug}"


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _service_id(repository_id: str, name: str) -> str:
    name_slug = slugify_identifier(name, fallback="service")
    return f"service:{repository_id}:{name_slug}"


def _route_contract_id(repository_id: str, route_path: str, method: str) -> str:
    return canonical_http_contract_id(repository_id, method, route_path)
