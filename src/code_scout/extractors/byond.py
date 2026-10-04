from __future__ import annotations

import re
from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.models.graph import Edge, Evidence, Symbol
from code_scout.utils.identifiers import slugify_identifier

EXTRACTOR_NAME = "byond"
BYOND_CONFIDENCE = 0.8

PATH_DECLARATION_PATTERN = re.compile(
    r"^(?P<path>/[A-Za-z_]\w*(?:/[A-Za-z_]\w*)*)"
    r"(?:\s*\([^)]*\))?(?:\s|$)",
)
PROC_DECLARATION_PATTERN = re.compile(
    r"^\s*(?P<path>proc/[A-Za-z_]\w*(?:/[A-Za-z_]\w*)*)\s*\(",
)

PATH_KIND_BY_SEGMENT = {
    "mob": "mob",
    "obj": "object",
    "object": "object",
    "area": "area",
    "turf": "turf",
    "proc": "procedure",
    "datum": "datum",
}


class ByondExtractor:
    """Extract BYOND DM path and procedure declarations."""

    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        if discovered_file.extension.lower() not in {".dm", ".dme"}:
            return SymbolExtraction()

        source = Path(discovered_file.absolute_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
        symbols = tuple(_symbols_from_source(discovered_file, source))
        return SymbolExtraction(
            symbols=symbols,
            edges=tuple(_symbol_edge(discovered_file, symbol) for symbol in symbols),
        )


def _symbols_from_source(
    discovered_file: DiscoveredFile,
    source: str,
) -> tuple[Symbol, ...]:
    symbols: list[Symbol] = []
    current_path: str | None = None

    for line_number, line in enumerate(source.splitlines(), start=1):
        path_match = PATH_DECLARATION_PATTERN.match(line)
        if path_match is not None:
            path = path_match.group("path")
            current_path = path
            symbols.append(_symbol(discovered_file, path, line_number))
            continue

        proc_match = PROC_DECLARATION_PATTERN.match(line)
        if proc_match is None:
            continue

        path = _resolve_proc_path(current_path, proc_match.group("path"))
        symbols.append(_symbol(discovered_file, path, line_number))

    return tuple(symbols)


def _resolve_proc_path(current_path: str | None, proc_path: str) -> str:
    if current_path is None:
        return f"/{proc_path}"
    return f"{current_path.rstrip('/')}/{proc_path}"


def _symbol(discovered_file: DiscoveredFile, path: str, line_number: int) -> Symbol:
    return Symbol(
        id=_symbol_id(discovered_file.repository_id, discovered_file.path, path),
        file_id=_file_id(discovered_file.repository_id, discovered_file.path),
        name=path,
        kind=_kind_for_path(path),
        qualified_name=path,
        start_line=line_number,
        end_line=line_number,
    )


def _kind_for_path(path: str) -> str:
    segments = tuple(segment for segment in path.split("/") if segment)
    if "proc" in segments:
        return "procedure"
    if not segments:
        return "path"
    return PATH_KIND_BY_SEGMENT.get(segments[0], "path")


def _symbol_edge(discovered_file: DiscoveredFile, symbol: Symbol) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="symbol",
        target_id=symbol.id,
        relation_type="declares_symbol",
        confidence=BYOND_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=BYOND_CONFIDENCE,
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


def _symbol_id(repository_id: str, path: str, qualified_name: str) -> str:
    symbol_slug = slugify_identifier(qualified_name, fallback="symbol")
    return f"symbol:{repository_id}:{path}:byond:{symbol_slug}"


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"
