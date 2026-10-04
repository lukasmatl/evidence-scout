from __future__ import annotations

from dataclasses import dataclass, field

from code_scout.discovery import DiscoveredFile
from code_scout.extractors import ExtractorRegistry, SymbolExtraction
from code_scout.extractors.byond import ByondExtractor


def test_byond_extracts_minimal_dm_path_and_proc_symbols(tmp_path) -> None:
    source_path = tmp_path / "code/player.dm"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        '/mob/player\n    name = "Player"\n    proc/Login()\n        return\n',
        encoding="utf-8",
    )

    extraction = ByondExtractor().extract(
        _discovered_file(
            path="code/player.dm",
            absolute_path=str(source_path),
            extension=".dm",
            kind="source",
            language="DM",
        ),
    )

    assert tuple(
        (symbol.name, symbol.kind, symbol.qualified_name)
        for symbol in extraction.symbols
    ) == (
        ("/mob/player", "mob", "/mob/player"),
        ("/mob/player/proc/Login", "procedure", "/mob/player/proc/Login"),
    )


def test_byond_extracts_global_proc_declarations_as_procedure_symbols(tmp_path) -> None:
    source_path = tmp_path / "code/procs.dm"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("/proc/bootstrap()\n    return\n", encoding="utf-8")

    extraction = ByondExtractor().extract(
        _discovered_file(
            path="code/procs.dm",
            absolute_path=str(source_path),
            extension=".dm",
            kind="source",
            language="DM",
        ),
    )

    assert tuple(
        (symbol.name, symbol.kind, symbol.qualified_name)
        for symbol in extraction.symbols
    ) == (("/proc/bootstrap", "procedure", "/proc/bootstrap"),)


def test_byond_metadata_assets_do_not_route_to_byond_symbol_extraction() -> None:
    byond = RecordingExtractor()
    registry = ExtractorRegistry(byond_extractor=byond)

    extraction = registry.extract(
        (
            _discovered_file(
                path="icons/player.dmi",
                extension=".dmi",
                kind="metadata",
                language="BYOND Image",
            ),
            _discovered_file(
                path="maps/station.dmm",
                extension=".dmm",
                kind="metadata",
                language="BYOND Map",
            ),
            _discovered_file(
                path="ui/main.dmf",
                extension=".dmf",
                kind="metadata",
                language="BYOND Interface",
            ),
        ),
    )

    assert byond.paths == []
    assert extraction == SymbolExtraction()


@dataclass
class RecordingExtractor:
    paths: list[str] = field(default_factory=list)

    def extract(self, discovered_file: DiscoveredFile) -> SymbolExtraction:
        self.paths.append(discovered_file.path)
        return SymbolExtraction()


def _discovered_file(
    *,
    path: str,
    absolute_path: str | None = None,
    extension: str,
    kind: str,
    language: str | None,
) -> DiscoveredFile:
    return DiscoveredFile(
        repository_id="repo",
        path=path,
        absolute_path=absolute_path or f"/workspace/{path}",
        kind=kind,
        extension=extension,
        content_hash="hash",
        modified_at="2026-06-20T00:00:00+00:00",
        language=language,
    )
