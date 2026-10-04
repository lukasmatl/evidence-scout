from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import (
    ExtractorRegistry,
    RepositoryFrameworkHint,
    SymbolExtraction,
)
from code_scout.models.graph import Contract, Edge, Service, Symbol


def test_extractor_registry_defaults_to_all_tiers_enabled() -> None:
    registry = ExtractorRegistry()

    assert registry.tree_sitter_enabled is True
    assert registry.heuristic_enabled is True
    assert registry.dotnet_enabled is True
    assert registry.byond_enabled is True


def test_symbol_extraction_defaults_to_empty_graph_facts() -> None:
    extraction = SymbolExtraction()

    assert extraction.symbols == ()
    assert extraction.services == ()
    assert extraction.contracts == ()
    assert extraction.framework_hints == ()
    assert extraction.edges == ()


def test_extractor_registry_returns_empty_extraction_without_files() -> None:
    registry = ExtractorRegistry()

    extraction = registry.extract(())

    assert extraction == SymbolExtraction(symbols=(), edges=())


def test_extractor_registry_dispatches_files_by_enabled_tier() -> None:
    tree_sitter = FakeExtractor("tree_sitter")
    heuristic = FakeExtractor("heuristic")
    dotnet = FakeExtractor("dotnet")
    byond = FakeExtractor("byond")
    registry = ExtractorRegistry(
        tree_sitter_extractor=tree_sitter,
        heuristic_extractor=heuristic,
        dotnet_extractor=dotnet,
        byond_extractor=byond,
    )

    extraction = registry.extract(
        (
            _discovered_file("src/app.py", ".py"),
            _discovered_file("src/widget.vue", ".vue"),
            _discovered_file("src/App.csproj", ".csproj"),
            _discovered_file("src/world.dm", ".dm"),
        ),
    )

    assert tree_sitter.paths == ["src/app.py"]
    assert heuristic.paths == ["src/widget.vue"]
    assert dotnet.paths == ["src/App.csproj"]
    assert byond.paths == ["src/world.dm"]
    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "tree_sitter:src/app.py",
        "heuristic:src/widget.vue",
        "dotnet:src/App.csproj",
        "byond:src/world.dm",
    )
    assert tuple(edge.source_id for edge in extraction.edges) == (
        "symbol:tree_sitter:src/app.py",
        "symbol:heuristic:src/widget.vue",
        "symbol:dotnet:src/App.csproj",
        "symbol:byond:src/world.dm",
    )
    assert tuple(service.name for service in extraction.services) == (
        "dotnet:src/App.csproj",
    )
    assert tuple(contract.name for contract in extraction.contracts) == (
        "dotnet:src/App.csproj",
    )
    assert extraction.framework_hints == (
        RepositoryFrameworkHint(repository_id="repo", framework="dotnet"),
    )


def test_extractor_registry_skips_disabled_tiers_and_none_tier_files() -> None:
    tree_sitter = FakeExtractor("tree_sitter")
    heuristic = FakeExtractor("heuristic")
    dotnet = FakeExtractor("dotnet")
    byond = FakeExtractor("byond")
    registry = ExtractorRegistry(
        tree_sitter_enabled=False,
        dotnet_enabled=False,
        tree_sitter_extractor=tree_sitter,
        heuristic_extractor=heuristic,
        dotnet_extractor=dotnet,
        byond_extractor=byond,
    )

    extraction = registry.extract(
        (
            _discovered_file("src/app.py", ".py"),
            _discovered_file("src/widget.vue", ".vue"),
            _discovered_file("src/App.csproj", ".csproj"),
            _discovered_file("src/world.dm", ".dm"),
            _discovered_file("package.json", ".json"),
        ),
    )

    assert tree_sitter.paths == []
    assert heuristic.paths == ["src/widget.vue"]
    assert dotnet.paths == []
    assert byond.paths == ["src/world.dm"]
    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "heuristic:src/widget.vue",
        "byond:src/world.dm",
    )


def test_extractor_registry_logs_warning_and_continues_after_file_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree_sitter = FakeExtractor("tree_sitter", fail_paths={"src/bad.py"})
    heuristic = FakeExtractor("heuristic")
    registry = ExtractorRegistry(
        tree_sitter_extractor=tree_sitter,
        heuristic_extractor=heuristic,
    )

    with caplog.at_level(logging.WARNING, logger="code_scout.extractors"):
        extraction = registry.extract(
            (
                _discovered_file("src/good.py", ".py"),
                _discovered_file("src/bad.py", ".py"),
                _discovered_file("src/widget.vue", ".vue"),
            ),
        )

    assert tree_sitter.paths == ["src/good.py", "src/bad.py"]
    assert heuristic.paths == ["src/widget.vue"]
    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "tree_sitter:src/good.py",
        "heuristic:src/widget.vue",
    )
    assert "Skipping symbol extraction for src/bad.py" in caplog.text
    assert "boom" in caplog.text


@dataclass
class FakeExtractor:
    name: str
    fail_paths: set[str] = field(default_factory=set)
    paths: list[str] = field(default_factory=list)

    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        self.paths.append(discovered_file.path)
        if discovered_file.path in self.fail_paths:
            raise RuntimeError("boom")

        symbol = Symbol(
            id=f"symbol:{self.name}:{discovered_file.path}",
            file_id=f"file:{discovered_file.path}",
            name=f"{self.name}:{discovered_file.path}",
            kind="fixture",
        )
        edge = Edge(
            source_type="symbol",
            source_id=symbol.id,
            target_type="file",
            target_id=symbol.file_id,
            relation_type="defined_in",
            confidence=1.0,
        )
        service = Service(
            id=f"service:{self.name}:{discovered_file.path}",
            name=f"{self.name}:{discovered_file.path}",
            repository_id=discovered_file.repository_id,
        )
        contract = Contract(
            id=f"contract:{self.name}:{discovered_file.path}",
            name=f"{self.name}:{discovered_file.path}",
            kind="fixture",
        )
        framework_hints = (
            (
                RepositoryFrameworkHint(
                    repository_id=discovered_file.repository_id,
                    framework=self.name,
                ),
            )
            if self.name == "dotnet"
            else ()
        )
        return SymbolExtraction(
            symbols=(symbol,),
            services=(service,) if self.name == "dotnet" else (),
            contracts=(contract,) if self.name == "dotnet" else (),
            framework_hints=framework_hints,
            edges=(edge,),
        )


def _discovered_file(path: str, extension: str) -> DiscoveredFile:
    return DiscoveredFile(
        repository_id="repo",
        path=path,
        absolute_path=f"/workspace/{path}",
        kind="source",
        extension=extension,
        content_hash="hash",
        modified_at="2026-06-20T00:00:00+00:00",
    )
