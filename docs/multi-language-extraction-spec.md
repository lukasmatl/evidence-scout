# Multi-Language Extraction — Implementation Spec

## Goal

Extend code-scout to discover, classify, and extract symbols from the full list of
target extensions below. Results land in the SQLite graph (`files`,
`symbols`, `edges`) and surface in `fastcontext.json`.

---

## Target Extensions

### Programming languages (kind: `source`)

| Group | Extensions                               |
|---|------------------------------------------|
| Python | `.py`                                    |
| JS / TS | `.js` `.jsx` `.mjs` `.ts` `.tsx`         |
| Go | `.go`                                    |
| Rust | `.rs`                                    |
| Java | `.java`                                  |
| C / C++ | `.c` `.cpp` `.h` `.hpp`                  |
| Ruby | `.rb`                                    |
| C# | `.cs`                                    |
| Kotlin | `.kt`                                    |
| Scala | `.scala`                                 |
| PHP | `.php`                                   |
| Swift | `.swift`                                 |
| Lua | `.lua` `.luau`                           |
| Zig | `.zig`                                   |
| PowerShell | `.ps1` `.psm1`                           |
| Elixir | `.ex` `.exs`                             |
| Objective-C | `.m` `.mm`                               |
| Julia | `.jl`                                    |
| Groovy / Gradle (script) | `.groovy` `.gradle`                      |
| Dart | `.dart`                                  |
| Verilog / SystemVerilog | `.v` `.sv` `.svh`                        |
| SQL | `.sql`                                   |
| Fortran | `.f` `.f90` `.f95` `.f03` `.f08`         |
| Pascal / Delphi | `.pas` `.pp` `.dpr` `.dpk` `.lpr` `.inc` |
| Delphi forms / packages | `.dfm` `.lfm` `.lpk`                     |
| Shell | `.sh` `.bash`, `.ps1`                    |
| BYOND / DM | `.dm` `.dme`                             |

### Frontend templates (kind: `source`)

| Group | Extensions |
|---|---|
| Vue SFC | `.vue` |
| Svelte | `.svelte` |
| Astro | `.astro` |
| Razor / Blazor | `.razor` `.cshtml` |

### .NET project files (kind: `metadata`)

| Group | Extensions |
|---|---|
| Solution | `.sln` `.slnx` |
| Project | `.csproj` `.fsproj` `.vbproj` |

### BYOND map / image (kind: `metadata`)

`.dmi` `.dmm` `.dmf`

### Already supported (no change needed)

`.json`, `.yaml`, `.yml`, `.md`, `.mdx`, `.ts`, `.tsx`, `.js`, `.jsx`,
`.mjs`

---

## Architecture

### Current problems

1. `INCLUDED_SUFFIXES` in `discovery.py` is the only gatekeeper — unlisted
   extensions are never discovered and never reach any extractor.
2. `DiscoveredFile` and `File` have no `language` field populated — the DB
   column exists but is always `null`.
3. The two tree-sitter extractors (`nestjs.py`, `nextjs.py`) are
   framework-specific and not reusable across languages.
4. There is no extractor registry — adding a language requires modifying
   `scan.py` directly.

### Target architecture

```
src/code_scout/
  discovery.py          # extend INCLUDED_SUFFIXES + add language detection
  language_map.py       # NEW: extension → language name + extractor tier
  extractors/
    __init__.py         # ExtractorRegistry + SymbolExtraction result type
    treesitter.py       # generic tree-sitter symbol extractor
    heuristic.py        # regex fallback for unsupported grammars
    dotnet.py           # .sln / .csproj / .razor specific parsing
    byond.py            # .dm / .dme specific parsing
  nestjs.py             # unchanged (framework layer on top of treesitter.py)
  nextjs.py             # unchanged (framework layer on top of treesitter.py)
  scan.py               # wire new extractors via registry
```

---

## Phase 1 — Discovery layer

### 1a. Extend `INCLUDED_SUFFIXES` in `discovery.py`

Replace the current `frozenset` with the full set covering all target
extensions listed above.

