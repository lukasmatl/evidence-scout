"""Capture exact, content-addressed working-tree checkpoints without Git writes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from code_scout.verification.contracts import (
    CapturedFile,
    CapturedMember,
    ExcludedIgnoredPath,
    WorkspaceCheckpoint,
    WorkspaceMember,
)
from code_scout.verification.policy import VerificationProfile


class CheckpointCaptureError(ValueError):
    """The live workspace cannot be represented safely as a checkpoint."""


@dataclass(frozen=True)
class CheckpointInputScope:
    """Exact paths admitted by earlier captures, retained monotonically."""

    member_paths: Mapping[str, frozenset[str]]
    workspace_paths: frozenset[str]

    @classmethod
    def empty(cls) -> CheckpointInputScope:
        return cls({}, frozenset())

    @classmethod
    def from_checkpoint(cls, checkpoint: WorkspaceCheckpoint) -> CheckpointInputScope:
        member_paths = {
            member.id: frozenset(member.input_scope) for member in checkpoint.members
        }
        return cls(
            member_paths,
            frozenset(
                checkpoint.workspace_input_scope
                or tuple(entry.path for entry in checkpoint.workspace_files)
            ),
        )


class ContentAddressedBlobStore:
    """Append-only local byte store keyed by the SHA-256 of exact source bytes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, source: bytes) -> str:
        digest = hashlib.sha256(source).hexdigest()
        path = self.root / digest[:2] / digest[2:]
        if path.exists():
            if not path.is_file() or path.read_bytes() != source:
                raise CheckpointCaptureError(f"corrupt content-addressed blob {digest}")
            return digest
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source)
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return digest

    def get(self, digest: str) -> bytes:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise CheckpointCaptureError("invalid content-addressed blob digest")
        try:
            source = (self.root / digest[:2] / digest[2:]).read_bytes()
        except OSError as error:
            raise CheckpointCaptureError(
                f"missing content-addressed blob {digest}"
            ) from error
        if hashlib.sha256(source).hexdigest() != digest:
            raise CheckpointCaptureError(f"corrupt content-addressed blob {digest}")
        return source


