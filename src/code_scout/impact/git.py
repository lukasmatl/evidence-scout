"""Read-only collection of explicit Git change selections."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

from code_scout.core.errors import GitChangeError
from code_scout.workspace.config import (
    DEFAULT_CONFIG_PATH,
    WorkspaceConfig,
    load_workspace_config,
)

_HUNK_HEADER = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class ChangeKind(StrEnum):
    """Stable classifications for files returned by a Git comparison."""

    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    MODE_CHANGED = "mode_changed"


@dataclass(frozen=True)
class ChangedLineRange:
    """One side of a zero-context Git diff hunk."""

    start_line: int
    line_count: int

    @property
    def end_line(self) -> int | None:
        if self.line_count == 0:
            return None
        return self.start_line + self.line_count - 1


@dataclass(frozen=True)
class GitRepositoryIdentity:
    id: str
    display_name: str
    path: str
    root: str


@dataclass(frozen=True)
class GitRevisionIdentity:
    label: str
    kind: str
    commit: str | None


@dataclass(frozen=True)
class GitComparison:
    mode: str
    old: GitRevisionIdentity
    new: GitRevisionIdentity
    requested_base_ref: str | None = None
    resolved_base_commit: str | None = None


@dataclass(frozen=True)
class ChangedFile:
    old_path: str | None
    new_path: str | None
    kind: ChangeKind
    old_ranges: tuple[ChangedLineRange, ...]
    new_ranges: tuple[ChangedLineRange, ...]
    old_blob: str | None
    new_blob: str | None
    old_mode: str
    new_mode: str
    binary: bool


@dataclass(frozen=True)
class GitChangeSelection:
    repository: GitRepositoryIdentity
    comparison: GitComparison
    files: tuple[ChangedFile, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _RawChange:
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    status: str
    old_path: str | None
    new_path: str | None


def collect_git_changes(
    workspace: str | Path,
    *,
    staged: bool = False,
    base_ref: str | None = None,
) -> GitChangeSelection:
    """Collect exactly one staged or committed branch comparison without writes."""
    if staged == (base_ref is not None):
        raise GitChangeError(
            "Select exactly one Git change mode: staged or base ref.",
            "Pass `staged=True` or pass `base_ref=<ref>`, but not both.",
        )
    if base_ref is not None and not base_ref.strip():
        raise GitChangeError(
            "The Git base ref must not be empty.",
            "Pass a branch, tag, or commit such as `main` or `HEAD~1`.",
        )
    repository, repository_path = _resolve_repository(Path(workspace))
    git_root = (
        _git_output(
            repository_path,
            ["rev-parse", "--show-toplevel"],
            failure="The selected repository is not a Git repository.",
            hint="Select a workspace whose repository path is a Git working tree.",
        )
        .decode(errors="surrogateescape")
        .rstrip("\n")
    )
    resolved_git_root = Path(git_root).resolve()
    if resolved_git_root != repository_path:
        raise GitChangeError(
            "Configured repository path is not the Git repository root: "
            f"{repository.path}.",
            f"Select the individual repository workspace at {resolved_git_root}.",
        )
    repository = GitRepositoryIdentity(
        id=repository.id,
        display_name=repository.display_name,
        path=repository.path,
        root=str(resolved_git_root),
    )

    unmerged = _git(repository_path, ["ls-files", "--unmerged", "-z"])
    if unmerged.returncode != 0:
        _raise_git_failure(unmerged, "Git could not inspect unmerged index entries.")
    if unmerged.stdout:
        raise GitChangeError(
            "The selected repository contains unmerged index entries.",
            "Resolve or abort the merge conflicts before collecting impact changes.",
        )

    if staged:
        comparison, diff_prefix = _staged_comparison(repository_path)
    else:
        assert base_ref is not None
        comparison, diff_prefix = _base_comparison(repository_path, base_ref)

    raw_output = _git_output(
        repository_path,
        [*diff_prefix, "--raw", "-z", "--no-abbrev", "--find-renames"],
        failure="Git could not collect the requested change list.",
        hint="Check the repository and comparison refs, then retry.",
    )
    raw_changes = _parse_raw_changes(raw_output)
    files = tuple(
        _materialize_change(repository_path, diff_prefix, change)
        for change in raw_changes
    )
    return GitChangeSelection(
        repository=repository,
        comparison=comparison,
        files=files,
    )


def _resolve_repository(
    workspace: Path,
) -> tuple[GitRepositoryIdentity, Path]:
    root = workspace.expanduser().resolve()
    if not root.is_dir():
        raise GitChangeError(
            f"Workspace directory does not exist: {workspace}.",
            "Pass an existing workspace directory.",
        )

    config_path = root / DEFAULT_CONFIG_PATH
    config = load_workspace_config(root) if config_path.exists() else WorkspaceConfig()
    if len(config.repositories) != 1:
        raise GitChangeError(
            "Git impact requires one selected repository; found "
            f"{len(config.repositories)}.",
            "Select an individual repository workspace instead of combining refs "
            "from multiple repositories.",
        )

    selected = config.repositories[0]
    configured_path = Path(selected.path)
    repository_path = (
        configured_path if configured_path.is_absolute() else root / configured_path
    ).resolve()
    try:
        repository_path.relative_to(root)
    except ValueError as error:
        raise GitChangeError(
            f"Configured repository path escapes the workspace: {selected.path}.",
            "Select an individual repository workspace below the workspace root.",
        ) from error
    if not repository_path.is_dir():
        raise GitChangeError(
            f"Configured repository path does not exist: {selected.path}.",
            "Create the repository directory or correct `.code-scout/config.yaml`.",
        )
    return (
        GitRepositoryIdentity(
            id=selected.id,
            display_name=selected.name or selected.id,
            path=selected.path,
            root=str(repository_path),
        ),
        repository_path,
    )


def _staged_comparison(repository: Path) -> tuple[GitComparison, list[str]]:
    head = _git(repository, ["rev-parse", "--verify", "HEAD^{commit}"])
    if head.returncode == 0:
        old = GitRevisionIdentity(
            label="HEAD", kind="commit", commit=_single_line(head.stdout)
        )
    else:
        empty_tree = _git_output(
            repository,
            ["hash-object", "-t", "tree", "--stdin"],
            input_bytes=b"",
            failure="Git could not identify the empty tree for an unborn branch.",
            hint="Check the repository object format and retry.",
        )
        old = GitRevisionIdentity(
            label="EMPTY_TREE", kind="empty_tree", commit=_single_line(empty_tree)
        )
    return (
        GitComparison(
            mode="staged",
            old=old,
            new=GitRevisionIdentity(label="INDEX", kind="index", commit=None),
        ),
        ["diff", "--cached"],
    )


def _base_comparison(
    repository: Path,
    base_ref: str,
) -> tuple[GitComparison, list[str]]:
    head = _resolve_commit(repository, "HEAD", "The current HEAD is not a commit.")
    base = _resolve_commit(
        repository,
        base_ref,
        f"Git base ref is invalid or does not name a commit: {base_ref}.",
    )
    merge_base = _git(repository, ["merge-base", base, head])
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        raise GitChangeError(
            f"No merge base exists between {base_ref} and HEAD.",
            "Choose a base ref that shares history with the current branch.",
        )
    ancestor = _single_line(merge_base.stdout)
    return (
        GitComparison(
            mode="base_ref",
            requested_base_ref=base_ref,
            resolved_base_commit=base,
            old=GitRevisionIdentity(
                label=f"merge-base({base_ref}, HEAD)",
                kind="commit",
                commit=ancestor,
            ),
            new=GitRevisionIdentity(label="HEAD", kind="commit", commit=head),
        ),
        ["diff", ancestor, head],
    )


def _resolve_commit(repository: Path, ref: str, failure: str) -> str:
    result = _git(
        repository,
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
    )
    if result.returncode != 0:
        raise GitChangeError(failure, "Pass an existing branch, tag, or commit ref.")
    return _single_line(result.stdout)


def _parse_raw_changes(payload: bytes) -> tuple[_RawChange, ...]:
    fields = payload.split(b"\0")
    changes: list[_RawChange] = []
    index = 0
    while index < len(fields) and fields[index]:
        header = fields[index]
        index += 1
        try:
            metadata = header.decode("ascii").split()
            old_mode, new_mode, old_blob, new_blob, status = metadata
            code = status[0]
            old_path = _path(fields[index])
            index += 1
            if code in {"R", "C"}:
                new_path = _path(fields[index])
                index += 1
            elif code == "A":
                new_path, old_path = old_path, None
            elif code == "D":
                new_path = None
            else:
                new_path = old_path
        except (IndexError, UnicodeDecodeError, ValueError) as error:
            raise GitChangeError(
                "Git returned an unrecognized raw diff record.",
                "Upgrade Git or retry with a supported repository format.",
            ) from error
        changes.append(
            _RawChange(
                old_mode=old_mode.removeprefix(":"),
                new_mode=new_mode,
                old_blob=old_blob,
                new_blob=new_blob,
                status=status,
                old_path=old_path,
                new_path=new_path,
            )
        )
    return tuple(changes)


def _materialize_change(
    repository: Path,
    diff_prefix: list[str],
    change: _RawChange,
) -> ChangedFile:
    paths = [path for path in (change.old_path, change.new_path) if path is not None]
    pathspecs = _literal_pathspecs(paths)
    patch = _git_output(
        repository,
        [
            *diff_prefix,
            "--patch",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            "--",
            *pathspecs,
        ],
        failure=f"Git could not read the diff for {paths[-1]!r}.",
        hint="Check repository attributes and retry the comparison.",
    )
    old_ranges, new_ranges = _parse_hunks(patch)
    binary = _is_binary(repository, diff_prefix, paths, pathspecs)
    return ChangedFile(
        old_path=change.old_path,
        new_path=change.new_path,
        kind=_change_kind(change),
        old_ranges=old_ranges,
        new_ranges=new_ranges,
        old_blob=None if set(change.old_blob) == {"0"} else change.old_blob,
        new_blob=None if set(change.new_blob) == {"0"} else change.new_blob,
        old_mode=change.old_mode,
        new_mode=change.new_mode,
        binary=binary,
    )


def _parse_hunks(
    patch: bytes,
) -> tuple[tuple[ChangedLineRange, ...], tuple[ChangedLineRange, ...]]:
    old_ranges: list[ChangedLineRange] = []
    new_ranges: list[ChangedLineRange] = []
    for line in patch.splitlines():
        match = _HUNK_HEADER.match(line)
        if match is None:
            continue
        old_start, old_count, new_start, new_count = match.groups()
        old_ranges.append(
            ChangedLineRange(int(old_start), 1 if old_count is None else int(old_count))
        )
        new_ranges.append(
            ChangedLineRange(int(new_start), 1 if new_count is None else int(new_count))
        )
    return tuple(old_ranges), tuple(new_ranges)


def _literal_pathspecs(paths: list[str]) -> list[str]:
    return [f":(literal){path}" for path in dict.fromkeys(paths)]


def _is_binary(
    repository: Path,
    diff_prefix: list[str],
    paths: list[str],
    pathspecs: list[str],
) -> bool:
    output = _git_output(
        repository,
        [*diff_prefix, "--numstat", "-z", "--find-renames", "--", *pathspecs],
        failure=f"Git could not classify the diff for {paths[-1]!r}.",
        hint="Check repository attributes and retry the comparison.",
    )
    first_record = output.split(b"\0", 1)[0]
    columns = first_record.split(b"\t", 2)
    return len(columns) >= 2 and columns[0] == columns[1] == b"-"


def _change_kind(change: _RawChange) -> ChangeKind:
    code = change.status[0]
    if code == "A":
        return ChangeKind.ADDED
    if code == "D":
        return ChangeKind.DELETED
    if code == "R":
        return ChangeKind.RENAMED
    if code == "C":
        return ChangeKind.COPIED
    if code == "T":
        return ChangeKind.TYPE_CHANGED
    if change.old_mode != change.new_mode and change.old_blob == change.new_blob:
        return ChangeKind.MODE_CHANGED
    return ChangeKind.MODIFIED


def _path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _single_line(value: bytes) -> str:
    return value.decode("ascii").strip()


def _git_output(
    repository: Path,
    arguments: list[str],
    *,
    failure: str,
    hint: str,
    input_bytes: bytes | None = None,
) -> bytes:
    result = _git(repository, arguments, input_bytes=input_bytes)
    if result.returncode != 0:
        _raise_git_failure(result, failure, hint)
    return result.stdout


def _git(
    repository: Path,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            env=environment,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise GitChangeError(
            f"Git is not installed or could not be executed: {error}.",
            "Install Git and ensure it is available on PATH.",
        ) from error


def _raise_git_failure(
    result: subprocess.CompletedProcess[bytes],
    failure: str,
    hint: str | None = None,
) -> None:
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    message = failure if not detail else f"{failure} Git reported: {detail}"
    raise GitChangeError(message, hint)


__all__ = [
    "ChangeKind",
    "ChangedFile",
    "ChangedLineRange",
    "GitChangeSelection",
    "GitComparison",
    "GitRepositoryIdentity",
    "GitRevisionIdentity",
    "collect_git_changes",
]
