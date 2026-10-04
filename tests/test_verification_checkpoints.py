from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from code_scout.verification import (
    CheckpointCaptureError,
    CheckpointInputScope,
    ContentAddressedBlobStore,
    WorkspaceMember,
    capture_workspace_checkpoint,
    parse_verification_policy,
)


def test_capture_retains_dirty_unborn_bytes_modes_and_ignored_exclusions(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "tracked.txt", b"committed")
    _git(repo, "add", "tracked.txt")
    _write(repo / "tracked.txt", b"unstaged")
    _write(repo / "new.txt", b"untracked")
    _write(repo / ".gitignore", b"ignored.txt\nalso-ignored.txt\n")
    _write(repo / "ignored.txt", b"secret")
    _write(repo / "also-ignored.txt", b"excluded")
    script = repo / "run.sh"
    _write(script, b"#!/bin/sh\n")
    script.chmod(0o755)
    checkpoint = _capture(repo, profile_inputs=["ignored.txt"])

    entries = {entry.path: entry for entry in checkpoint.members[0].files}
    assert set(entries) == {
        ".gitignore",
        "ignored.txt",
        "new.txt",
        "run.sh",
        "tracked.txt",
    }
    assert _blob(repo, entries["tracked.txt"].content_sha256) == b"unstaged"
    assert entries["run.sh"].mode == 0o755
    assert checkpoint.excluded_ignored_paths[0].path == "also-ignored.txt"
    assert checkpoint.excluded_ignored_paths[0].rule.endswith("also-ignored.txt")


def test_checkpoint_retains_old_bytes_after_overwrite_and_deletion(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "thing.txt", b"before")
    first = _capture(repo)
    old = first.members[0].files[0]
    _write(repo / "thing.txt", b"after")
    second = _capture(repo, previous=CheckpointInputScope.from_checkpoint(first))
    (repo / "thing.txt").unlink()
    third = _capture(repo, previous=CheckpointInputScope.from_checkpoint(second))

    assert _blob(repo, old.content_sha256) == b"before"
    assert first.workspace_digest != second.workspace_digest != third.workspace_digest
    assert third.members[0].files == ()


def test_hidden_tracked_edits_and_staging_do_not_change_content_identity(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "tracked.txt", b"original")
    _git(repo, "add", "tracked.txt")
    _write(repo / "tracked.txt", b"hidden edit")
    _git(repo, "update-index", "--assume-unchanged", "tracked.txt")
    first = _capture(repo)
    _git(repo, "add", "-f", "tracked.txt")
    second = _capture(repo)

    assert _blob(repo, first.members[0].files[0].content_sha256) == b"hidden edit"
    assert first.workspace_digest == second.workspace_digest


def test_mode_and_declared_scope_changes_change_content_identity(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    script = repo / "run"
    _write(script, b"same")
    first = _capture(repo)
    script.chmod(0o755)
    mode_changed = _capture(repo)
    _write(repo / "declared", b"same")
    scope_changed = _capture(repo, profile_inputs=["declared"])

    assert first.workspace_digest != mode_changed.workspace_digest
    assert mode_changed.workspace_digest != scope_changed.workspace_digest


def test_scope_grows_when_ignore_rule_changes_and_keeps_prior_file(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "later.txt", b"captured first")
    first = _capture(repo)
    _write(repo / ".gitignore", b"later.txt\n")
    second = _capture(repo, previous=CheckpointInputScope.from_checkpoint(first))

    assert {entry.path for entry in second.members[0].files} == {
        ".gitignore",
        "later.txt",
    }
    assert first.input_scope_version != second.input_scope_version
    assert second.excluded_ignored_paths == ()


def test_deduplicates_blobs_and_captures_external_workspace_inputs(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "same-a", b"same")
    _write(repo / "same-b", b"same")
    _write(tmp_path / "integration.txt", b"outside")
    checkpoint = _capture(
        repo, workspace=tmp_path, workspace_inputs=["integration.txt"]
    )

    entries = checkpoint.members[0].files
    assert entries[0].content_sha256 == entries[1].content_sha256
    assert len(list((repo.parent / "blobs").glob("*/*"))) == 2
    assert checkpoint.workspace_files[0].path == "integration.txt"


def test_capture_rejects_overflow_and_unsupported_input_types(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    _write(repo / "too-big", b"12345")
    with pytest.raises(CheckpointCaptureError, match="source input exceeds"):
        _capture(repo, source_limit=4)

    with pytest.raises(CheckpointCaptureError, match="checkpoint logical"):
        _capture(repo, checkpoint_limit=4)

    (repo / "too-big").unlink()
    os.symlink("missing", repo / "link")
    with pytest.raises(CheckpointCaptureError, match="unsupported symlink"):
        _capture(repo)


def _capture(
    repo: Path,
    *,
    previous: CheckpointInputScope | None = None,
    profile_inputs: list[str] | None = None,
    workspace: Path | None = None,
    workspace_inputs: list[str] | None = None,
    source_limit: int | None = None,
    checkpoint_limit: int | None = None,
):
    source_inputs = ", ".join(f'"{item}"' for item in profile_inputs or [])
    outside_inputs = ", ".join(f'"{item}"' for item in workspace_inputs or [])
    limit_values = []
    if source_limit is not None:
        limit_values.append(f"source_file_bytes = {source_limit}")
    if checkpoint_limit is not None:
        limit_values.append(f"checkpoint_bytes = {checkpoint_limit}")
    limit_table = (
        ""
        if not limit_values
        else "[profiles.default.limits]\n" + "\n".join(limit_values) + "\n"
    )
    policy = parse_verification_policy(
        f"""schema_version = 1
[profiles.default]
workspace_input_paths = [{outside_inputs}]
{limit_table}
[profiles.default.repositories.repo]
input_paths = [{source_inputs}]
[[profiles.default.steps]]
id = "check"
scope = "repository"
repository = "repo"
argv = ["true"]
"""
    )
    return capture_workspace_checkpoint(
        members=(WorkspaceMember(id="repo", root=str(repo)),),
        profile=policy.profiles["default"],
        workspace_root=workspace or repo,
        blob_store=ContentAddressedBlobStore(repo.parent / "blobs"),
        previous_scope=previous,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    return path


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def _blob(repo: Path, digest: str) -> bytes:
    return ContentAddressedBlobStore(repo.parent / "blobs").get(digest)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
