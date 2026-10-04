from __future__ import annotations

from dataclasses import dataclass

import pytest

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.extractors.heuristic import HEURISTIC_CONFIDENCE, HeuristicExtractor


@dataclass(frozen=True)
class HeuristicFixture:
    path: str
    language: str
    extension: str
    source: str
    expected_name: str
    expected_kind: str


@pytest.mark.parametrize(
    "fixture",
    (
        HeuristicFixture(
            path="scripts/users.ps1",
            language="PowerShell",
            extension=".ps1",
            source="function Get-User {\n}\n",
            expected_name="Get-User",
            expected_kind="function",
        ),
        HeuristicFixture(
            path="Sources/UserView.m",
            language="Objective-C",
            extension=".m",
            source="@interface UserView\n@end\n",
            expected_name="UserView",
            expected_kind="class",
        ),
        HeuristicFixture(
            path="src/UserJob.groovy",
            language="Groovy",
            extension=".groovy",
            source="class UserJob {}\n",
            expected_name="UserJob",
            expected_kind="class",
        ),
        HeuristicFixture(
            path="src/solver.f90",
            language="Fortran",
            extension=".f90",
            source="subroutine solve_users()\nend subroutine\n",
            expected_name="solve_users",
            expected_kind="subroutine",
        ),
        HeuristicFixture(
            path="src/UserUnit.pas",
            language="Pascal",
            extension=".pas",
            source="unit UserUnit;\ninterface\nimplementation\nend.\n",
            expected_name="UserUnit",
            expected_kind="unit",
        ),
        HeuristicFixture(
            path="rtl/user_counter.v",
            language="Verilog",
            extension=".v",
            source="module user_counter(input clk);\nendmodule\n",
            expected_name="user_counter",
            expected_kind="module",
        ),
        HeuristicFixture(
            path="rtl/user_bus.sv",
            language="SystemVerilog",
            extension=".sv",
            source="interface user_bus;\nendinterface\n",
            expected_name="user_bus",
            expected_kind="interface",
        ),
        HeuristicFixture(
            path="src/player.luau",
            language="Luau",
            extension=".luau",
            source="local function loadPlayer()\nend\n",
            expected_name="loadPlayer",
            expected_kind="function",
        ),
        HeuristicFixture(
            path="code/mob.dm",
            language="DM",
            extension=".dm",
            source='/mob/living/carbon\n    name = "carbon mob"\n',
            expected_name="/mob/living/carbon",
            expected_kind="object",
        ),
    ),
)
def test_heuristic_extracts_minimal_language_group_symbols(
    tmp_path,
    fixture: HeuristicFixture,
) -> None:
    extraction = _extract(tmp_path, fixture)

    assert tuple(symbol.name for symbol in extraction.symbols) == (
        fixture.expected_name,
    )
    assert tuple(symbol.kind for symbol in extraction.symbols) == (
        fixture.expected_kind,
    )
    assert tuple(symbol.qualified_name for symbol in extraction.symbols) == (
        fixture.expected_name,
    )
    _assert_confidence(extraction)


@pytest.mark.parametrize(
    ("path", "language", "extension", "source", "expected_name"),
    (
        (
            "components/user-profile.vue",
            "Vue",
            ".vue",
            "<script>\nexport default {}\n</script>\n",
            "UserProfile",
        ),
        (
            "components/settings-panel.svelte",
            "Svelte",
            ".svelte",
            "<script>\n  export let user;\n</script>\n",
            "SettingsPanel",
        ),
        (
            "pages/account-card.astro",
            "Astro",
            ".astro",
            "---\nconst user = Astro.props.user;\n---\n",
            "AccountCard",
        ),
    ),
)
def test_component_names_derive_from_filenames(
    tmp_path,
    path: str,
    language: str,
    extension: str,
    source: str,
    expected_name: str,
) -> None:
    extraction = _extract(
        tmp_path,
        HeuristicFixture(
            path=path,
            language=language,
            extension=extension,
            source=source,
            expected_name=expected_name,
            expected_kind="component",
        ),
    )

    assert tuple(symbol.name for symbol in extraction.symbols) == (expected_name,)
    assert tuple(symbol.kind for symbol in extraction.symbols) == ("component",)
    _assert_confidence(extraction)


def test_heuristic_returns_empty_extraction_without_matches(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        HeuristicFixture(
            path="scripts/no-symbols.ps1",
            language="PowerShell",
            extension=".ps1",
            source="Write-Host 'no declarations'\n",
            expected_name="",
            expected_kind="",
        ),
    )

    assert extraction == SymbolExtraction()


def _extract(tmp_path, fixture: HeuristicFixture) -> SymbolExtraction:
    source_path = tmp_path / fixture.path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(fixture.source, encoding="utf-8")

    return HeuristicExtractor().extract(
        DiscoveredFile(
            repository_id="repo",
            path=fixture.path,
            absolute_path=str(source_path),
            kind="source",
            extension=fixture.extension,
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
            language=fixture.language,
        ),
    )


def _assert_confidence(extraction: SymbolExtraction) -> None:
    assert len(extraction.edges) == len(extraction.symbols)
    assert tuple(edge.confidence for edge in extraction.edges) == (
        HEURISTIC_CONFIDENCE,
    ) * len(extraction.symbols)
    assert tuple(edge.evidence[0].confidence for edge in extraction.edges) == (
        HEURISTIC_CONFIDENCE,
    ) * len(extraction.symbols)
