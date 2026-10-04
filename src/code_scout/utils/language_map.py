from __future__ import annotations

from typing import Literal

ExtractorTier = Literal["tree_sitter", "heuristic", "dotnet", "byond", "none"]

_EXTENSION_MAP: dict[str, tuple[str, ExtractorTier]] = {
    ".astro": ("Astro", "heuristic"),
    ".bash": ("Bash", "tree_sitter"),
    ".c": ("C", "tree_sitter"),
    ".cjs": ("JavaScript", "tree_sitter"),
    ".cpp": ("C++", "tree_sitter"),
    ".cs": ("C#", "tree_sitter"),
    ".cshtml": ("Razor", "dotnet"),
    ".csproj": ("MSBuild", "dotnet"),
    ".cts": ("TypeScript", "tree_sitter"),
    ".dart": ("Dart", "tree_sitter"),
    ".dfm": ("Delphi Form", "none"),
    ".dm": ("DM", "byond"),
    ".dme": ("DM", "byond"),
    ".dmf": ("BYOND Interface", "none"),
    ".dmi": ("BYOND Image", "none"),
    ".dmm": ("BYOND Map", "none"),
    ".dpk": ("Delphi", "none"),
    ".dpr": ("Delphi", "heuristic"),
    ".ex": ("Elixir", "tree_sitter"),
    ".exs": ("Elixir", "tree_sitter"),
    ".f": ("Fortran", "heuristic"),
    ".f03": ("Fortran", "heuristic"),
    ".f08": ("Fortran", "heuristic"),
    ".f90": ("Fortran", "heuristic"),
    ".f95": ("Fortran", "heuristic"),
    ".fsproj": ("MSBuild", "dotnet"),
    ".go": ("Go", "tree_sitter"),
    ".gradle": ("Groovy", "none"),
    ".groovy": ("Groovy", "heuristic"),
    ".h": ("C", "tree_sitter"),
    ".hpp": ("C++", "tree_sitter"),
    ".inc": ("Pascal", "heuristic"),
    ".java": ("Java", "tree_sitter"),
    ".jl": ("Julia", "tree_sitter"),
    ".js": ("JavaScript", "tree_sitter"),
    ".json": ("JSON", "none"),
    ".jsx": ("JavaScript", "tree_sitter"),
    ".kt": ("Kotlin", "tree_sitter"),
    ".lfm": ("Lazarus Form", "none"),
    ".lpk": ("Lazarus Pkg", "none"),
    ".lpr": ("Pascal", "heuristic"),
    ".lua": ("Lua", "tree_sitter"),
    ".luau": ("Luau", "heuristic"),
    ".m": ("Objective-C", "heuristic"),
    ".md": ("Markdown", "none"),
    ".mdx": ("MDX", "none"),
    ".mjs": ("JavaScript", "tree_sitter"),
    ".mm": ("Objective-C", "heuristic"),
    ".mts": ("TypeScript", "tree_sitter"),
    ".pas": ("Pascal", "heuristic"),
    ".php": ("PHP", "tree_sitter"),
    ".pp": ("Pascal", "heuristic"),
    ".ps1": ("PowerShell", "heuristic"),
    ".psm1": ("PowerShell", "heuristic"),
    ".py": ("Python", "tree_sitter"),
    ".razor": ("Razor", "dotnet"),
    ".rb": ("Ruby", "tree_sitter"),
    ".rs": ("Rust", "tree_sitter"),
    ".scala": ("Scala", "tree_sitter"),
    ".sh": ("Bash", "tree_sitter"),
    ".sln": ("Solution", "dotnet"),
    ".slnx": ("Solution", "dotnet"),
    ".sql": ("SQL", "tree_sitter"),
    ".sv": ("SystemVerilog", "heuristic"),
    ".svelte": ("Svelte", "heuristic"),
    ".svh": ("SystemVerilog", "heuristic"),
    ".swift": ("Swift", "tree_sitter"),
    ".ts": ("TypeScript", "tree_sitter"),
    ".tsx": ("TypeScript", "tree_sitter"),
    ".v": ("Verilog", "heuristic"),
    ".vbproj": ("MSBuild", "dotnet"),
    ".vue": ("Vue", "heuristic"),
    ".yaml": ("YAML", "none"),
    ".yml": ("YAML", "none"),
    ".zig": ("Zig", "tree_sitter"),
}


def language_for_extension(ext: str) -> str | None:
    entry = _EXTENSION_MAP.get(_normalize_extension(ext))
    return entry[0] if entry is not None else None


def tier_for_extension(ext: str) -> ExtractorTier:
    entry = _EXTENSION_MAP.get(_normalize_extension(ext))
    return entry[1] if entry is not None else "none"


def _normalize_extension(ext: str) -> str:
    normalized = ext.lower()
    if normalized and not normalized.startswith("."):
        return f".{normalized}"
    return normalized