def capture_workspace_checkpoint(
    *,
    members: tuple[WorkspaceMember, ...],
    profile: VerificationProfile,
    workspace_root: Path,
    blob_store: ContentAddressedBlobStore,
    previous_scope: CheckpointInputScope | None = None,
    nested_member_roots: tuple[Path, ...] = (),
) -> WorkspaceCheckpoint:
    """Read current working bytes and retain them without staging or modifying Git.

    ``previous_scope`` is normally derived from the session checkpoint. It keeps
    a previously selected file in scope if a later ignore rule would hide it.
    """

    scope = previous_scope or CheckpointInputScope.empty()
    member_by_id = {member.id: member for member in members}
    if set(member_by_id) != set(profile.repositories):
        raise CheckpointCaptureError(
            "profile repositories must match checkpoint members"
        )
    root = workspace_root.resolve()
    captured_members: list[CapturedMember] = []
    excluded: list[ExcludedIgnoredPath] = []
    for member in sorted(members, key=lambda item: item.id):
        member_root = Path(member.root).resolve()
        _require_directory(member_root, f"member {member.id}")
        _reject_gitlinks(member_root)
        tracked = _git_paths(member_root, ["ls-files", "-z"])
        untracked = _git_paths(
            member_root, ["ls-files", "--others", "--exclude-standard", "-z"]
        )
        declared = _expand_patterns(
            member_root,
            (
                *profile.repositories[member.id].input_paths,
                *profile.repositories[member.id].protected_paths,
            ),
        )
        prior = scope.member_paths.get(member.id, frozenset())
        paths = set(tracked) | set(untracked) | declared | set(prior)
        paths = _without_nested_member_paths(member_root, paths, nested_member_roots)
        _reject_undeclared_nested_repositories(member_root, paths, nested_member_roots)
        files = _capture_files(
            member_root,
            paths,
            blob_store,
            source_limit=profile.limits.source_file_bytes,
        )
        ignored = _ignored_paths(member_root)
        excluded.extend(
            ExcludedIgnoredPath(owner_id=member.id, path=path, rule=rule)
            for path, rule in ignored.items()
            if path not in paths
        )
        captured_members.append(
            CapturedMember(
                id=member.id,
                role=member.role,
                worktree_identity=member.worktree_identity or str(member_root),
                files=files,
                input_scope=tuple(sorted(paths)),
                content_digest=_member_digest(member, member_root, files, paths),
            )
        )

    workspace_patterns = (
        *profile.workspace_input_paths,
        *profile.workspace_protected_paths,
    )
    workspace_paths = _expand_patterns(root, workspace_patterns) | set(
        scope.workspace_paths
    )
    member_roots = tuple(Path(member.root).resolve() for member in members)
    if any(
        (root / path).resolve().is_relative_to(member_root)
        for path in workspace_paths
        for member_root in member_roots
    ):
        raise CheckpointCaptureError(
            "workspace input is owned by a selected repository"
        )
    workspace_files = _capture_files(
        root,
        workspace_paths,
        blob_store,
        source_limit=profile.limits.source_file_bytes,
    )
    logical_bytes = sum(
        entry.size_bytes for member in captured_members for entry in member.files
    ) + sum(entry.size_bytes for entry in workspace_files)
    if logical_bytes > profile.limits.checkpoint_bytes:
        raise CheckpointCaptureError(
            "checkpoint logical source bytes exceed the frozen checkpoint limit"
        )
    aggregate_scope = {
        member.id: tuple(sorted(member.input_scope)) for member in captured_members
    }
    aggregate_scope["workspace"] = tuple(sorted(workspace_paths))
    scope_version = _digest(aggregate_scope)
    workspace_digest = _digest(
        {
            "members": [
                {
                    "id": member.id,
                    "role": member.role.value,
                    "worktree_identity": member.worktree_identity,
                    "content_digest": member.content_digest,
                }
                for member in captured_members
            ],
            "workspace_files": [_file_record(entry) for entry in workspace_files],
            "input_scope_version": scope_version,
        }
    )
    return WorkspaceCheckpoint(
        workspace_digest=workspace_digest,
        member_digests=tuple(
            (member.id, member.content_digest) for member in captured_members
        ),
        members=tuple(captured_members),
        workspace_files=workspace_files,
        workspace_input_scope=tuple(sorted(workspace_paths)),
        input_scope_version=scope_version,
        excluded_ignored_paths=tuple(
            sorted(excluded, key=lambda item: (item.owner_id, item.path))
        ),
    )


