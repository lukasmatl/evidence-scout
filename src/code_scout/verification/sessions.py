"""Admission of explicit, stable multi-repository verification sessions."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from code_scout.core.errors import ConfigError, StorageError
from code_scout.verification.checkpoints import (
    CheckpointCaptureError,
    CheckpointInputScope,
    ContentAddressedBlobStore,
    capture_workspace_checkpoint,
)
from code_scout.verification.contracts import (
    MemberRole,
    VerificationSession,
    WorkspaceCheckpoint,
    WorkspaceMember,
)
from code_scout.verification.policy import (
    VerificationProfile,
    load_verification_policy,
    validate_profile_members,
)

_CAPTURE_ATTEMPTS = 3


class SessionAdmissionError(ConfigError):
    """The selected workspace cannot safely become a verification session."""


def begin_session(
    *,
    workspace_root: Path,
    repositories: Sequence[tuple[str, str, MemberRole]],
    policy_path: Path,
    profile_id: str,
) -> VerificationSession:
    """Freeze a selected dirty workspace, publishing a session only when stable."""

    workspace = workspace_root.resolve()
    if not workspace.is_dir():
        raise SessionAdmissionError("workspace is not an accessible directory")
    members = _members(workspace, repositories)
    policy_source = _read_policy(policy_path)
    policy = load_verification_policy(policy_path)
    try:
        profile = policy.profiles[profile_id]
    except KeyError as error:
        raise SessionAdmissionError(
            f"unknown verification profile: {profile_id}"
        ) from error
    try:
        validate_profile_members(profile, members, workspace_root=workspace)
    except ConfigError as error:
        raise SessionAdmissionError(error.message, error.hint) from error
    _require_literal_protected_inputs(workspace, members, profile)

    with _session_locks(workspace, members):
        _recheck_members(members)
        checkpoint = _capture_stably(workspace, members, profile)
        _recheck_members(members)
        session = VerificationSession(
            id=f"session-{uuid.uuid4().hex}",
            members=members,
            initial_checkpoint_id=f"checkpoint-{uuid.uuid4().hex}",
            frozen_policy_digest=hashlib.sha256(policy_source).hexdigest(),
            policy_path=str(policy_path.resolve()),
            protected_input_digest=_protected_digest(checkpoint, profile),
            created_at=datetime.now(UTC).isoformat(),
        )
        _publish_session(workspace, session, checkpoint, policy_source)
    return session


def _read_policy(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise SessionAdmissionError(f"could not read policy {path}: {error}") from error


def policy_matches_session(session: VerificationSession) -> bool:
    """Return whether the admitted policy source still has its frozen bytes."""

    if not session.policy_path:
        return False
    try:
        source = Path(session.policy_path).read_bytes()
    except OSError:
        return False
    return hashlib.sha256(source).hexdigest() == session.frozen_policy_digest


def _members(
    workspace: Path,
    repositories: Sequence[tuple[str, str, MemberRole]],
) -> tuple[WorkspaceMember, ...]:
    if not repositories:
        raise SessionAdmissionError("session begin requires at least one repository")
    members: list[WorkspaceMember] = []
    ids: set[str] = set()
    roots: set[Path] = set()
    for member_id, raw_root, role in repositories:
        if member_id in ids:
            raise SessionAdmissionError(f"duplicate member id: {member_id}")
        root = (workspace / raw_root).resolve()
        if root in roots:
            raise SessionAdmissionError(f"duplicate member root: {root}")
        try:
            root.relative_to(workspace)
        except ValueError as error:
            raise SessionAdmissionError(
                f"member root escapes workspace: {raw_root}"
            ) from error
        common, git_dir = _git_identity(root)
        ids.add(member_id)
        roots.add(root)
        members.append(
            WorkspaceMember(
                id=member_id,
                root=str(root),
                git_common_directory=str(common),
                worktree_identity=str(git_dir),
                role=role,
            )
        )
    return tuple(sorted(members, key=lambda member: member.id))


def _git_identity(root: Path) -> tuple[Path, Path]:
    if not root.is_dir():
        raise SessionAdmissionError(
            f"member root is not an accessible directory: {root}"
        )
    result = _git(
        root, ["rev-parse", "--show-toplevel", "--git-common-dir", "--git-dir"]
    )
    if result.returncode:
        raise SessionAdmissionError(f"member is not an accessible Git worktree: {root}")
    values = result.stdout.decode("utf-8", "replace").splitlines()
    if len(values) != 3 or Path(values[0]).resolve() != root:
        raise SessionAdmissionError(f"member root is not its Git worktree root: {root}")
    return ((root / values[1]).resolve(), (root / values[2]).resolve())


def _recheck_members(members: Sequence[WorkspaceMember]) -> None:
    for member in members:
        root = Path(member.root)
        common, git_dir = _git_identity(root)
        if (
            str(common) != member.git_common_directory
            or str(git_dir) != member.worktree_identity
        ):
            raise SessionAdmissionError(
                f"member identity changed during admission: {member.id}"
            )


def _capture_stably(
    workspace: Path,
    members: tuple[WorkspaceMember, ...],
    profile: VerificationProfile,
    previous_scope: CheckpointInputScope | None = None,
    blob_root: Path | None = None,
) -> WorkspaceCheckpoint:
    store = ContentAddressedBlobStore(
        blob_root or workspace / ".code-scout/verification/blobs"
    )
    for _attempt in range(_CAPTURE_ATTEMPTS):
        try:
            first = capture_workspace_checkpoint(
                members=members,
                profile=profile,
                workspace_root=workspace,
                blob_store=store,
                previous_scope=previous_scope,
                nested_member_roots=tuple(Path(member.root) for member in members),
            )
            _recheck_members(members)
            second = capture_workspace_checkpoint(
                members=members,
                profile=profile,
                workspace_root=workspace,
                blob_store=store,
                previous_scope=CheckpointInputScope.from_checkpoint(first),
                nested_member_roots=tuple(Path(member.root) for member in members),
            )
            _recheck_members(members)
        except CheckpointCaptureError as error:
            raise SessionAdmissionError(str(error)) from error
        if first.workspace_digest == second.workspace_digest:
            return second
    raise SessionAdmissionError("UNSTABLE_WORKSPACE: source changed during capture")


def _require_literal_protected_inputs(
    workspace: Path,
    members: Sequence[WorkspaceMember],
    profile: VerificationProfile,
) -> None:
    for member in members:
        for pattern in profile.repositories[member.id].protected_paths:
            _require_literal_input(Path(member.root), pattern)
    for pattern in profile.workspace_protected_paths:
        _require_literal_input(workspace, pattern)


def _require_literal_input(root: Path, pattern: str) -> None:
    if any(character in pattern for character in "*?"):
        return
    candidate = root / pattern
    if not candidate.is_file():
        raise SessionAdmissionError(f"declared protected input is missing: {pattern}")


@contextmanager
def _session_locks(
    workspace: Path, members: Sequence[WorkspaceMember]
) -> Iterator[None]:
    lock_paths = {
        Path(member.worktree_identity) / "code-scout-verification.lock"
        for member in members
    }
    lock_paths.add(workspace / ".code-scout/verification/workspace.lock")
    descriptors: list[int] = []
    try:
        for path in sorted(lock_paths, key=lambda item: str(item)):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _protected_digest(
    checkpoint: WorkspaceCheckpoint, profile: VerificationProfile
) -> str:
    records: list[dict[str, object]] = []
    for member in checkpoint.members:
        patterns = profile.repositories[member.id].protected_paths
        for entry in member.files:
            if member.role is MemberRole.REFERENCE or _matches(entry.path, patterns):
                records.append({"member": member.id, **asdict(entry)})
    for entry in checkpoint.workspace_files:
        if _matches(entry.path, profile.workspace_protected_paths):
            records.append({"workspace": True, **asdict(entry)})
    return _digest(records)


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(Path(path).match(pattern) for pattern in patterns)


def _publish_session(
    workspace: Path,
    session: VerificationSession,
    checkpoint: WorkspaceCheckpoint,
    policy_source: bytes,
) -> None:
    root = workspace / ".code-scout/verification/sessions"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / f"{session.id}.json"
    if target.exists():
        raise StorageError("session ID already exists")
    payload = {
        "schema_version": 1,
        "session": _json_value(session),
        "initial_checkpoint": _json_value(checkpoint),
        "frozen_policy": policy_source.decode("utf-8"),
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".session-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise StorageError(
            f"could not publish verification session: {error}"
        ) from error


def _json_value(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, MemberRole):
        return value.value
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *arguments], cwd=root, capture_output=True, check=False
        )
    except OSError as error:
        raise SessionAdmissionError(f"Git could not execute: {error}") from error
