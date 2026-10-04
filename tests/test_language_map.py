from __future__ import annotations

from collections.abc import Iterable

import pytest

from code_scout.utils.language_map import language_for_extension, tier_for_extension

ExpectedExtension = tuple[str, str, str]
ExpectedCategory = tuple[str, tuple[ExpectedExtension, ...]]

SPEC_EXTENSION_CATEGORIES: tuple[ExpectedCategory, ...] = (
    (
        "programming languages",
        (
            (".py", "Python", "tree_sitter"),
            (".js", "JavaScript", "tree_sitter"),
            (".jsx", "JavaScript", "tree_sitter"),
            (".mjs", "JavaScript", "tree_sitter"),
            (".cjs", "JavaScript", "tree_sitter"),
            (".ts", "TypeScript", "tree_sitter"),
            (".tsx", "TypeScript", "tree_sitter"),
            (".mts", "TypeScript", "tree_sitter"),
            (".cts", "TypeScript", "tree_sitter"),
            (".go", "Go", "tree_sitter"),
            (".rs", "Rust", "tree_sitter"),
            (".java", "Java", "tree_sitter"),
            (".c", "C", "tree_sitter"),
            (".h", "C", "tree_sitter"),
            (".cpp", "C++", "tree_sitter"),
            (".hpp", "C++", "tree_sitter"),
            (".rb", "Ruby", "tree_sitter"),
            (".cs", "C#", "tree_sitter"),
            (".kt", "Kotlin", "tree_sitter"),
            (".scala", "Scala", "tree_sitter"),
            (".php", "PHP", "tree_sitter"),
            (".swift", "Swift", "tree_sitter"),
            (".lua", "Lua", "tree_sitter"),
            (".luau", "Luau", "heuristic"),
            (".zig", "Zig", "tree_sitter"),
            (".ps1", "PowerShell", "heuristic"),
            (".psm1", "PowerShell", "heuristic"),
            (".ex", "Elixir", "tree_sitter"),
            (".exs", "Elixir", "tree_sitter"),
            (".m", "Objective-C", "heuristic"),
            (".mm", "Objective-C", "heuristic"),
            (".jl", "Julia", "tree_sitter"),
            (".groovy", "Groovy", "heuristic"),
            (".gradle", "Groovy", "none"),
            (".dart", "Dart", "tree_sitter"),
            (".v", "Verilog", "heuristic"),
            (".sv", "SystemVerilog", "heuristic"),
            (".svh", "SystemVerilog", "heuristic"),
            (".sql", "SQL", "tree_sitter"),
            (".f", "Fortran", "heuristic"),
            (".f90", "Fortran", "heuristic"),
            (".f95", "Fortran", "heuristic"),
            (".f03", "Fortran", "heuristic"),
            (".f08", "Fortran", "heuristic"),
            (".pas", "Pascal", "heuristic"),
            (".pp", "Pascal", "heuristic"),
            (".dpr", "Delphi", "heuristic"),
            (".dpk", "Delphi", "none"),
            (".lpr", "Pascal", "heuristic"),
            (".inc", "Pascal", "heuristic"),
            (".dfm", "Delphi Form", "none"),
            (".lfm", "Lazarus Form", "none"),
            (".lpk", "Lazarus Pkg", "none"),
            (".sh", "Bash", "tree_sitter"),
            (".bash", "Bash", "tree_sitter"),
            (".dm", "DM", "byond"),
            (".dme", "DM", "byond"),
        ),
    ),
    (
        "frontend templates",
        (
            (".vue", "Vue", "heuristic"),
            (".svelte", "Svelte", "heuristic"),
            (".astro", "Astro", "heuristic"),
            (".razor", "Razor", "dotnet"),
            (".cshtml", "Razor", "dotnet"),
        ),
    ),
    (
        ".NET project files",
        (
            (".sln", "Solution", "dotnet"),
            (".slnx", "Solution", "dotnet"),
            (".csproj", "MSBuild", "dotnet"),
            (".fsproj", "MSBuild", "dotnet"),
            (".vbproj", "MSBuild", "dotnet"),
        ),
    ),
    (
        "BYOND map / image",
        (
            (".dmi", "BYOND Image", "none"),
            (".dmm", "BYOND Map", "none"),
            (".dmf", "BYOND Interface", "none"),
        ),
    ),
    (
        "already supported",
        (
            (".json", "JSON", "none"),
            (".yaml", "YAML", "none"),
            (".yml", "YAML", "none"),
            (".md", "Markdown", "none"),
            (".mdx", "MDX", "none"),
        ),
    ),
)


def _expected_extensions() -> Iterable[ExpectedExtension]:
    for _, extensions in SPEC_EXTENSION_CATEGORIES:
        yield from extensions


@pytest.mark.parametrize(
    ("extension", "language", "tier"),
    tuple(_expected_extensions()),
)
def test_language_map_classifies_every_target_extension(
    extension: str,
    language: str,
    tier: str,
) -> None:
    assert language_for_extension(extension) == language
    assert tier_for_extension(extension) == tier


def test_language_map_expected_data_has_no_duplicate_extensions() -> None:
    extensions = [extension for extension, _, _ in _expected_extensions()]

    assert len(extensions) == len(set(extensions))


def test_language_map_normalizes_extension_case_and_dot_prefix() -> None:
    assert language_for_extension("TSX") == "TypeScript"
    assert tier_for_extension("TSX") == "tree_sitter"


def test_language_map_returns_no_language_and_no_tier_for_unknown_extension() -> None:
    assert language_for_extension(".unknown") is None
    assert tier_for_extension(".unknown") == "none"
