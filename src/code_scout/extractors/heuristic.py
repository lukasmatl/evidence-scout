from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from re import Pattern

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.models.graph import Edge, Evidence, Symbol
from code_scout.utils.identifiers import slugify_identifier

EXTRACTOR_NAME = "heuristic"
HEURISTIC_CONFIDENCE = 0.6


@dataclass(frozen=True)
class HeuristicPattern:
    pattern: Pattern[str]
    kind: str | None = None
    kind_group: int | None = None
    name_group: int = 1


POWERSHELL_PATTERNS = (
    HeuristicPattern(re.compile(r"^function\s+([\w-]+)", re.IGNORECASE), "function"),
    HeuristicPattern(re.compile(r"^class\s+(\w+)", re.IGNORECASE), "class"),
)
OBJECTIVE_C_PATTERNS = (
    HeuristicPattern(re.compile(r"^[-+]\s*\([^)]+\)\s*(\w+)"), "method"),
    HeuristicPattern(re.compile(r"^@interface\s+(\w+)"), "class"),
    HeuristicPattern(re.compile(r"^@implementation\s+(\w+)"), "class"),
)
GROOVY_PATTERNS = (
    HeuristicPattern(
        re.compile(r"^(class|interface|enum|def)\s+(\w+)"),
        kind_group=1,
        name_group=2,
    ),
)
FORTRAN_PATTERNS = (
    HeuristicPattern(
        re.compile(
            r"^\s*(subroutine|function|module|program)\s+(\w+)",
            re.IGNORECASE,
        ),
        kind_group=1,
        name_group=2,
    ),
)
PASCAL_PATTERNS = (
    HeuristicPattern(
        re.compile(
            r"^(procedure|function|type|unit|program)\s+(\w+)",
            re.IGNORECASE,
        ),
        kind_group=1,
        name_group=2,
    ),
)
VERILOG_PATTERNS = (HeuristicPattern(re.compile(r"^module\s+(\w+)"), "module"),)
SYSTEMVERILOG_PATTERNS = (
    HeuristicPattern(
        re.compile(r"^(module|interface|class|package)\s+(\w+)"),
        kind_group=1,
        name_group=2,
    ),
)
LUAU_PATTERNS = (
    HeuristicPattern(re.compile(r"^function\s+([\w.:]+)"), "function"),
    HeuristicPattern(re.compile(r"^local\s+function\s+([\w.]+)"), "function"),
)
DM_PATH_PATTERNS = (HeuristicPattern(re.compile(r"^(/[\w/]+)"), "object"),)

LANGUAGE_PATTERNS: dict[str, tuple[HeuristicPattern, ...]] = {
    "PowerShell": POWERSHELL_PATTERNS,
    "Objective-C": OBJECTIVE_C_PATTERNS,
    "Groovy": GROOVY_PATTERNS,
    "Fortran": FORTRAN_PATTERNS,
    "Pascal": PASCAL_PATTERNS,
    "Delphi": PASCAL_PATTERNS,
    "Verilog": VERILOG_PATTERNS,
    "SystemVerilog": SYSTEMVERILOG_PATTERNS,
    "Luau": LUAU_PATTERNS,
    "DM": DM_PATH_PATTERNS,
}
COMPONENT_LANGUAGES = frozenset({"Vue", "Svelte", "Astro"})


class HeuristicExtractor:
    """Extract fallback symbols with language-specific regex patterns."""

    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        language = discovered_file.language
        if language in COMPONENT_LANGUAGES:
            return _component_extraction(discovered_file, language)
        if language is None:
            return SymbolExtraction()

        patterns = LANGUAGE_PATTERNS.get(language)
        if patterns is None:
            return SymbolExtraction()

        source = Path(discovered_file.absolute_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
        symbols = tuple(
            _symbols_from_source(discovered_file, language, source, patterns)
        )
        edges = tuple(_symbol_edge(discovered_file, symbol) for symbol in symbols)
        return SymbolExtraction(symbols=symbols, edges=edges)


def _symbols_from_source(
    discovered_file: DiscoveredFile,
    language: str,
    source: str,
    patterns: tuple[HeuristicPattern, ...],
) -> tuple[Symbol, ...]:
    symbols: list[Symbol] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        for pattern in patterns:
            match = pattern.pattern.match(line)
            if match is None:
                continue

            name = match.group(pattern.name_group)
            kind = (
                match.group(pattern.kind_group).lower()
                if pattern.kind_group is not None
                else pattern.kind
            )
            if kind is None:
                continue

            symbols.append(
                _symbol(
                    discovered_file,
                    language,
                    name=name,
                    kind=kind,
                    qualified_name=name,
                    start_line=line_number,
                    end_line=line_number,
                ),
            )
            break
    return tuple(symbols)


def _component_extraction(
    discovered_file: DiscoveredFile,
    language: str,
) -> SymbolExtraction:
    source = Path(discovered_file.absolute_path).read_text(
        encoding="utf-8",
        errors="replace",
    )
    if language == "Vue" and "export default" not in source:
        return SymbolExtraction()

    symbol = _symbol(
        discovered_file,
        language,
        name=_component_name(discovered_file.path),
        kind="component",
        qualified_name=_component_name(discovered_file.path),
        start_line=1,
        end_line=max(1, len(source.splitlines())),
    )
    return SymbolExtraction(
        symbols=(symbol,),
        edges=(_symbol_edge(discovered_file, symbol),),
    )


def _component_name(path: str) -> str:
    stem = Path(path).stem
    parts = tuple(part for part in re.split(r"[^A-Za-z0-9]+", stem) if part)
    if not parts:
        return "Component"
    if len(parts) == 1:
        return parts[0][:1].upper() + parts[0][1:]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _symbol(
    discovered_file: DiscoveredFile,
    language: str,
    *,
    name: str,
    kind: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
) -> Symbol:
    return Symbol(
        id=_symbol_id(
            discovered_file.repository_id,
            discovered_file.path,
            language,
            qualified_name,
        ),
        file_id=_file_id(discovered_file.repository_id, discovered_file.path),
        name=name,
        kind=kind,
        qualified_name=qualified_name,
        start_line=start_line,
        end_line=end_line,
    )


def _symbol_edge(discovered_file: DiscoveredFile, symbol: Symbol) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(discovered_file.repository_id, discovered_file.path),
        target_type="symbol",
        target_id=symbol.id,
        relation_type="declares_symbol",
        confidence=HEURISTIC_CONFIDENCE,
        evidence=(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=HEURISTIC_CONFIDENCE,
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
