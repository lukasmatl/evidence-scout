from __future__ import annotations

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import SymbolExtraction
from code_scout.extractors.byond import BYOND_CONFIDENCE, ByondExtractor


def test_byond_extracts_mob_player_path(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="code/mob.dm",
        source='/mob/player\n    name = "Player"\n',
    )

    assert len(extraction.symbols) == 1
    symbol = extraction.symbols[0]
    assert symbol.name == "/mob/player"
    assert symbol.kind == "mob"
    assert symbol.qualified_name == "/mob/player"
    assert symbol.start_line == 1
    assert symbol.file_id == "file:repo:code/mob.dm"
    _assert_confidence(extraction)


def test_byond_extracts_indented_proc_declaration_as_full_path(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="code/player.dm",
        source="/mob/player\n    proc/Login()\n        return\n",
    )

    assert tuple(symbol.name for symbol in extraction.symbols) == (
        "/mob/player",
        "/mob/player/proc/Login",
    )
    assert tuple(symbol.kind for symbol in extraction.symbols) == ("mob", "procedure")
    assert tuple(symbol.qualified_name for symbol in extraction.symbols) == (
        "/mob/player",
        "/mob/player/proc/Login",
    )
    assert tuple(symbol.start_line for symbol in extraction.symbols) == (1, 2)
    _assert_confidence(extraction)


def test_byond_extracts_global_proc_path_as_procedure(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="code/global.dm",
        source="/proc/bootstrap()\n    return\n",
    )

    assert len(extraction.symbols) == 1
    symbol = extraction.symbols[0]
    assert symbol.name == "/proc/bootstrap"
    assert symbol.kind == "procedure"
    assert symbol.qualified_name == "/proc/bootstrap"


def test_byond_maps_first_path_segments_to_symbol_kinds(tmp_path) -> None:
    extraction = _extract(
        tmp_path,
        path="code/world.dm",
        source="\n".join(
            (
                "/obj/item",
                "/area/station",
                "/turf/open/floor",
                "/datum/controller",
                "/client",
            ),
        ),
    )

    assert tuple(symbol.kind for symbol in extraction.symbols) == (
        "object",
        "area",
        "turf",
        "datum",
        "path",
    )


def test_byond_returns_empty_extraction_for_non_byond_file(tmp_path) -> None:
    source_path = tmp_path / "src/example.txt"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("/mob/player\n", encoding="utf-8")

    extraction = ByondExtractor().extract(
        DiscoveredFile(
            repository_id="repo",
            path="src/example.txt",
            absolute_path=str(source_path),
            kind="source",
            extension=".txt",
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
            language=None,
        ),
    )

    assert extraction == SymbolExtraction()


def _extract(
    tmp_path,
    *,
    path: str,
    source: str,
    extension: str = ".dm",
) -> SymbolExtraction:
    source_path = tmp_path / path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source, encoding="utf-8")

    return ByondExtractor().extract(
        DiscoveredFile(
            repository_id="repo",
            path=path,
            absolute_path=str(source_path),
            kind="source",
            extension=extension,
            content_hash="hash",
            modified_at="2026-06-20T00:00:00+00:00",
            language="DM",
        ),
    )


def _assert_confidence(extraction: SymbolExtraction) -> None:
    assert tuple(edge.relation_type for edge in extraction.edges) == (
        "declares_symbol",
    ) * len(extraction.edges)
    assert tuple(edge.confidence for edge in extraction.edges) == (
        BYOND_CONFIDENCE,
    ) * len(extraction.edges)
    assert tuple(edge.evidence[0].confidence for edge in extraction.edges) == (
        BYOND_CONFIDENCE,
    ) * len(extraction.edges)
    assert {edge.evidence[0].extractor_name for edge in extraction.edges} == {"byond"}
