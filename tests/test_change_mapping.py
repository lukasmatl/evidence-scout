from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from code_scout.impact.git import collect_git_changes
from code_scout.impact.mapping import (
    MappingSide,
    MappingStatus,
    map_changed_ranges_to_symbols,
)
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.storage import database_path, open_storage, upsert_active_facts
from code_scout.workspace.status import WorkspaceStatusReport


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _init(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "code-scout@example.test")
    _git(repository, "config", "user.name", "Code Scout Test")


def _commit(repository: Path, message: str = "change") -> str:
    _git(repository, "add", "--all")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _status(
    repository: Path,
    *,
    freshness: str = "stale",
    language: str = "Python",
    grammar_state: str = "observed",
) -> WorkspaceStatusReport:
    return WorkspaceStatusReport(
        workspace={
            "id": "workspace",
            "root_path": str(repository),
            "repositories": [{"id": "workspace", "path": "."}],
        },
        index={"state": "ready", "database_path": "unused"},
        freshness={
            "state": freshness,
            "coverage": "complete",
            "coverage_reasons": [],
        },
        changes={},
        coverage={
            "grammar_support": [
                {"language": language, "state": grammar_state},
            ],
            "extractors": {
                "states": [
                    {
                        "name": "symbol_extraction",
                        "state": "observed",
                    }
                ]
            },
        },
    )


def _seed_index(
    repository: Path,
    *,
    path: str,
    content: str | bytes,
    symbols: tuple[tuple[str, int | None, int | None], ...] = (),
    language: str | None = "Python",
) -> None:
    payload = content.encode() if isinstance(content, str) else content
    file_id = f"file:workspace:{path}"
    evidence = FactEvidence(
        file_path=path,
        extractor_name="tree_sitter",
        confidence=1.0,
        start_line=1,
        end_line=None,
        metadata={"source": "test-index"},
    )
    facts = [
        ActiveFact(
            id=f"fact:file:{path}",
            extractor="file_discovery",
            kind="file",
            source_file=path,
            identity={"entity_type": "file", "entity_id": file_id},
            properties={
                "repository_id": "workspace",
                "path": path,
                "kind": "source",
                "extension": Path(path).suffix,
                "language": language,
                "content_hash": hashlib.sha256(payload).hexdigest(),
            },
            evidence=(evidence,),
            confidence=1.0,
        )
    ]
    facts.extend(
        ActiveFact(
            id=f"fact:symbol:{path}:{name}",
            extractor="tree_sitter",
            kind="symbol",
            source_file=path,
            identity={
                "entity_type": "symbol",
                "entity_id": f"symbol:workspace:{path}:{name}",
            },
            properties={
                "file_id": file_id,
                "name": name,
                "kind": "function",
                "qualified_name": name,
                "start_line": start,
                "end_line": end,
            },
            evidence=(
                FactEvidence(
                    file_path=path,
                    extractor_name="tree_sitter",
                    confidence=0.9,
                    start_line=start,
                    end_line=end,
                    metadata={"source": "test-symbol"},
                ),
            ),
            confidence=0.9,
        )
        for name, start, end in symbols
    )
    with open_storage(repository) as connection:
        upsert_active_facts(connection, facts)
        connection.commit()


