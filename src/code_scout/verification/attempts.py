"""Sequential task-attempt lifecycle records and retained comparisons."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_scout.core.errors import ConfigError, StorageError
from code_scout.verification.checkpoints import (
    CheckpointInputScope,
    ContentAddressedBlobStore,
)
from code_scout.verification.contracts import (
    AttemptLifecycle,
    CapturedFile,
    CapturedMember,
    MemberRole,
    TaskAttempt,
    VerificationSession,
    WorkspaceCheckpoint,
    WorkspaceMember,
)
from code_scout.verification.policy import parse_verification_policy
from code_scout.verification.sessions import (
    _capture_stably,
    _recheck_members,
    _session_locks,
    policy_matches_session,
)


class AttemptLifecycleError(ConfigError):
    """A requested task transition is incompatible with the saved session."""


def begin_attempt(*, workspace_root: Path, session_id: str, label: str) -> TaskAttempt:
    """Capture the real current files as a new attempt's before checkpoint."""

    if not label or label.startswith("-"):
        raise AttemptLifecycleError("change begin requires a non-empty task label")
    workspace = workspace_root.resolve()
    session, initial, policy = _load_session(workspace, session_id)
    if _session_is_finished(workspace, session_id):
        raise AttemptLifecycleError("finished session rejects new task attempts")
    attempts = _attempt_records(workspace, session_id)
    if any(
        record["attempt"]["lifecycle"] == AttemptLifecycle.ACTIVE.value
        for record in attempts
    ):
        raise AttemptLifecycleError("session already has an active task attempt")
    prior_scope = _scope_from_checkpoints(
        [
            initial,
            *(
                _checkpoint_from_payload(
                    record["before_checkpoint"],
                    record["attempt"]["before_checkpoint_id"],
                )
                for record in attempts
            ),
        ]
    )
    with _session_locks(workspace, session.members):
        _recheck_members(session.members)
        if _session_is_finished(workspace, session_id):
            raise AttemptLifecycleError("finished session rejects new task attempts")
        if not policy_matches_session(session):
            raise AttemptLifecycleError(
                "session policy source changed since admission; start a new session"
            )
        checkpoint = _capture_stably(workspace, session.members, policy, prior_scope)
        _recheck_members(session.members)
        checkpoint = replace(checkpoint, id=f"checkpoint-{uuid.uuid4().hex}")
        attempt = TaskAttempt(
            id=f"change-{uuid.uuid4().hex}",
            label=label,
            session_id=session.id,
            before_checkpoint_id=checkpoint.id,
            previous_attempt_id=attempts[-1]["attempt"]["id"] if attempts else None,
        )
        payload = {
            "schema_version": 1,
            "attempt": _json_value(attempt),
            "before_checkpoint": _json_value(checkpoint),
            "comparisons": _comparisons(
                workspace, session, initial, checkpoint, checkpoint
            ),
            "created_at": datetime.now(UTC).isoformat(),
        }
        _publish_attempt(workspace, attempt.id, payload)
    return attempt


def abandon_attempt(*, workspace_root: Path, attempt_id: str) -> TaskAttempt:
    """Close an active attempt while retaining its captured bytes and edits."""

    workspace = workspace_root.resolve()
    path, payload = _load_attempt(workspace, attempt_id)
    attempt = _attempt_from_payload(payload["attempt"])
    if attempt.lifecycle is not AttemptLifecycle.ACTIVE:
        raise AttemptLifecycleError(f"task attempt is not active: {attempt_id}")
    payload["attempt"] = _json_value(
        replace(attempt, lifecycle=AttemptLifecycle.ABANDONED)
    )
    payload["abandoned_at"] = datetime.now(UTC).isoformat()
    _replace_json(path, payload)
    return replace(attempt, lifecycle=AttemptLifecycle.ABANDONED)


def _session_is_finished(workspace: Path, session_id: str) -> bool:
    path = workspace / ".code-scout/verification/sessions" / f"{session_id}.json"
    try:
        return json.loads(path.read_text()).get("completion_receipt") is not None
    except (OSError, json.JSONDecodeError) as error:
        raise AttemptLifecycleError(
            f"could not read verification session {session_id}"
        ) from error