```python
INCLUDED_SUFFIXES = frozenset({
    # already present
    ".cjs", ".cts", ".js", ".jsx", ".json",
    ".md", ".mdx", ".mjs", ".mts", ".ts", ".tsx",
    ".yaml", ".yml",
    # programming languages
    ".py", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp",
    ".rb", ".cs", ".kt", ".scala", ".php", ".swift",
    ".lua", ".luau", ".zig",
    ".ps1", ".psm1",
    ".ex", ".exs",
    ".m", ".mm",
    ".jl",
    ".groovy", ".gradle",
    ".dart",
    ".v", ".sv", ".svh",
    ".sql",
    ".f", ".f90", ".f95", ".f03", ".f08",
    ".pas", ".pp", ".dpr", ".dpk", ".lpr", ".inc",
    ".dfm", ".lfm", ".lpk",
    ".sh", ".bash",
    ".dm", ".dme", ".dmi", ".dmm", ".dmf",
    # frontend templates
    ".vue", ".svelte", ".astro", ".razor", ".cshtml",
    # .NET project files
    ".sln", ".slnx", ".csproj", ".fsproj", ".vbproj",
})
```

### 1b. Update `_file_kind()` in `discovery.py`

Add branches before the final `return "source"`:

```python
_METADATA_EXTENSIONS = frozenset({
    ".json", ".yaml", ".yml",
    ".sln", ".slnx", ".csproj", ".fsproj", ".vbproj",
    ".dmi", ".dmm", ".dmf",       # BYOND binary/map assets
    ".dfm", ".lfm", ".lpk",       # Delphi designer/package files
    ".gradle",                     # Gradle build scripts (treat as metadata)
})
```

Return `"metadata"` for anything in `_METADATA_EXTENSIONS` and `"source"`
for everything else (including `.razor`, `.cshtml`, `.vue`, `.svelte`,
`.astro`, `.dm`, `.dme`, `.sh`, `.bash`, `.ps1`, `.sql`, etc.).

### 1c. Add `language` to `DiscoveredFile`

In `discovery.py`, add field:

```python
@dataclass(frozen=True)
class DiscoveredFile:
    ...
    language: str | None = None
```

Populate it in `discover_workspace_files()` by calling
`language_map.language_for_extension(path.suffix.lower())`.

### 1d. Propagate language in `scan.py`

In `_graph_file()`:

```python
def _graph_file(discovered_file: DiscoveredFile) -> File:
    return File(
        ...
        language=discovered_file.language,   # add this
    )
```

---

## Phase 2 — `language_map.py` (new file)

Single source of truth mapping extension → language name and extractor tier.

