from __future__ import annotations

import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from code_scout.core.errors import ExtractionError
from code_scout.discovery import DiscoveredFile
from code_scout.domain.normalize import normalized_symbol_id
from code_scout.models.facts import edge_to_relation_facts, relation_fact_to_active_fact
from code_scout.models.graph import Edge, Evidence, Symbol
from code_scout.models.workspace import RepositoryConfig, Workspace
from code_scout.utils.identifiers import slugify_identifier
from code_scout.workspace.config import ExtractorEntryConfig

DEFAULT_TYPESCRIPT_LSP_COMMAND = ("typescript-language-server", "--stdio")
TYPESCRIPT_LSP_EXTENSIONS = frozenset({".cts", ".mts", ".ts", ".tsx"})
TYPESCRIPT_LSP_SETUP_MESSAGE = "TypeScript language server is unavailable."
TYPESCRIPT_LSP_SETUP_HINT = (
    "Install `typescript-language-server` in the repository toolchain or configure "
    "`extractors.lsp.command` in `.code-scout/config.yaml`, then rerun "
    "`code-scout scan --extractor lsp`."
)
LSP_RELATION_TYPES = {
    "definition": "lsp_definition",
    "reference": "lsp_reference",
    "implementation": "lsp_implementation",
}


@dataclass(frozen=True)
class LSPDocumentSymbol:
    repository_id: str
    file_path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    qualified_name: str | None = None
    detail: str | None = None
    confidence: float = 0.9
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class LSPRelationship:
    repository_id: str
    source_path: str
    source_line: int
    target_path: str
    target_line: int
    kind: Literal["definition", "reference", "implementation", "type_definition"]
    confidence: float = 0.85
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class TypeScriptLSPRun:
    repository_id: str
    repository_root: str
    mode: str
    command: tuple[str, ...] = ()
    connect: str | None = None
    document_symbols: tuple[LSPDocumentSymbol, ...] = ()
    relationships: tuple[LSPRelationship, ...] = ()