def _load_session(
    workspace: Path, session_id: str
) -> tuple[VerificationSession, WorkspaceCheckpoint, Any]:
    path = workspace / ".code-scout/verification/sessions" / f"{session_id}.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AttemptLifecycleError(
            f"could not read verification session {session_id}"
        ) from error
    session_value = payload.get("session")
    if not isinstance(session_value, dict) or session_value.get("id") != session_id:
        raise AttemptLifecycleError(f"invalid verification session {session_id}")
    members = tuple(
        WorkspaceMember(
            id=item["id"],
            root=item["root"],
            git_common_directory=item["git_common_directory"],
            worktree_identity=item["worktree_identity"],
            role=MemberRole(item["role"]),
        )
        for item in session_value["members"]
    )
    session = VerificationSession(
        id=session_id,
        members=members,
        initial_checkpoint_id=session_value["initial_checkpoint_id"],
        frozen_policy_digest=session_value["frozen_policy_digest"],
        policy_path=session_value.get("policy_path", ""),
        protected_input_digest=session_value["protected_input_digest"],
        created_at=session_value["created_at"],
    )
    initial = _checkpoint_from_payload(
        payload["initial_checkpoint"], session.initial_checkpoint_id
    )
    try:
        profiles = parse_verification_policy(payload["frozen_policy"]).profiles
    except (TypeError, ValueError) as error:
        raise AttemptLifecycleError(
            "session has an unreadable frozen policy"
        ) from error
    matching = [
        profile
        for profile in profiles.values()
        if set(profile.repositories) == {m.id for m in members}
    ]
    if len(matching) != 1:
        raise AttemptLifecycleError(
            "session frozen policy has ambiguous member profile"
        )
    return session, initial, matching[0]


def _attempt_records(workspace: Path, session_id: str) -> list[dict[str, Any]]:
    root = workspace / ".code-scout/verification/attempts"
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(
        root.glob("change-*.json"), key=lambda item: item.stat().st_mtime_ns
    ):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise AttemptLifecycleError(
                f"could not read task attempt {path.stem}"
            ) from error
        if payload.get("attempt", {}).get("session_id") == session_id:
            records.append(payload)
    return records


def _load_attempt(workspace: Path, attempt_id: str) -> tuple[Path, dict[str, Any]]:
    path = workspace / ".code-scout/verification/attempts" / f"{attempt_id}.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AttemptLifecycleError(
            f"could not read task attempt {attempt_id}"
        ) from error
    if payload.get("attempt", {}).get("id") != attempt_id:
        raise AttemptLifecycleError(f"invalid task attempt {attempt_id}")
    return path, payload


def _checkpoint_from_payload(
    value: dict[str, Any], checkpoint_id: str
) -> WorkspaceCheckpoint:
    members = tuple(
        CapturedMember(
            id=member["id"],
            role=MemberRole(member["role"]),
            worktree_identity=member["worktree_identity"],
            files=tuple(CapturedFile(**entry) for entry in member["files"]),
            input_scope=tuple(member["input_scope"]),
            content_digest=member["content_digest"],
        )
        for member in value["members"]
    )
    return WorkspaceCheckpoint(
        id=value.get("id") or checkpoint_id,
        workspace_digest=value["workspace_digest"],
        member_digests=tuple(tuple(item) for item in value["member_digests"]),
        members=members,
        workspace_files=tuple(
            CapturedFile(**entry) for entry in value["workspace_files"]
        ),
        workspace_input_scope=tuple(value.get("workspace_input_scope", ())),
        input_scope_version=value["input_scope_version"],
    )


def _attempt_from_payload(value: dict[str, Any]) -> TaskAttempt:
    return TaskAttempt(
        id=value["id"],
        label=value["label"],
        session_id=value["session_id"],
        before_checkpoint_id=value["before_checkpoint_id"],
        previous_attempt_id=value.get("previous_attempt_id"),
        lifecycle=AttemptLifecycle(value["lifecycle"]),
    )


def _scope_from_checkpoints(
    checkpoints: list[WorkspaceCheckpoint],
) -> CheckpointInputScope:
    scope = CheckpointInputScope.empty()
    for checkpoint in checkpoints:
        scope = CheckpointInputScope(
            {
                **scope.member_paths,
                **{
                    m.id: frozenset(
                        set(scope.member_paths.get(m.id, ())) | set(m.input_scope)
                    )
                    for m in checkpoint.members
                },
            },
            frozenset(
                set(scope.workspace_paths)
                | set(
                    checkpoint.workspace_input_scope
                    or tuple(f.path for f in checkpoint.workspace_files)
                )
            ),
        )
    return scope


