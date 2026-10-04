from __future__ import annotations

import re

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.extractors.heuristic import (
    HEURISTIC_CONFIDENCE,
    LANGUAGE_PATTERNS,
    HeuristicExtractor,
)


def test_heuristic_patterns_are_compiled_at_module_level() -> None:
    assert isinstance(LANGUAGE_PATTERNS["PowerShell"][0].pattern, re.Pattern)
    assert isinstance(LANGUAGE_PATTERNS["DM"][0].pattern, re.Pattern)


def test_heuristic_extracts_powershell_function(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="scripts/users.ps1",
        language="PowerShell",
        extension=".ps1",
        source="function Get-User {\n}\n",
    )

    _assert_single_symbol(extraction, name="Get-User", kind="function")


def test_heuristic_extracts_objective_c_interface_and_method(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="Sources/UserView.m",
        language="Objective-C",
        extension=".m",
        source="@interface UserView\n@end\n- (void)refresh\n",
    )

    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "UserView",
        "refresh",
    )
    assert tuple(symbol.kind for symbol in extraction.symbols) == ("class", "method")
    _assert_confidence(extraction)


def test_heuristic_extracts_groovy_class(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/UserJob.groovy",
        language="Groovy",
        extension=".groovy",
        source="class UserJob {}\n",
    )

    _assert_single_symbol(extraction, name="UserJob", kind="class")


def test_heuristic_extracts_fortran_subroutine(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/solver.f90",
        language="Fortran",
        extension=".f90",
        source="subroutine solve_users()\nend subroutine\n",
    )

    _assert_single_symbol(extraction, name="solve_users", kind="subroutine")


def test_heuristic_extracts_pascal_unit(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/UserUnit.pas",
        language="Pascal",
        extension=".pas",
        source="unit UserUnit;\ninterface\nimplementation\nend.\n",
    )

    _assert_single_symbol(extraction, name="UserUnit", kind="unit")


def test_heuristic_extracts_verilog_module(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="rtl/user_counter.v",
        language="Verilog",
        extension=".v",
        source="module user_counter(input clk);\nendmodule\n",
    )

    _assert_single_symbol(extraction, name="user_counter", kind="module")


def test_heuristic_extracts_systemverilog_interface(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="rtl/user_bus.sv",
        language="SystemVerilog",
        extension=".sv",
        source="interface user_bus;\nendinterface\n",
    )

    _assert_single_symbol(extraction, name="user_bus", kind="interface")


def test_heuristic_extracts_vue_component_from_filename(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="components/user-profile.vue",
        language="Vue",
        extension=".vue",
        source="<script>\nexport default {}\n</script>\n",
    )

    _assert_single_symbol(extraction, name="UserProfile", kind="component")


def test_heuristic_extracts_svelte_component_from_filename(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="components/SettingsPanel.svelte",
        language="Svelte",
        extension=".svelte",
        source="<script>\n  export let user;\n</script>\n",
    )

    _assert_single_symbol(extraction, name="SettingsPanel", kind="component")


def test_heuristic_extracts_astro_component_from_filename(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="pages/account-card.astro",
        language="Astro",
        extension=".astro",
        source="---\nconst user = Astro.props.user;\n---\n",
    )

    _assert_single_symbol(extraction, name="AccountCard", kind="component")


def test_heuristic_extracts_luau_function(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="src/player.luau",
        language="Luau",
        extension=".luau",
        source="local function loadPlayer()\nend\n",
    )

    _assert_single_symbol(extraction, name="loadPlayer", kind="function")


def test_heuristic_extracts_dm_compatible_path_declaration(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="code/mob.dm",
        language="DM",
        extension=".dm",
        source='/mob/living/carbon\n    name = "carbon mob"\n',
    )

    _assert_single_symbol(extraction, name="/mob/living/carbon", kind="object")
    assert extraction.symbols[0].qualified_name == "/mob/living/carbon"


def test_heuristic_returns_empty_extraction_without_matches(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="scripts/empty.ps1",
        language="PowerShell",
        extension=".ps1",
        source="Write-Host 'no declarations'\n",
    )

    assert extraction == SymbolExtraction()


def _extract(
    tmp_path,
    *,
    path: str,
    language: str,
    extension: str,
    source: str,
) -> SymbolExtraction:
    source_path = tmp_path / path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source, encoding="utf-8")

    return HeuristicExtractor().extract(
        DiscoveredFile(
            repository_id="repo",
            path=path,
            absolute_path=str(source_path),
            kind="source",
            extension=extension,
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
            language=language,
        ),
    )


def _assert_single_symbol(
    extraction: SymbolExtraction,
    *,
    name: str,
    kind: str,
) -> None:
    assert len(extraction.symbols) == 1
    assert extraction.symbols[0].name == name
    assert extraction.symbols[0].kind == kind
    assert extraction.symbols[0].qualified_name == name
    _assert_confidence(extraction)


def _assert_confidence(extraction: SymbolExtraction) -> None:
    assert tuple(edge.confidence for edge in extraction.edges) == (
        HEURISTIC_CONFIDENCE,
    ) * len(extraction.edges)
    assert tuple(edge.evidence[0].confidence for edge in extraction.edges) == (
        HEURISTIC_CONFIDENCE,
    ) * len(extraction.edges)
    assert {edge.evidence[0].extractor_name for edge in extraction.edges} == {
        "heuristic"
    }
