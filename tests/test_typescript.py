from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace

import pytest

from code_scout.core.errors import ExtractionError
from code_scout.discovery import DiscoveredFile
from code_scout.extractors.typescript import (
    TREE_SITTER_SETUP_HINT,
    TREE_SITTER_SETUP_MESSAGE,
    parse_typescript_file,
)
from code_scout.utils.language_map import language_for_extension


def test_parse_typescript_file_returns_normalized_tree_sitter_result(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "export function greet(name: string) {\n  return `hi ${name}`;\n}\n"
    source_file = tmp_path / "src" / "greet.ts"
    source_file.parent.mkdir()
    source_file.write_text(source, encoding="utf-8")
    monkeypatch.setattr(
        "code_scout.extractors.typescript._tree_sitter_modules",
        lambda: _fake_tree_sitter_modules("function_declaration", "function greet"),
    )

    result = parse_typescript_file(
        _discovered_file(source_file, relative_path="src/greet.ts")
    )

    assert result is not None
    assert result.repository_id == "workspace"
    assert result.path == "src/greet.ts"
    assert result.language == "typescript"
    assert result.has_errors is False
    assert result.root.type == "program"
    assert result.root.start_line == 1
    assert result.root.children[0].type == "function_declaration"
    assert result.root.children[0].text == "function greet"
    assert result.root.children[0].start_line == 1
    assert result.root.children[0].start_column == 8


def test_parse_tsx_file_uses_tsx_grammar_and_returns_jsx_nodes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "export function Card() {\n  return <section>Hi</section>;\n}\n"
    source_file = tmp_path / "Card.tsx"
    source_file.write_text(source, encoding="utf-8")
    fake_modules = _fake_tree_sitter_modules("jsx_element", "<section>Hi</section>")
    monkeypatch.setattr(
        "code_scout.extractors.typescript._tree_sitter_modules", lambda: fake_modules
    )

    result = parse_typescript_file(_discovered_file(source_file))

    assert result is not None
    assert result.language == "tsx"
    assert result.root.children[0].type == "jsx_element"
    assert result.root.children[0].text == "<section>Hi</section>"
    assert fake_modules[0].Parser.languages == ["tsx-language"]


def test_parse_typescript_file_ignores_non_typescript_files(tmp_path) -> None:
    source_file = tmp_path / "app.js"
    source_file.write_text("export const value = 1;\n", encoding="utf-8")

    assert parse_typescript_file(_discovered_file(source_file)) is None


def test_parse_typescript_file_reports_setup_message_when_parser_is_unavailable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_file = tmp_path / "app.ts"
    source_file.write_text("export const value = 1;\n", encoding="utf-8")

    def fail_import(_module_name: str):
        raise ImportError("missing parser")

    monkeypatch.setattr(
        "code_scout.extractors.typescript.importlib.import_module", fail_import
    )

    with pytest.raises(ExtractionError) as error:
        parse_typescript_file(_discovered_file(source_file))

    assert error.value.message == TREE_SITTER_SETUP_MESSAGE
    assert error.value.hint == TREE_SITTER_SETUP_HINT


def _fake_tree_sitter_modules(node_type: str, node_text: str):
    class FakeNode:
        def __init__(
            self,
            *,
            type: str,
            source: bytes,
            text: str,
            children: tuple[FakeNode, ...] = (),
        ) -> None:
            self.type = type
            self.is_named = True
            self.children = children
            self.start_byte = source.index(text.encode("utf-8"))
            self.end_byte = self.start_byte + len(text.encode("utf-8"))
            self.start_point = _point_for_offset(source, self.start_byte)
            self.end_point = _point_for_offset(source, self.end_byte)
            self.has_error = False

    class FakeTree:
        def __init__(self, root_node: FakeNode) -> None:
            self.root_node = root_node

    class FakeParser:
        languages: list[str] = []

        def __init__(self, language) -> None:
            self.languages.append(language)

        def parse(self, source: bytes) -> FakeTree:
            child = FakeNode(type=node_type, source=source, text=node_text)
            root = FakeNode(
                type="program",
                source=source,
                text=source.decode("utf-8"),
                children=(child,),
            )
            return FakeTree(root)

    tree_sitter = SimpleNamespace(Parser=FakeParser)
    tree_sitter_typescript = SimpleNamespace(
        language_typescript=lambda: "typescript-language",
        language_tsx=lambda: "tsx-language",
    )
    return tree_sitter, tree_sitter_typescript


def _point_for_offset(source: bytes, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    return prefix.count(b"\n"), len(prefix.rsplit(b"\n", 1)[-1])


def _discovered_file(
    file_path,
    *,
    repository_id: str = "workspace",
    relative_path: str | None = None,
) -> DiscoveredFile:
    content = file_path.read_bytes()
    return DiscoveredFile(
        repository_id=repository_id,
        path=relative_path if relative_path is not None else file_path.name,
        absolute_path=str(file_path),
        kind="source",
        extension=file_path.suffix,
        content_hash=sha256(content).hexdigest(),
        modified_at="2026-06-20T10:00:00Z",
        language=language_for_extension(file_path.suffix),
    )