@dataclass(frozen=True)
class TypeScriptLSPExtraction:
    runs: tuple[TypeScriptLSPRun, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    edges: tuple[Edge, ...] = ()


class TypeScriptLSPAdapter(Protocol):
    def start(
        self,
        *,
        repository_id: str,
        repository_root: str,
        command: tuple[str, ...],
    ) -> TypeScriptLSPRun: ...

    def connect(
        self,
        *,
        repository_id: str,
        repository_root: str,
        address: str,
    ) -> TypeScriptLSPRun: ...

    def collect(
        self,
        *,
        run: TypeScriptLSPRun,
        discovered_files: tuple[DiscoveredFile, ...],
    ) -> TypeScriptLSPRun: ...


class TypeScriptLSPProcessAdapter:
    def start(
        self,
        *,
        repository_id: str,
        repository_root: str,
        command: tuple[str, ...],
    ) -> TypeScriptLSPRun:
        try:
            process = subprocess.Popen(
                command,
                cwd=repository_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise ExtractionError(
                TYPESCRIPT_LSP_SETUP_MESSAGE,
                hint=TYPESCRIPT_LSP_SETUP_HINT,
            ) from error

        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

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
        host, port = _parse_lsp_address(address)
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError as error:
            raise ExtractionError(
                TYPESCRIPT_LSP_SETUP_MESSAGE,
                hint=(
                    f"Start a TypeScript language server at `{address}` or update "
                    "`extractors.lsp.connect` in `.code-scout/config.yaml`."
                ),
            ) from error

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


def extract_typescript_lsp(
    workspace: Workspace,
    discovered_files: tuple[DiscoveredFile, ...],
    config: ExtractorEntryConfig,
    *,
    adapter: TypeScriptLSPAdapter | None = None,
) -> TypeScriptLSPExtraction:
    if not config.enabled:
        return TypeScriptLSPExtraction()

    lsp_adapter = TypeScriptLSPProcessAdapter() if adapter is None else adapter
    repository_ids = _repositories_with_typescript(discovered_files)
    runs: list[TypeScriptLSPRun] = []

    for repository in workspace.repositories:
        if repository.id not in repository_ids:
            continue
        repository_root = _repository_root(workspace.root_path, repository)
        if config.connect:
            run = lsp_adapter.connect(
                repository_id=repository.id,
                repository_root=repository_root,
                address=config.connect,
            )
        else:
            run = lsp_adapter.start(
                repository_id=repository.id,
                repository_root=repository_root,
                command=typescript_lsp_command(config),
            )
        runs.append(
            lsp_adapter.collect(
                run=run,
                discovered_files=_typescript_files_for_repository(
                    discovered_files,
                    repository.id,
                ),
            ),
        )

    return TypeScriptLSPExtraction(
        runs=tuple(runs),
    )


def typescript_lsp_command(config: ExtractorEntryConfig) -> tuple[str, ...]:
    return config.command or DEFAULT_TYPESCRIPT_LSP_COMMAND


def typescript_lsp_validation_edges(
    runs: tuple[TypeScriptLSPRun, ...],
    *,
    authoritative_symbols: tuple[Symbol, ...],
    authoritative_edges: tuple[Edge, ...],
) -> tuple[Edge, ...]:
    symbol_matches = _matched_lsp_symbols(runs, authoritative_symbols)
    symbol_index = _authoritative_symbol_index(authoritative_symbols)
    edges: list[Edge] = []
    seen_edges: set[tuple[str, str, str, str, str]] = set()

    for run in runs:
        for document_symbol in run.document_symbols:
            matched_symbol = symbol_matches.get(_document_symbol_key(document_symbol))
            if matched_symbol is None:
                continue
            declaring_edge = _find_declaring_edge(matched_symbol, authoritative_edges)
            if declaring_edge is None:
                continue
            edges.append(
                _dedupe_edge(
                    seen_edges,
                    Edge(
                        source_type=declaring_edge.source_type,
                        source_id=declaring_edge.source_id,
                        target_type=declaring_edge.target_type,
                        target_id=declaring_edge.target_id,
                        relation_type=declaring_edge.relation_type,
                        confidence=document_symbol.confidence,
                        evidence=(
                            _validation_symbol_evidence(
                                document_symbol,
                                validated_edge=declaring_edge,
                            ),
                        ),
                    ),
                ),
            )

        for relationship in run.relationships:
            source_type, source_id = _endpoint_for_line(
                symbol_index,
                relationship.repository_id,
                relationship.source_path,
                relationship.source_line,
            )
            target_type, target_id = _endpoint_for_line(
                symbol_index,
                relationship.repository_id,
                relationship.target_path,
                relationship.target_line,
            )
            matching_edge = _find_matching_edge(
                authoritative_edges,
                source_type=source_type,
                source_id=source_id,
                target_type=target_type,
                target_id=target_id,
            )
            if matching_edge is None:
                continue
            edges.append(
                _dedupe_edge(
                    seen_edges,
                    Edge(
                        source_type=matching_edge.source_type,
                        source_id=matching_edge.source_id,
                        target_type=matching_edge.target_type,
                        target_id=matching_edge.target_id,
                        relation_type=matching_edge.relation_type,
                        confidence=relationship.confidence,
                        evidence=(
                            _validation_relationship_evidence(
                                relationship,
                                validated_edge=matching_edge,
                            ),
                        ),
                    ),
                ),
            )

    return tuple(edge for edge in edges if edge is not None)


def _repositories_with_typescript(
    discovered_files: tuple[DiscoveredFile, ...],
) -> frozenset[str]:
    return frozenset(
        discovered_file.repository_id
        for discovered_file in discovered_files
        if discovered_file.extension in TYPESCRIPT_LSP_EXTENSIONS
    )


def _typescript_files_for_repository(
    discovered_files: tuple[DiscoveredFile, ...],
    repository_id: str,
) -> tuple[DiscoveredFile, ...]:
    return tuple(
        discovered_file
        for discovered_file in discovered_files
        if discovered_file.repository_id == repository_id
        and discovered_file.extension in TYPESCRIPT_LSP_EXTENSIONS
    )


def _lsp_symbols(runs: tuple[TypeScriptLSPRun, ...]) -> tuple[Symbol, ...]:
    symbols: list[Symbol] = []
    seen_ids: set[str] = set()
    for run in runs:
        for document_symbol in run.document_symbols:
            symbol = _symbol_from_lsp(document_symbol)
            if symbol.id in seen_ids:
                continue
            seen_ids.add(symbol.id)
            symbols.append(symbol)
    return tuple(symbols)


def _lsp_edges(runs: tuple[TypeScriptLSPRun, ...]) -> tuple[Edge, ...]:
    symbol_index = _symbol_index(runs)
    edges: list[Edge] = []
    seen_edges: set[tuple[str, str, str, str, str]] = set()

    for run in runs:
        for document_symbol in run.document_symbols:
            symbol = _symbol_from_lsp(document_symbol)
            edges.append(
                _dedupe_edge(
                    seen_edges,
                    Edge(
                        source_type="file",
                        source_id=_file_id(
                            document_symbol.repository_id,
                            document_symbol.file_path,
                        ),
                        target_type="symbol",
                        target_id=symbol.id,
                        relation_type="declares_symbol",
                        confidence=document_symbol.confidence,
                        evidence=(_symbol_evidence(document_symbol),),
                    ),
                ),
            )

        for relationship in run.relationships:
            source_type, source_id = _endpoint_for_line(
                symbol_index,
                relationship.repository_id,
                relationship.source_path,
                relationship.source_line,
            )
            target_type, target_id = _endpoint_for_line(
                symbol_index,
                relationship.repository_id,
                relationship.target_path,
                relationship.target_line,
            )
            edges.append(
                _dedupe_edge(
                    seen_edges,
                    Edge(
                        source_type=source_type,
                        source_id=source_id,
                        target_type=target_type,
                        target_id=target_id,
                        relation_type=LSP_RELATION_TYPES[relationship.kind],
                        confidence=relationship.confidence,
                        evidence=(_relationship_evidence(relationship),),
                    ),
                ),
            )

    return tuple(edge for edge in edges if edge is not None)


def _matched_lsp_symbols(
    runs: tuple[TypeScriptLSPRun, ...],
    authoritative_symbols: tuple[Symbol, ...],
) -> dict[tuple[str, str, str, str], Symbol]:
    authoritative_by_normalized_id = {
        normalized_symbol_id(symbol): symbol for symbol in authoritative_symbols
    }
    matches: dict[tuple[str, str, str, str], Symbol] = {}
    for run in runs:
        for document_symbol in run.document_symbols:
            lsp_symbol = _symbol_from_lsp(document_symbol)
            match = authoritative_by_normalized_id.get(normalized_symbol_id(lsp_symbol))
            if match is None:
                continue
            matches[_document_symbol_key(document_symbol)] = match
    return matches


def _document_symbol_key(
    document_symbol: LSPDocumentSymbol,
) -> tuple[str, str, str, str]:
    return (
        document_symbol.repository_id,
        document_symbol.file_path,
        document_symbol.qualified_name or document_symbol.name,
        document_symbol.kind,
    )


def _authoritative_symbol_index(
    authoritative_symbols: tuple[Symbol, ...],
) -> dict[tuple[str, str], tuple[Symbol, ...]]:
    symbols_by_file: dict[tuple[str, str], list[Symbol]] = {}
    for symbol in authoritative_symbols:
        repository_id, file_path = _symbol_file_parts(symbol)
        if repository_id is None or file_path is None:
            continue
        symbols_by_file.setdefault((repository_id, file_path), []).append(symbol)
    return {
        key: tuple(
            sorted(
                symbols,
                key=lambda symbol: (
                    (symbol.end_line or symbol.start_line or 0)
                    - (symbol.start_line or 0),
                    symbol.name,
                ),
            ),
        )
        for key, symbols in symbols_by_file.items()
    }


def _find_declaring_edge(
    matched_symbol: Symbol,
    authoritative_edges: tuple[Edge, ...],
) -> Edge | None:
    normalized_target_id = normalized_symbol_id(matched_symbol)
    for edge in authoritative_edges:
        if edge.relation_type != "declares_symbol" or edge.target_type != "symbol":
            continue
        if _normalized_endpoint_id(edge.target_type, edge.target_id) != (
            normalized_target_id
        ):
            continue
        return edge
    return None


def _find_matching_edge(
    authoritative_edges: tuple[Edge, ...],
    *,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
) -> Edge | None:
    for edge in authoritative_edges:
        if edge.source_type != source_type or edge.target_type != target_type:
            continue
        if _normalized_endpoint_id(edge.source_type, edge.source_id) != (
            _normalized_endpoint_id(source_type, source_id)
        ):
            continue
        if _normalized_endpoint_id(edge.target_type, edge.target_id) != (
            _normalized_endpoint_id(target_type, target_id)
        ):
            continue
        return edge
    return None


def _normalized_endpoint_id(node_type: str, node_id: str) -> str:
    if node_type != "symbol":
        return node_id
    repository_id, file_path, qualified_name = _symbol_id_parts(node_id)
    if repository_id is None or file_path is None or qualified_name is None:
        return node_id
    return normalized_symbol_id(
        Symbol(
            id=node_id,
            file_id=_file_id(repository_id, file_path),
            name=qualified_name,
            kind="unknown",
            qualified_name=qualified_name,
        ),
    )


def _validation_symbol_evidence(
    document_symbol: LSPDocumentSymbol,
    *,
    validated_edge: Edge,
) -> Evidence:
    evidence = _symbol_evidence(document_symbol)
    return Evidence(
        file_path=evidence.file_path,
        extractor_name=evidence.extractor_name,
        confidence=evidence.confidence,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        metadata={
            **evidence.metadata,
            "validation_kind": "declaration",
            **_validated_edge_metadata(validated_edge),
        },
    )


def _validation_relationship_evidence(
    relationship: LSPRelationship,
    *,
    validated_edge: Edge,
) -> Evidence:
    evidence = _relationship_evidence(relationship)
    return Evidence(
        file_path=evidence.file_path,
        extractor_name=evidence.extractor_name,
        confidence=evidence.confidence,
        start_line=evidence.start_line,
        end_line=evidence.end_line,
        metadata={
            **evidence.metadata,
            "validation_kind": relationship.kind,
            **_validated_edge_metadata(validated_edge),
        },
    )


def _validated_edge_metadata(edge: Edge) -> dict[str, object]:
    metadata: dict[str, object] = {
        "validated_relation": edge.relation_type,
    }
    active_fact_ids = tuple(
        relation_fact_to_active_fact(fact).id for fact in edge_to_relation_facts(edge)
    )
    if active_fact_ids:
        metadata["validated_fact_id"] = active_fact_ids[0]
    validated_extractors = tuple(
        sorted({evidence.extractor_name for evidence in edge.evidence})
    )
    if validated_extractors:
        metadata["validated_extractors"] = validated_extractors
    return metadata


def _symbol_file_parts(symbol: Symbol) -> tuple[str | None, str | None]:
    parts = symbol.file_id.split(":", maxsplit=2)
    if len(parts) != 3 or parts[0] != "file":
        return None, None
    return parts[1], parts[2]


def _symbol_id_parts(symbol_id: str) -> tuple[str | None, str | None, str | None]:
    parts = symbol_id.split(":")
    if len(parts) < 5 or parts[0] != "symbol":
        return None, None, None
    repository_id = parts[1]
    file_path = ":".join(parts[2:-2])
    qualified_name = parts[-1]
    return repository_id, file_path, qualified_name


def _symbol_index(
    runs: tuple[TypeScriptLSPRun, ...],
) -> dict[tuple[str, str], tuple[Symbol, ...]]:
    symbols_by_file: dict[tuple[str, str], list[Symbol]] = {}
    for run in runs:
        for document_symbol in run.document_symbols:
            symbols_by_file.setdefault(
                (document_symbol.repository_id, document_symbol.file_path),
                [],
            ).append(_symbol_from_lsp(document_symbol))
    return {
        key: tuple(
            sorted(
                symbols,
                key=lambda symbol: (
                    (symbol.end_line or symbol.start_line or 0)
                    - (symbol.start_line or 0),
                    symbol.name,
                ),
            ),
        )
        for key, symbols in symbols_by_file.items()
    }


def _endpoint_for_line(
    symbol_index: dict[tuple[str, str], tuple[Symbol, ...]],
    repository_id: str,
    file_path: str,
    line: int,
) -> tuple[Literal["file", "symbol"], str]:
    for symbol in symbol_index.get((repository_id, file_path), ()):
        start_line = symbol.start_line or 1
        end_line = symbol.end_line or start_line
        if start_line <= line <= end_line:
            return "symbol", symbol.id
    return "file", _file_id(repository_id, file_path)


def _symbol_from_lsp(document_symbol: LSPDocumentSymbol) -> Symbol:
    qualified_name = document_symbol.qualified_name or document_symbol.name
    return Symbol(
        id=_symbol_id(
            document_symbol.repository_id,
            document_symbol.file_path,
            qualified_name,
        ),
        file_id=_file_id(document_symbol.repository_id, document_symbol.file_path),
        name=document_symbol.name,
        kind=f"lsp_{document_symbol.kind}",
        qualified_name=qualified_name,
        start_line=document_symbol.start_line,
        end_line=document_symbol.end_line,
    )


def _symbol_evidence(document_symbol: LSPDocumentSymbol) -> Evidence:
    metadata = {
        "repository_id": document_symbol.repository_id,
        "lsp_kind": document_symbol.kind,
    }
    if document_symbol.detail is not None:
        metadata["detail"] = document_symbol.detail
    if document_symbol.metadata:
        metadata.update(document_symbol.metadata)
    return Evidence(
        file_path=document_symbol.file_path,
        extractor_name="lsp",
        confidence=document_symbol.confidence,
        start_line=document_symbol.start_line,
        end_line=document_symbol.end_line,
        metadata=metadata,
    )


def _relationship_evidence(relationship: LSPRelationship) -> Evidence:
    metadata = {
        "repository_id": relationship.repository_id,
        "relationship": relationship.kind,
        "source_path": relationship.source_path,
        "source_line": relationship.source_line,
        "target_path": relationship.target_path,
        "target_line": relationship.target_line,
    }
    if relationship.metadata:
        metadata.update(relationship.metadata)
    return Evidence(
        file_path=relationship.source_path,
        extractor_name="lsp",
        confidence=relationship.confidence,
        start_line=relationship.source_line,
        end_line=relationship.source_line,
        metadata=metadata,
    )


def _dedupe_edge(
    seen_edges: set[tuple[str, str, str, str, str]],
    edge: Edge,
) -> Edge | None:
    key = (
        edge.source_type,
        edge.source_id,
        edge.target_type,
        edge.target_id,
        edge.relation_type,
    )
    if key in seen_edges:
        return None
    seen_edges.add(key)
    return edge


def _symbol_id(repository_id: str, path: str, qualified_name: str) -> str:
    symbol_slug = slugify_identifier(qualified_name, fallback="symbol")
    return f"symbol:{repository_id}:{path}:lsp:{symbol_slug}"


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _repository_root(root_path: str, repository: RepositoryConfig) -> str:
    repository_path = Path(repository.path)
    if repository_path.is_absolute():
        return str(repository_path.resolve(strict=True))
    return str((Path(root_path) / repository_path).resolve(strict=True))


def _parse_lsp_address(address: str) -> tuple[str, int]:
    normalized = address.removeprefix("tcp://")
    host, separator, port_text = normalized.rpartition(":")
    if not separator or not host:
        raise ExtractionError(
            f"Invalid TypeScript language server address `{address}`.",
            "Use `host:port` or `tcp://host:port` in `extractors.lsp.connect`.",
        )
    try:
        return host, int(port_text)
    except ValueError as error:
        raise ExtractionError(
            f"Invalid TypeScript language server address `{address}`.",
            "Use a numeric port in `extractors.lsp.connect`.",
        ) from error
