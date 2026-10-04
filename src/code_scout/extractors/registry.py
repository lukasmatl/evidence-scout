from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from code_scout.discovery import DiscoveredFile
from code_scout.models.graph import Contract, Edge, Service, Symbol
from code_scout.utils.language_map import ExtractorTier, tier_for_extension

logger = logging.getLogger("code_scout.extractors")


@dataclass(frozen=True)
class RepositoryFrameworkHint:
    repository_id: str
    framework: str


@dataclass(frozen=True)
class SymbolExtraction:
    symbols: tuple[Symbol, ...] = ()
    services: tuple[Service, ...] = ()
    contracts: tuple[Contract, ...] = ()
    framework_hints: tuple[RepositoryFrameworkHint, ...] = ()
    edges: tuple[Edge, ...] = ()


class SymbolExtractor(Protocol):
    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction: ...


class _EmptyExtractor:
    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        return SymbolExtraction()


class ExtractorRegistry:
    """Routes discovered files to language symbol extractors."""

    def __init__(
        self,
        *,
        tree_sitter_enabled: bool = True,
        heuristic_enabled: bool = True,
        dotnet_enabled: bool = True,
        byond_enabled: bool = True,
        tree_sitter_extractor: SymbolExtractor | None = None,
        heuristic_extractor: SymbolExtractor | None = None,
        dotnet_extractor: SymbolExtractor | None = None,
        byond_extractor: SymbolExtractor | None = None,
    ) -> None:
        self.tree_sitter_enabled = tree_sitter_enabled
        self.heuristic_enabled = heuristic_enabled
        self.dotnet_enabled = dotnet_enabled
        self.byond_enabled = byond_enabled
        self.tree_sitter_extractor = tree_sitter_extractor or _EmptyExtractor()
        self.heuristic_extractor = heuristic_extractor or _EmptyExtractor()
        self.dotnet_extractor = dotnet_extractor or _EmptyExtractor()
        self.byond_extractor = byond_extractor or _EmptyExtractor()

    def extract(
        self,
        discovered_files: tuple[DiscoveredFile, ...],
    ) -> SymbolExtraction:
        symbols: list[Symbol] = []
        services: list[Service] = []
        contracts: list[Contract] = []
        framework_hints: list[RepositoryFrameworkHint] = []
        edges: list[Edge] = []

        for discovered_file in discovered_files:
            for extractor in self._extractors_for_file(discovered_file):
                try:
                    extraction = extractor.extract(discovered_file)
                except Exception as error:
                    logger.warning(
                        "Skipping symbol extraction for %s after %s failed: %s",
                        discovered_file.path,
                        extractor.__class__.__name__,
                        error,
                    )
                    continue

                symbols.extend(extraction.symbols)
                services.extend(extraction.services)
                contracts.extend(extraction.contracts)
                framework_hints.extend(extraction.framework_hints)
                edges.extend(extraction.edges)

        return SymbolExtraction(
            symbols=tuple(symbols),
            services=tuple(services),
            contracts=tuple(contracts),
            framework_hints=tuple(
                {
                    (hint.repository_id, hint.framework): hint
                    for hint in framework_hints
                }.values(),
            ),
            edges=tuple(edges),
        )

    def _extractor_for_tier(
        self,
        tier: ExtractorTier,
    ) -> SymbolExtractor | None:
        if tier == "tree_sitter" and self.tree_sitter_enabled:
            return self.tree_sitter_extractor
        if tier == "heuristic" and self.heuristic_enabled:
            return self.heuristic_extractor
        if tier == "dotnet" and self.dotnet_enabled:
            return self.dotnet_extractor
        if tier == "byond" and self.byond_enabled:
            return self.byond_extractor
        return None

    def _extractors_for_file(
        self,
        discovered_file: DiscoveredFile,
    ) -> tuple[SymbolExtractor, ...]:
        extractors: list[SymbolExtractor] = []
        tier_extractor = self._extractor_for_tier(
            tier_for_extension(discovered_file.extension),
        )
        if tier_extractor is not None:
            extractors.append(tier_extractor)
        if discovered_file.extension.lower() == ".cs" and self.dotnet_enabled:
            extractors.append(self.dotnet_extractor)
        return tuple(extractors)