def test_maps_new_blob_insertions_to_nested_symbols_with_provenance(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    source = repository / "app.py"
    source.write_text("def outer():\n    return 1\n", encoding="utf-8")
    _commit(repository, "old")
    new_content = (
        "def outer():\n    def inner():\n        return 2\n    return inner()\n"
    )
    source.write_text(new_content, encoding="utf-8")
    _git(repository, "add", "app.py")
    _seed_index(
        repository,
        path="app.py",
        content=new_content,
        symbols=(
            ("outer", 1, 4),
            ("inner", 2, 3),
            ("unknown", None, None),
        ),
    )

    index_mtime = database_path(repository).stat().st_mtime_ns
    result = map_changed_ranges_to_symbols(
        repository,
        collect_git_changes(repository, staged=True),
        _status(repository),
    )

    assert result.freshness["state"] == "stale"
    assert result.files[0].mapping_status is MappingStatus.MAPPED
    assert result.files[0].mapping_side is MappingSide.NEW
    assert [symbol.name for symbol in result.symbols] == ["inner", "outer"]
    assert all(symbol.mapping_side is MappingSide.NEW for symbol in result.symbols)
    assert result.symbols[0].provenance[0].extractor_name == "tree_sitter"
    assert result.symbols[0].provenance[0].metadata == {"source": "test-symbol"}
    assert "missing_symbol_ranges" in {gap.code for gap in result.coverage_gaps}
    assert database_path(repository).stat().st_mtime_ns == index_mtime


def test_literal_path_hunks_do_not_select_symbols_from_a_matching_file(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    bracketed = repository / "[ab].py"
    matched = repository / "a.py"
    original = "".join(f"line {line}\n" for line in range(1, 13))
    bracketed.write_text(original, encoding="utf-8")
    matched.write_text(original, encoding="utf-8")
    _commit(repository, "initial")
    bracketed_content = original.replace("line 1\n", "bracketed\n", 1)
    matched_content = original.replace("line 11\n", "matched\n", 1)
    bracketed.write_text(bracketed_content, encoding="utf-8")
    matched.write_text(matched_content, encoding="utf-8")
    _git(repository, "add", "--all")
    _seed_index(
        repository,
        path="[ab].py",
        content=bracketed_content,
        symbols=(("bracketed_changed", 1, 1), ("unrelated_hunk", 11, 11)),
    )
    _seed_index(
        repository,
        path="a.py",
        content=matched_content,
        symbols=(("matched_changed", 11, 11),),
    )

    result = map_changed_ranges_to_symbols(
        repository,
        collect_git_changes(repository, staged=True),
        _status(repository),
    )

    assert [symbol.name for symbol in result.symbols] == [
        "bracketed_changed",
        "matched_changed",
    ]


def test_partial_staging_maps_old_blob_but_rejects_a_third_content_hash(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    source = repository / "app.py"
    old_content = "def value():\n    return 1\n"
    source.write_text(old_content, encoding="utf-8")
    _commit(repository, "old")
    _seed_index(
        repository,
        path="app.py",
        content=old_content,
        symbols=(("value", 1, 2),),
    )
    source.write_text("def value():\n    return 2\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    unstaged_content = "def value():\n    return 3\n"
    source.write_text(unstaged_content, encoding="utf-8")
    selection = collect_git_changes(repository, staged=True)

    old_result = map_changed_ranges_to_symbols(
        repository,
        selection,
        _status(repository),
    )
    assert old_result.files[0].mapping_side is MappingSide.OLD
    assert old_result.symbols[0].changed_ranges[0].side is MappingSide.OLD

    _seed_index(
        repository,
        path="app.py",
        content=unstaged_content,
        symbols=(("value", 1, 2),),
    )
    stale_result = map_changed_ranges_to_symbols(
        repository,
        selection,
        _status(repository),
    )
    assert stale_result.files[0].mapping_status is MappingStatus.UNRESOLVED
    assert stale_result.files[0].mapping_reason == "indexed_content_mismatch"
    assert stale_result.symbols == ()


def test_deleted_symbol_and_renamed_file_map_against_old_blob(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    removed = repository / "removed.py"
    renamed = repository / "before.py"
    removed_content = "def removed():\n    return True\n"
    renamed_content = "def renamed():\n    return True\n"
    removed.write_text(removed_content, encoding="utf-8")
    renamed.write_text(renamed_content, encoding="utf-8")
    _commit(repository, "old")
    _seed_index(
        repository,
        path="removed.py",
        content=removed_content,
        symbols=(("removed", 1, 2),),
    )
    _seed_index(
        repository,
        path="before.py",
        content=renamed_content,
        symbols=(("renamed", 1, 2),),
    )
    removed.unlink()
    renamed.rename(repository / "after.py")
    _git(repository, "add", "--all")

    result = map_changed_ranges_to_symbols(
        repository,
        collect_git_changes(repository, staged=True),
        _status(repository),
    )
    files = {seed.new_path or seed.old_path: seed for seed in result.files}

    assert files["removed.py"].mapping_side is MappingSide.OLD
    assert files["removed.py"].mapping_status is MappingStatus.MAPPED
    assert files["after.py"].mapping_side is MappingSide.OLD
    assert files["after.py"].indexed_path == "before.py"
    assert files["after.py"].mapping_reason == "no_changed_text_ranges"
    assert [symbol.name for symbol in result.symbols] == ["removed"]


def test_retains_binary_unindexed_unsupported_and_unavailable_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    binary = repository / "asset.bin"
    unknown = repository / "notes.unknown"
    unavailable = repository / "code.py"
    binary.write_bytes(b"old\x00")
    unknown.write_text("old\n", encoding="utf-8")
    unavailable.write_text("old\n", encoding="utf-8")
    _commit(repository, "old")
    binary.write_bytes(b"new\x00")
    unknown.write_text("new\n", encoding="utf-8")
    unavailable.write_text("new\n", encoding="utf-8")
    (repository / "unindexed.py").write_text("new\n", encoding="utf-8")
    _git(repository, "add", "--all")
    _seed_index(repository, path="asset.bin", content=b"new\x00", language=None)
    _seed_index(repository, path="notes.unknown", content="new\n", language=None)
    _seed_index(repository, path="code.py", content="new\n")

    result = map_changed_ranges_to_symbols(
        repository,
        collect_git_changes(repository, staged=True),
        _status(repository, grammar_state="unavailable"),
    )
    files = {seed.new_path or seed.old_path: seed for seed in result.files}

    assert files["asset.bin"].mapping_reason == "binary_change"
    assert files["notes.unknown"].mapping_reason == "unsupported_language"
    assert files["code.py"].mapping_reason == "symbol_extraction_unavailable"
    assert files["unindexed.py"].mapping_status is MappingStatus.UNRESOLVED
    assert files["unindexed.py"].mapping_reason == "unindexed_file"
    assert {gap.code for gap in result.coverage_gaps} >= {
        "binary_change",
        "unsupported_language",
        "symbol_extraction_unavailable",
        "unindexed_file",
    }


def test_unavailable_index_retains_file_seed_without_creating_state(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    source = repository / "app.py"
    source.write_text("old\n", encoding="utf-8")
    _commit(repository, "old")
    source.write_text("new\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    status = _status(repository)
    status.index["state"] = "unscanned"

    result = map_changed_ranges_to_symbols(
        repository,
        collect_git_changes(repository, staged=True),
        status,
    )

    assert result.files[0].mapping_reason == "index_unavailable"
    assert result.symbols == ()
    assert not (repository / ".code-scout").exists()