def _comparisons(
    workspace: Path,
    session: VerificationSession,
    initial: WorkspaceCheckpoint,
    before: WorkspaceCheckpoint,
    current: WorkspaceCheckpoint,
) -> dict[str, Any]:
    return {
        "task_window": _comparison("task-window", before, current),
        "cumulative_session": _comparison("cumulative-session", initial, current),
        "recorded_head": [
            _head_comparison(workspace, member, current) for member in session.members
        ],
    }


def _comparison(
    label: str, before: WorkspaceCheckpoint, after: WorkspaceCheckpoint
) -> dict[str, Any]:
    return {
        "label": label,
        "before_checkpoint_id": before.id,
        "after_checkpoint_id": after.id,
        "members": [
            _file_delta(a.id, a.files, b.files)
            for a, b in zip(before.members, after.members, strict=True)
        ],
    }


def _file_delta(
    member_id: str, before: tuple[CapturedFile, ...], after: tuple[CapturedFile, ...]
) -> dict[str, Any]:
    left, right = (
        {item.path: item for item in before},
        {item.path: item for item in after},
    )
    changes = []
    for path in sorted(left.keys() | right.keys()):
        if left.get(path) == right.get(path):
            continue
        changes.append(
            {
                "path": path,
                "kind": "added"
                if path not in left
                else "deleted"
                if path not in right
                else "modified",
                "binary": True,
                "before_source": _source(left.get(path)),
                "after_source": _source(right.get(path)),
            }
        )
    return {"member_id": member_id, "deltas": changes}


def _source(entry: CapturedFile | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    digest = entry.content_sha256
    return {
        "sha256": digest,
        "blob": f"blobs/{digest[:2]}/{digest[2:]}",
        "mode": entry.mode,
        "size_bytes": entry.size_bytes,
    }


def _head_comparison(
    workspace: Path, member: WorkspaceMember, current: WorkspaceCheckpoint
) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=member.root, capture_output=True, check=False
    )
    head = result.stdout.decode().strip() if result.returncode == 0 else None
    captured = next(item for item in current.members if item.id == member.id)
    before = _head_files(
        Path(member.root),
        head,
        ContentAddressedBlobStore(workspace / ".code-scout/verification/blobs"),
    )
    return {
        "label": "recorded-HEAD",
        "member_id": member.id,
        "head": head,
        "comparison": _file_delta(member.id, before, captured.files),
    }


def _head_files(
    root: Path, head: str | None, store: ContentAddressedBlobStore
) -> tuple[CapturedFile, ...]:
    if head is None:
        return ()
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "-z", head], cwd=root, capture_output=True, check=False
    )
    if listing.returncode:
        raise AttemptLifecycleError("could not read recorded Git HEAD")
    entries: list[CapturedFile] = []
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        metadata, path = raw.split(b"\t", 1)
        mode, kind, _object = metadata.decode("ascii").split()
        if kind != "blob":
            continue
        source = subprocess.run(
            ["git", "show", f"{head}:{path.decode('utf-8')}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if source.returncode:
            raise AttemptLifecycleError("could not retain recorded Git HEAD source")
        entries.append(
            CapturedFile(
                path=path.decode("utf-8"),
                content_sha256=store.put(source.stdout),
                mode=int(mode, 8),
                size_bytes=len(source.stdout),
            )
        )
    return tuple(sorted(entries, key=lambda item: item.path))


def _publish_attempt(workspace: Path, attempt_id: str, payload: dict[str, Any]) -> None:
    root = workspace / ".code-scout/verification/attempts"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = root / f"{attempt_id}.json"
    if target.exists():
        raise StorageError("task attempt ID already exists")
    _replace_json(target, payload, must_not_exist=True)


def _replace_json(
    path: Path, payload: dict[str, Any], *, must_not_exist: bool = False
) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".attempt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        if must_not_exist and path.exists():
            raise StorageError("task attempt ID already exists")
        os.replace(temporary, path)
    except OSError as error:
        Path(temporary).unlink(missing_ok=True)
        raise StorageError(f"could not publish task attempt: {error}") from error


def _json_value(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, MemberRole | AttemptLifecycle):
        return value.value
    return value