```python
# language_map.py

from __future__ import annotations

# Extractor tier controls which extractor handles the file:
#   "tree_sitter"  — a tree-sitter grammar exists and is wired up
#   "heuristic"    — regex-based fallback
#   "dotnet"       — .NET project/solution/template parser
#   "byond"        — BYOND DM source parser
#   "none"         — discovered + classified, no symbol extraction
ExtractorTier = Literal["tree_sitter", "heuristic", "dotnet", "byond", "none"]

_EXTENSION_MAP: dict[str, tuple[str, ExtractorTier]] = {
    # (language_name, extractor_tier)
    ".py":    ("Python",      "tree_sitter"),
    ".js":    ("JavaScript",  "tree_sitter"),
    ".jsx":   ("JavaScript",  "tree_sitter"),
    ".mjs":   ("JavaScript",  "tree_sitter"),
    ".cjs":   ("JavaScript",  "tree_sitter"),
    ".ts":    ("TypeScript",  "tree_sitter"),
    ".tsx":   ("TypeScript",  "tree_sitter"),
    ".mts":   ("TypeScript",  "tree_sitter"),
    ".cts":   ("TypeScript",  "tree_sitter"),
    ".go":    ("Go",          "tree_sitter"),
    ".rs":    ("Rust",        "tree_sitter"),
    ".java":  ("Java",        "tree_sitter"),
    ".c":     ("C",           "tree_sitter"),
    ".h":     ("C",           "tree_sitter"),
    ".cpp":   ("C++",         "tree_sitter"),
    ".hpp":   ("C++",         "tree_sitter"),
    ".rb":    ("Ruby",        "tree_sitter"),
    ".cs":    ("C#",          "tree_sitter"),
    ".kt":    ("Kotlin",      "tree_sitter"),
    ".scala": ("Scala",       "tree_sitter"),
    ".php":   ("PHP",         "tree_sitter"),
    ".swift": ("Swift",       "tree_sitter"),
    ".lua":   ("Lua",         "tree_sitter"),
    ".luau":  ("Luau",        "heuristic"),
    ".zig":   ("Zig",         "tree_sitter"),
    ".dart":  ("Dart",        "tree_sitter"),
    ".ex":    ("Elixir",      "tree_sitter"),
    ".exs":   ("Elixir",      "tree_sitter"),
    ".jl":    ("Julia",       "tree_sitter"),
    ".sh":    ("Bash",        "tree_sitter"),
    ".bash":  ("Bash",        "tree_sitter"),
    ".sql":   ("SQL",         "tree_sitter"),
    ".groovy":("Groovy",      "heuristic"),
    ".gradle":("Groovy",      "none"),           # build metadata, skip symbols
    ".ps1":   ("PowerShell",  "heuristic"),
    ".psm1":  ("PowerShell",  "heuristic"),
    ".m":     ("Objective-C", "heuristic"),
    ".mm":    ("Objective-C", "heuristic"),
    ".vue":   ("Vue",         "heuristic"),
    ".svelte":("Svelte",      "heuristic"),
    ".astro": ("Astro",       "heuristic"),
    ".f":     ("Fortran",     "heuristic"),
    ".f90":   ("Fortran",     "heuristic"),
    ".f95":   ("Fortran",     "heuristic"),
    ".f03":   ("Fortran",     "heuristic"),
    ".f08":   ("Fortran",     "heuristic"),
    ".pas":   ("Pascal",      "heuristic"),
    ".pp":    ("Pascal",      "heuristic"),
    ".dpr":   ("Delphi",      "heuristic"),
    ".dpk":   ("Delphi",      "none"),
    ".lpr":   ("Pascal",      "heuristic"),
    ".inc":   ("Pascal",      "heuristic"),
    ".v":     ("Verilog",     "heuristic"),
    ".sv":    ("SystemVerilog","heuristic"),
    ".svh":   ("SystemVerilog","heuristic"),
    ".dm":    ("DM",          "byond"),
    ".dme":   ("DM",          "byond"),
    # .NET project/template files
    ".cs":    ("C#",          "tree_sitter"),
    ".razor": ("Razor",       "dotnet"),
    ".cshtml":("Razor",       "dotnet"),
    ".csproj":("MSBuild",     "dotnet"),
    ".fsproj":("MSBuild",     "dotnet"),
    ".vbproj":("MSBuild",     "dotnet"),
    ".sln":   ("Solution",    "dotnet"),
    ".slnx":  ("Solution",    "dotnet"),
    # assets / metadata — no symbol extraction
    ".dmi":   ("BYOND Image", "none"),
    ".dmm":   ("BYOND Map",   "none"),
    ".dmf":   ("BYOND Interface","none"),
    ".dfm":   ("Delphi Form", "none"),
    ".lfm":   ("Lazarus Form","none"),
    ".lpk":   ("Lazarus Pkg", "none"),
    ".json":  ("JSON",        "none"),
    ".yaml":  ("YAML",        "none"),
    ".yml":   ("YAML",        "none"),
}


def language_for_extension(ext: str) -> str | None:
    entry = _EXTENSION_MAP.get(ext)
    return entry[0] if entry else None


def tier_for_extension(ext: str) -> ExtractorTier:
    entry = _EXTENSION_MAP.get(ext)
    return entry[1] if entry else "none"
```

---

## Phase 3 — Extractor registry (`src/code_scout/extractors/__init__.py`)

