from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction, treesitter
from code_scout.extractors.treesitter import TreeSitterExtractor


@dataclass(frozen=True)
class TreeSitterFixture:
    language: str
    extension: str
    source: str
    grammar_language: str | None = None


TREE_SITTER_FIXTURES = (
    TreeSitterFixture("Python", ".py", "class Sample:\n    pass\n"),
    TreeSitterFixture("JavaScript", ".js", "function sample() {}\n"),
    TreeSitterFixture("TypeScript", ".ts", "interface Sample {}\n"),
    TreeSitterFixture(
        "TypeScript",
        ".tsx",
        "function Sample() { return <div />; }\n",
        grammar_language="TSX",
    ),
    TreeSitterFixture("Go", ".go", "package main\nfunc Sample() {}\n"),
    TreeSitterFixture("Rust", ".rs", "fn sample() {}\n"),
    TreeSitterFixture("Java", ".java", "class Sample {}\n"),
    TreeSitterFixture("C", ".c", "struct Sample { int value; };\n"),
    TreeSitterFixture("C++", ".cpp", "class Sample {};\n"),
    TreeSitterFixture("Ruby", ".rb", "def sample\nend\n"),
    TreeSitterFixture("C#", ".cs", "class Sample {}\n"),
    TreeSitterFixture("Kotlin", ".kt", "class Sample\n"),
    TreeSitterFixture("Scala", ".scala", "class Sample\n"),
    TreeSitterFixture("PHP", ".php", "<?php function sample() {}\n"),
    TreeSitterFixture("Swift", ".swift", "class Sample {}\n"),
    TreeSitterFixture("Lua", ".lua", "local function sample() end\n"),
    TreeSitterFixture("Zig", ".zig", "fn sample() void {}\n"),
    TreeSitterFixture("Dart", ".dart", "class Sample {}\n"),
    TreeSitterFixture("Elixir", ".ex", "defmodule Sample do\nend\n"),
    TreeSitterFixture("Julia", ".jl", "function sample()\nend\n"),
    TreeSitterFixture("Bash", ".sh", "sample() {\n  echo ok\n}\n"),
    TreeSitterFixture("SQL", ".sql", "CREATE TABLE sample (id INTEGER);\n"),
)


@pytest.mark.parametrize(
    "fixture",
    TREE_SITTER_FIXTURES,
    ids=lambda fixture: (
        f"{fixture.grammar_language or fixture.language}{fixture.extension}"
    ),
)
def test_installed_treesitter_grammar_extracts_symbol(
    fixture: TreeSitterFixture,
    tmp_path,
) -> None:
    grammar_language = fixture.grammar_language or fixture.language
    grammar_result = treesitter.load_grammar(grammar_language)
    if not grammar_result.available:
        pytest.skip(grammar_result.missing_reason)

    source_path = tmp_path / f"sample{fixture.extension}"
    source_path.write_text(fixture.source, encoding="utf-8")

    extraction = TreeSitterExtractor(max_depth=4).extract(
        DiscoveredFile(
            repository_id="repo",
            path=f"src/sample{fixture.extension}",
            absolute_path=str(source_path),
            kind="source",
            extension=fixture.extension,
            language=fixture.language,
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
        ),
    )

    assert extraction.symbols


def test_missing_treesitter_grammar_returns_empty_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source_path = tmp_path / "sample.py"
    source_path.write_text("class Sample:\n    pass\n", encoding="utf-8")

    def import_module(name: str):
        if name == "tree_sitter":
            return SimpleNamespace()
        if name == "tree_sitter_python":
            raise ImportError("missing test grammar")
        return __import__(name)

    monkeypatch.setattr(treesitter.importlib, "import_module", import_module)

    extraction = TreeSitterExtractor().extract(
        DiscoveredFile(
            repository_id="repo",
            path="src/sample.py",
            absolute_path=str(source_path),
            kind="source",
            extension=".py",
            language="Python",
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
        ),
    )

    assert extraction == SymbolExtraction()