def _capture_files(
    root: Path,
    paths: Iterable[str],
    store: ContentAddressedBlobStore,
    *,
    source_limit: int,
) -> tuple[CapturedFile, ...]:
    files: list[CapturedFile] = []
    for path in sorted(set(paths)):
        _safe_path(path)
        candidate = root / path
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise CheckpointCaptureError(f"cannot read {path}: {error}") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CheckpointCaptureError(f"unsupported symlink input: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckpointCaptureError(f"unsupported filesystem input: {path}")
        if metadata.st_size > source_limit:
            raise CheckpointCaptureError(
                f"source input exceeds the frozen limit: {path}"
            )
        try:
            source = candidate.read_bytes()
        except OSError as error:
            raise CheckpointCaptureError(f"cannot read {path}: {error}") from error
        if len(source) != metadata.st_size:
            raise CheckpointCaptureError(f"unstable source read: {path}")
        files.append(
            CapturedFile(
                path=path,
                content_sha256=store.put(source),
                mode=stat.S_IMODE(metadata.st_mode),
                size_bytes=len(source),
            )
        )
    return tuple(files)


def _without_nested_member_paths(
    root: Path, paths: set[str], nested_roots: tuple[Path, ...]
) -> set[str]:
    children = tuple(
        child.resolve()
        for child in nested_roots
        if child.resolve() != root and child.resolve().is_relative_to(root)
    )
    return {
        path
        for path in paths
        if not any((root / path).resolve().is_relative_to(child) for child in children)
    }


def _reject_undeclared_nested_repositories(
    root: Path, paths: set[str], nested_roots: tuple[Path, ...]
) -> None:
    declared = {child.resolve() for child in nested_roots}
    for path in paths:
        candidate = root / path
        current = candidate if candidate.is_dir() else candidate.parent
        while current != root:
            if (current / ".git").exists() and current.resolve() not in declared:
                raise CheckpointCaptureError(
                    f"undeclared nested Git repository in included input: {current}"
                )
            current = current.parent


def _expand_patterns(root: Path, patterns: Iterable[str]) -> set[str]:
    matches: set[str] = set()
    for pattern in patterns:
        _safe_path(pattern)
        for candidate in root.glob(pattern):
            if candidate.is_dir():
                continue
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError as error:
                raise CheckpointCaptureError(
                    f"declared input escapes root: {pattern}"
                ) from error
            matches.add(relative)
    return matches


def _ignored_paths(root: Path) -> dict[str, str]:
    paths = _git_paths(
        root,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
    )
    if not paths:
        return {}
    result = _git(
        root, ["check-ignore", "-z", "-v", "--stdin"], "\0".join(paths).encode() + b"\0"
    )
    if result.returncode not in {0, 1}:
        raise CheckpointCaptureError("Git could not inspect ignored input rules")
    fields = result.stdout.split(b"\0")
    rules: dict[str, str] = {}
    for index in range(0, len(fields) - 4, 4):
        source, line, pattern, path = fields[index : index + 4]
        if path:
            rules[_decode_path(path)] = ":".join(
                (
                    source.decode("utf-8", "replace"),
                    line.decode(),
                    pattern.decode("utf-8", "replace"),
                )
            )
    return {path: rules.get(path, "<unknown>") for path in paths}


def _git_paths(root: Path, arguments: list[str]) -> tuple[str, ...]:
    result = _git(root, arguments)
    if result.returncode != 0:
        raise CheckpointCaptureError("Git could not enumerate workspace inputs")
    return tuple(
        path
        for item in result.stdout.split(b"\0")
        if item
        if not (path := _decode_path(item)).startswith(".code-scout/verification/")
    )


def _reject_gitlinks(root: Path) -> None:
    result = _git(root, ["ls-files", "-s", "-z"])
    if result.returncode != 0:
        raise CheckpointCaptureError("Git could not inspect index entries")
    for item in result.stdout.split(b"\0"):
        if item.startswith(b"160000 "):
            raise CheckpointCaptureError("submodule/gitlink inputs are unsupported")


def _git(
    root: Path, arguments: list[str], input_bytes: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            input=input_bytes,
            capture_output=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"},
        )
    except OSError as error:
        raise CheckpointCaptureError(f"Git could not execute: {error}") from error


def _decode_path(value: bytes) -> str:
    try:
        path = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CheckpointCaptureError("unsupported non-UTF-8 filename") from error
    _safe_path(path)
    return path


def _safe_path(path: str) -> None:
    if not path or "\x00" in path or any(ord(char) < 32 for char in path):
        raise CheckpointCaptureError("unsupported control-character filename")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise CheckpointCaptureError(f"unsafe checkpoint path: {path}")


def _require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise CheckpointCaptureError(f"{label} is not an accessible directory")


def _member_digest(
    member: WorkspaceMember,
    root: Path,
    files: tuple[CapturedFile, ...],
    scope: set[str],
) -> str:
    return _digest(
        {
            "id": member.id,
            "role": member.role.value,
            "worktree_identity": member.worktree_identity or str(root),
            "files": [_file_record(entry) for entry in files],
            "input_scope": sorted(scope),
        }
    )


def _file_record(entry: CapturedFile) -> dict[str, object]:
    return {
        "path": entry.path,
        "content_sha256": entry.content_sha256,
        "mode": entry.mode,
    }


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()