```python
@dataclass(frozen=True)
class SymbolExtraction:
    symbols: tuple[Symbol, ...]
    edges: tuple[Edge, ...]


class SymbolExtractor(Protocol):
    def extract(
        self,
        discovered_file: DiscoveredFile,
    ) -> SymbolExtraction: ...


class ExtractorRegistry:
    """Routes each DiscoveredFile to the right extractor by tier."""

    def __init__(
        self,
        *,
        tree_sitter_enabled: bool = True,
        heuristic_enabled: bool = True,
        dotnet_enabled: bool = True,
        byond_enabled: bool = True,
    ) -> None: ...

    def extract(
        self,
        discovered_files: tuple[DiscoveredFile, ...],
    ) -> SymbolExtraction: ...
```

The registry calls `language_map.tier_for_extension(file.extension)` and
dispatches to the appropriate extractor. Failures per file are caught and
logged as warnings — one bad file must not abort the scan.

---

## Phase 4 — Generic tree-sitter extractor (`src/code_scout/extractors/treesitter.py`)

Replace the current per-framework approach with one generic extractor that:

1. Resolves the tree-sitter grammar package for a language name.
2. Parses the file bytes.
3. Walks the AST looking for **declaration nodes** using a per-language
   node-type map.
4. Returns `Symbol` records for each declaration found.

### Grammar package map

| Language | PyPI package | Grammar function |
|---|---|---|
| Python | `tree-sitter-python` | `language()` |
| JavaScript | `tree-sitter-javascript` | `language()` |
| TypeScript | `tree-sitter-typescript` | `language_typescript()` |
| TSX | `tree-sitter-typescript` | `language_tsx()` |
| Go | `tree-sitter-go` | `language()` |
| Rust | `tree-sitter-rust` | `language()` |
| Java | `tree-sitter-java` | `language()` |
| C | `tree-sitter-c` | `language()` |
| C++ | `tree-sitter-cpp` | `language()` |
| Ruby | `tree-sitter-ruby` | `language()` |
| C# | `tree-sitter-c-sharp` | `language()` |
| Kotlin | `tree-sitter-kotlin` | `language()` |
| Scala | `tree-sitter-scala` | `language()` |
| PHP | `tree-sitter-php` | `language_php()` |
| Swift | `tree-sitter-swift` | `language()` |
| Lua | `tree-sitter-lua` | `language()` |
| Zig | `tree-sitter-zig` | `language()` |
| Dart | `tree-sitter-dart` | `language()` |
| Elixir | `tree-sitter-elixir` | `language()` |
| Julia | `tree-sitter-julia` | `language()` |
| Bash | `tree-sitter-bash` | `language()` |
| SQL | `tree-sitter-sql` | `language()` |

All packages are **optional extras** — if a grammar is missing, the extractor
falls through to heuristic for that file. Never raise at import time.

### Declaration node map (per language)

Each entry: `tree_sitter_node_type → code_scout_symbol_kind`

```python
_DECLARATION_NODES: dict[str, dict[str, str]] = {
    "Python": {
        "class_definition": "class",
        "function_definition": "function",
        "decorated_definition": "function",   # walk into body
        "async_function_definition": "function",
    },
    "Go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",           # walk into spec to get struct/interface
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
        "assignment_statement": "variable",   # filter to function-valued only
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
        "lexical_declaration": "variable",    # filter to function-valued only
    },
}
```

The extractor walks only the **top two levels** of the AST (file root + class
body) to avoid pulling internal helper functions into the global symbol space.
Depth is configurable per language.

### Name extraction

For each matched declaration node, look for the first child node of type
`identifier`, `name`, or `type_identifier` to get the symbol name. If none is
found, skip the node (do not emit an anonymous symbol).

### Symbol ID format (consistent with existing extractors)

```
symbol:{repository_id}:{file_path}:{language_slug}:{qualified_name_slug}
```

---

## Phase 5 — Heuristic extractor (`src/code_scout/extractors/heuristic.py`)

For languages without a stable tree-sitter Python binding, use regex patterns
to extract top-level declarations. Lower confidence (0.6).

### Pattern table

