from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from code_scout.core.errors import GitChangeError
from code_scout.impact.git import (
    ChangedLineRange,
    ChangeKind,
    collect_git_changes,
)


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _init(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "code-scout@example.test")
    _git(repository, "config", "user.name", "Code Scout Test")


def _commit_all(repository: Path, message: str) -> str:
    _git(repository, "add", "--all")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def test_staged_changes_exclude_unstaged_content_and_support_unusual_paths(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    source = repository / "sp ace-λ.py"
    source.write_text("one\ntwo\n", encoding="utf-8")
    head = _commit_all(repository, "initial")

    source.write_text("one\nstaged\n", encoding="utf-8")
    _git(repository, "add", "--", source.name)
    staged_blob = _git(repository, "rev-parse", f":{source.name}")
    source.write_text("one\nstaged\nunstaged\n", encoding="utf-8")

    selection = collect_git_changes(repository, staged=True)

    assert selection.repository.id == "workspace"
    assert selection.repository.root == str(repository.resolve())
    assert selection.comparison.mode == "staged"
    assert selection.comparison.old.commit == head
    assert selection.comparison.new.label == "INDEX"
    assert selection.comparison.new.commit is None
    assert len(selection.files) == 1
    changed = selection.files[0]
    assert changed.old_path == source.name
    assert changed.new_path == source.name
    assert changed.kind is ChangeKind.MODIFIED
    assert changed.new_blob == staged_blob
    assert [(item.start_line, item.line_count) for item in changed.old_ranges] == [
        (2, 1)
    ]
    assert [(item.start_line, item.line_count) for item in changed.new_ranges] == [
        (2, 1)
    ]


def test_staged_changed_filenames_are_literal_pathspecs(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    bracketed = repository / "[ab].py"
    matched = repository / "a.py"
    original = "".join(f"line {line}\n" for line in range(1, 13))
    bracketed.write_text(original, encoding="utf-8")
    matched.write_text(original, encoding="utf-8")
    _commit_all(repository, "initial")

    bracketed.write_text(
        original.replace("line 1\n", "bracketed\n", 1), encoding="utf-8"
    )
    matched.write_text(original.replace("line 11\n", "matched\n", 1), encoding="utf-8")
    _git(repository, "add", "--all")

    selection = collect_git_changes(repository, staged=True)
    changes = {item.new_path: item for item in selection.files}

    assert (
        changes["[ab].py"].old_ranges
        == changes["[ab].py"].new_ranges
        == (ChangedLineRange(1, 1),)
    )
    assert (
        changes["a.py"].old_ranges
        == changes["a.py"].new_ranges
        == (ChangedLineRange(11, 1),)
    )


@pytest.mark.parametrize("mode", ["staged", "base_ref"])
def test_per_file_queries_do_not_broaden_exotic_paths(
    tmp_path: Path,
    mode: str,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    text_names = (
        "wild*.py",
        "wildcard.py",
        ":(glob)magic*.py",
        "magic-hit.py",
        "sp ace-λ.py",
    )
    for name in text_names:
        (repository / name).write_text("old\n", encoding="utf-8")
    (repository / "before[ab].py").write_text("rename\n", encoding="utf-8")
    (repository / "beforea.py").write_text("old\n", encoding="utf-8")
    (repository / "after x λ.py").write_text("old\n", encoding="utf-8")
    (repository / "asset[ab].bin").write_bytes(b"old text\n")
    (repository / "asseta.bin").write_bytes(b"old text\n")
    _commit_all(repository, "initial")
    if mode == "base_ref":
        _git(repository, "switch", "-q", "-c", "feature")

    for line, name in enumerate(text_names, start=1):
        (repository / name).write_text(
            f"changed {line}\n" + ("padding\n" * line),
            encoding="utf-8",
        )
    (repository / "before[ab].py").rename(repository / "after * λ.py")
    (repository / "beforea.py").write_text("changed old match\n", encoding="utf-8")
    (repository / "after x λ.py").write_text("changed new match\n", encoding="utf-8")
    (repository / "asset[ab].bin").write_bytes(b"still text\n")
    (repository / "asseta.bin").write_bytes(b"binary\x00content")

    if mode == "staged":
        _git(repository, "add", "--all")
        selection = collect_git_changes(repository, staged=True)
    else:
        head = _commit_all(repository, "feature")
        selection = collect_git_changes(repository, base_ref="main")
        assert selection.comparison.new.commit == head
        assert selection.comparison.requested_base_ref == "main"

    changes = {item.new_path or item.old_path: item for item in selection.files}
    assert set(changes) == {
        *text_names,
        "after * λ.py",
        "beforea.py",
        "after x λ.py",
        "asset[ab].bin",
        "asseta.bin",
    }
    for name in text_names:
        assert changes[name].new_ranges == (
            ChangedLineRange(1, 2 + text_names.index(name)),
        )
    renamed = changes["after * λ.py"]
    assert renamed.kind is ChangeKind.RENAMED
    assert renamed.old_path == "before[ab].py"
    assert renamed.new_path == "after * λ.py"
    assert renamed.old_ranges == renamed.new_ranges == ()
    assert changes["asset[ab].bin"].binary is False
    assert changes["asseta.bin"].binary is True


def test_staged_changes_support_an_unborn_head(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    (repository / "first.txt").write_text("first\n", encoding="utf-8")
    _git(repository, "add", "first.txt")

    selection = collect_git_changes(repository, staged=True)

    assert selection.comparison.old.kind == "empty_tree"
    assert selection.comparison.old.commit == _git(
        repository, "hash-object", "-t", "tree", "--stdin"
    )
    assert selection.files[0].kind is ChangeKind.ADDED
    assert selection.files[0].old_path is None
    assert selection.files[0].old_ranges[0].line_count == 0
    assert selection.files[0].new_ranges[0].line_count == 1


def test_base_ref_uses_merge_base_and_excludes_index_and_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    source = repository / "app.py"
    source.write_text("base\n", encoding="utf-8")
    _commit_all(repository, "base")
    _git(repository, "switch", "-q", "-c", "feature")
    source.write_text("committed\n", encoding="utf-8")
    head = _commit_all(repository, "feature")
    source.write_text("staged\n", encoding="utf-8")
    _git(repository, "add", "app.py")
    source.write_text("unstaged\n", encoding="utf-8")

    selection = collect_git_changes(repository, base_ref="main")

    merge_base = _git(repository, "merge-base", "main", "HEAD")
    assert selection.comparison.requested_base_ref == "main"
    assert selection.comparison.resolved_base_commit == _git(
        repository, "rev-parse", "main"
    )
    assert selection.comparison.old.commit == merge_base
    assert selection.comparison.new.commit == head
    assert selection.files[0].new_blob == _git(repository, "rev-parse", "HEAD:app.py")
    assert selection.files[0].new_blob != _git(repository, "rev-parse", ":app.py")


def test_collects_deleted_renamed_binary_and_mode_only_files(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    (repository / "delete.txt").write_text("remove me\n", encoding="utf-8")
    (repository / "old name.txt").write_text("rename me\n", encoding="utf-8")
    executable = repository / "script.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    _commit_all(repository, "initial")

    (repository / "delete.txt").unlink()
    (repository / "old name.txt").rename(repository / "nëw name.txt")
    (repository / "image.bin").write_bytes(b"\x00\x01\xff")
    executable.chmod(executable.stat().st_mode | 0o111)
    _git(repository, "add", "--all")

    selection = collect_git_changes(repository, staged=True)
    changes = {item.new_path or item.old_path: item for item in selection.files}

    assert changes["delete.txt"].kind is ChangeKind.DELETED
    assert changes["delete.txt"].new_path is None
    assert changes["delete.txt"].new_ranges[0].line_count == 0
    renamed = changes["nëw name.txt"]
    assert renamed.kind is ChangeKind.RENAMED
    assert renamed.old_path == "old name.txt"
    assert renamed.new_path == "nëw name.txt"
    assert renamed.old_ranges == renamed.new_ranges == ()
    assert changes["image.bin"].kind is ChangeKind.ADDED
    assert changes["image.bin"].binary is True
    assert changes["image.bin"].old_ranges == changes["image.bin"].new_ranges == ()
    assert changes["script.sh"].kind is ChangeKind.MODE_CHANGED
    assert changes["script.sh"].old_ranges == changes["script.sh"].new_ranges == ()


def test_invalid_ref_and_missing_merge_base_are_actionable(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    (repository / "file.txt").write_text("main\n", encoding="utf-8")
    _commit_all(repository, "main")

    with pytest.raises(GitChangeError, match="invalid.*missing") as invalid:
        collect_git_changes(repository, base_ref="missing")
    assert "branch, tag, or commit" in (invalid.value.hint or "")

    main = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "--orphan", "orphan")
    (repository / "file.txt").write_text("orphan\n", encoding="utf-8")
    _commit_all(repository, "orphan")

    with pytest.raises(GitChangeError, match="No merge base") as missing_base:
        collect_git_changes(repository, base_ref=main)
    assert "shares history" in (missing_base.value.hint or "")


def test_reports_missing_git_non_git_multi_repo_and_unmerged_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_git = tmp_path / "non-git"
    non_git.mkdir()
    with pytest.raises(GitChangeError, match="not a Git repository"):
        collect_git_changes(non_git, staged=True)

    def missing_git(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr("code_scout.impact.git.subprocess.run", missing_git)
    with pytest.raises(GitChangeError, match="not installed"):
        collect_git_changes(non_git, staged=True)
    monkeypatch.undo()

    multi = tmp_path / "multi"
    (multi / ".code-scout").mkdir(parents=True)
    (multi / "one").mkdir()
    (multi / "two").mkdir()
    (multi / ".code-scout" / "config.yaml").write_text(
        "workspace:\n"
        "  id: multi\n"
        "  name: Multi\n"
        "repositories:\n"
        "  - id: one\n"
        "    path: one\n"
        "  - id: two\n"
        "    path: two\n",
        encoding="utf-8",
    )
    with pytest.raises(GitChangeError, match="one selected repository") as error:
        collect_git_changes(multi, staged=True)
    assert "individual repository workspace" in (error.value.hint or "")

    repository = tmp_path / "conflict"
    _init(repository)
    conflict = repository / "conflict.txt"
    conflict.write_text("base\n", encoding="utf-8")
    _commit_all(repository, "base")
    _git(repository, "switch", "-q", "-c", "other")
    conflict.write_text("other\n", encoding="utf-8")
    _commit_all(repository, "other")
    _git(repository, "switch", "-q", "main")
    conflict.write_text("main\n", encoding="utf-8")
    _commit_all(repository, "main")
    _git(repository, "merge", "other", check=False)
    with pytest.raises(GitChangeError, match="unmerged") as unmerged:
        collect_git_changes(repository, staged=True)
    assert "conflicts" in (unmerged.value.hint or "")


def test_collection_does_not_mutate_git_state(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init(repository)
    tracked = repository / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _commit_all(repository, "initial")
    tracked.write_text("after\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")

    before_state = (
        _git(repository, "status", "--porcelain=v2"),
        _git(repository, "show-ref"),
        _git(repository, "count-objects", "-v"),
    )
    before_index_mtime = os.stat(repository / ".git" / "index").st_mtime_ns
    collect_git_changes(repository, staged=True)
    after_index_mtime = os.stat(repository / ".git" / "index").st_mtime_ns
    after_state = (
        _git(repository, "status", "--porcelain=v2"),
        _git(repository, "show-ref"),
        _git(repository, "count-objects", "-v"),
    )

    assert after_state == before_state
    assert after_index_mtime == before_index_mtime


@pytest.mark.parametrize(
    ("staged", "base_ref"),
    [(False, None), (True, "main")],
)
def test_requires_exactly_one_change_mode(
    tmp_path: Path,
    staged: bool,
    base_ref: str | None,
) -> None:
    with pytest.raises(GitChangeError, match="exactly one"):
        collect_git_changes(tmp_path, staged=staged, base_ref=base_ref)