| Language | Pattern | Capture group |
|---|---|---|
| PowerShell | `^function\s+([\w-]+)` | name |
| PowerShell | `^class\s+(\w+)` | name |
| Objective-C | `^[-+]\s*\([^)]+\)\s*(\w+)` | method selector |
| Objective-C | `^@interface\s+(\w+)` | class name |
| Objective-C | `^@implementation\s+(\w+)` | class name |
| Groovy | `^(class\|interface\|enum\|def)\s+(\w+)` | kind + name |
| Fortran | `^\s*(subroutine\|function\|module\|program)\s+(\w+)` | kind + name |
| Pascal | `^(procedure\|function\|type\|unit\|program)\s+(\w+)` | kind + name |
| Verilog | `^module\s+(\w+)` | name |
| SystemVerilog | `^(module\|interface\|class\|package)\s+(\w+)` | kind + name |
| Vue | `export default` → kind `component`, name from filename | — |
| Svelte | filename → kind `component` | — |
| Astro | filename → kind `component` | — |
| Luau | same patterns as Lua | — |
| DM (BYOND) | `^\/[\w\/]+` path-style object declaration | path |

All patterns are compiled once at module level. Emit confidence 0.6 for
heuristic symbols.

---

## Phase 6 — .NET extractor (`src/code_scout/extractors/dotnet.py`)

### `.sln` / `.slnx`

Parse project references and emit `Edge(relation_type="references_project")`
edges between the solution file and each `.csproj`/`.fsproj`/`.vbproj` path.

`.sln` format: lines matching `Project\(.*\) = ".*", "(.*\..*proj)"` (regex).
`.slnx` format: XML, `<Project Path="...">`.

### `.csproj` / `.fsproj` / `.vbproj`

Parse as XML. Extract:

- `<AssemblyName>` → Symbol(kind="assembly")
- `<RootNamespace>` → Symbol(kind="namespace")
- `<PackageReference Include="..." Version="...">` →
  Edge(relation_type="depends_on", target_id=package name)
- `<ProjectReference Include="...">` →
  Edge(relation_type="references_project")

### `.razor` / `.cshtml`

Regex scan for:

- `@model (\w+)` → Symbol(kind="model")
- `@inject (\S+) (\w+)` → Edge(relation_type="injects", target=service type)
- `@page "([^"]+)"` → Contract(kind="razor_route", path=value)

---

## Phase 7 — BYOND extractor (`src/code_scout/extractors/byond.py`)

DM files use a path-based object hierarchy:

```dm
/mob/player
    var/health = 100
    proc/move()
```

Extract:

- Lines starting with `/` → Symbol(kind based on first path segment:
  `/mob` → "mob", `/obj` → "object", `/area` → "area",
  `/turf` → "turf", `/proc` → "procedure", `/datum` → "datum")
- `proc/` declarations → Symbol(kind="procedure")

Qualified name = the full DM path string.

---

## Phase 8 — Wire into `scan.py`

### New extractor name: `symbol_extraction`

Add `symbol_extraction` to `_EXTRACTOR_NAMES` in `config.py` and
`ExtractorConfig`. It controls whether the new registry runs.

In `scan_workspace()`:

```python
symbol_extraction_result = (
    ExtractorRegistry(
        tree_sitter_enabled="tree_sitter" in selected_extractors,
        heuristic_enabled=True,
        dotnet_enabled=True,
        byond_enabled=True,
    ).extract(discovered_files)
    if "symbol_extraction" in selected_extractors
    else SymbolExtraction(symbols=(), edges=())
)
```

Merge into `normalize_graph_facts`:

```python
symbols=(
    *nestjs_symbol_nodes,
    *nextjs_symbol_nodes,
    *symbol_extraction_result.symbols,
    *(graphify_extraction.symbols if graphify_extraction is not None else ()),
    *(lsp_extraction.symbols if lsp_extraction is not None else ()),
),
edges=(
    *symbol_extraction_result.edges,
    ...existing edges...
),
```

---

## Phase 9 — Repository language hints

After discovery, infer `language_hints` for each repository from the most
common `language` values across its discovered files:

```python
def _infer_language_hints(
    discovered_files: tuple[DiscoveredFile, ...],
    repository_id: str,
) -> tuple[str, ...]:
    counts: Counter[str] = Counter()
    for f in discovered_files:
        if f.repository_id == repository_id and f.language:
            counts[f.language] += 1
    # return languages that make up ≥5% of files, capped at top 5
    total = sum(counts.values()) or 1
    return tuple(
        lang for lang, n in counts.most_common(5)
        if n / total >= 0.05
    )
```

Merge with existing `repository.language_hints` in `scan_workspace()`.

---

## Testing requirements

### Unit tests (new)

- `test_language_map.py` — assert `language_for_extension` and
  `tier_for_extension` return correct values for every extension in the target
  list. Assert unknown extension returns `(None, "none")`.

- `test_discovery_language.py` — assert `DiscoveredFile.language` is populated
  for `.cs`, `.py`, `.go`, `.rs` files; assert `None` for `.json`.

- `test_extractors_treesitter.py` — parametrize over language fixtures (one
  minimal source file per language) and assert at least one symbol is extracted.
  Gracefully skip if the grammar package is not installed.

- `test_extractors_heuristic.py` — same pattern for heuristic languages.
  No optional dependencies — these must always pass.

- `test_extractors_dotnet.py` — parse a minimal `.csproj`, `.sln`, `.razor`
  fixture and assert the expected symbols/edges are returned.

- `test_extractors_byond.py` — parse a minimal `.dm` fixture and assert
  `/mob/player` yields a symbol.

### Integration test

- `test_scan_multilang.py` — create a temp workspace with one source file per
  tier, run `scan_workspace()`, open the DB, assert `files.language` is
  populated and `symbols` count > 0.

---

## Optional dependencies

Add to `pyproject.toml` as optional extras:

```toml
[project.optional-dependencies]
tree-sitter-all = [
    "tree-sitter>=0.21",
    "tree-sitter-python",
    "tree-sitter-javascript",
    "tree-sitter-typescript",
    "tree-sitter-go",
    "tree-sitter-rust",
    "tree-sitter-java",
    "tree-sitter-c",
    "tree-sitter-cpp",
    "tree-sitter-ruby",
    "tree-sitter-c-sharp",
    "tree-sitter-kotlin",
    "tree-sitter-scala",
    "tree-sitter-php",
    "tree-sitter-swift",
    "tree-sitter-lua",
    "tree-sitter-zig",
    "tree-sitter-dart",
    "tree-sitter-elixir",
    "tree-sitter-julia",
    "tree-sitter-bash",
    "tree-sitter-sql",
]
```

At runtime each grammar is imported with `importlib.import_module()` inside
a try/except. A missing grammar degrades gracefully to heuristic for that
language — it is never a hard error unless `tree_sitter` itself is absent and
the `tree_sitter` extractor is the only enabled one.

---

---

## Execution order for a coding agent

1. `language_map.py` — create, no dependencies on other new code.
2. `discovery.py` — extend `INCLUDED_SUFFIXES`, add `language` field to
   `DiscoveredFile`, call `language_map.language_for_extension`, update
   `_file_kind`.
3. `graph.py` — no changes needed (`File.language` already exists).
4. `scan.py` — propagate `discovered_file.language` in `_graph_file()`.
5. `src/code_scout/extractors/__init__.py` — define `SymbolExtraction` and
   `ExtractorRegistry` skeleton.
6. `src/code_scout/extractors/treesitter.py` — generic tree-sitter extractor with
   declaration node map.
7. `src/code_scout/extractors/heuristic.py` — regex extractor.
8. `src/code_scout/extractors/dotnet.py` — .NET parser.
9. `src/code_scout/extractors/byond.py` — BYOND DM parser.
10. `config.py` — add `symbol_extraction` extractor name and config field.
11. `scan.py` — wire `ExtractorRegistry` into `scan_workspace()`.
12. Tests — in the order listed above.

Steps 1–4 can ship independently and immediately improve language-populated
graph files and broader file coverage. Steps 5–11 add actual symbol extraction.
