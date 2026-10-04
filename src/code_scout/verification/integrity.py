"""Session-wide live-workspace integrity checks for frozen verification runs."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from code_scout.verification.attempts import _load_session, _scope_from_checkpoints
from code_scout.verification.contracts import CapturedFile, WorkspaceCheckpoint
from code_scout.verification.sessions import (
    _capture_stably,
    _matches,
    _recheck_members,
    _session_locks,
    policy_matches_session,
)


@dataclass(frozen=True)
class IntegrityCheck:
    """A live comparison with admission controls and one run candidate."""

    current: WorkspaceCheckpoint
    policy_drift: bool
    protected_input_drift: bool
    candidate_drift: bool

    @property
    def drifted(self) -> bool:
        return self.policy_drift or self.protected_input_drift or self.candidate_drift


def check_session_integrity(
    *,
    workspace_root: Path,
    session_id: str,
    candidate: WorkspaceCheckpoint,
    already_locked: bool = False,
) -> IntegrityCheck:
    """Capture current state and reject controls weakened after session admission.

    Reference members are compared in full to admission.  Protected paths use
    the frozen patterns on both inventories, so a newly-created matching path
    is drift rather than an opportunity to silently widen the accepted set.
    The complete aggregate inventory must also still equal ``candidate``.
    """

    workspace = workspace_root.resolve()
    session, initial, profile = _load_session(workspace, session_id)
    lock = (
        nullcontext() if already_locked else _session_locks(workspace, session.members)
    )
    with lock:
        _recheck_members(session.members)
        current = _capture_stably(
            workspace,
            session.members,
            profile,
            _scope_from_checkpoints([initial, candidate]),
        )
        _recheck_members(session.members)
        return IntegrityCheck(
            current=current,
            policy_drift=not policy_matches_session(session),
            protected_input_drift=_protected_drift(initial, current, profile),
            candidate_drift=current.workspace_digest != candidate.workspace_digest,
        )


def _protected_drift(
    admission: WorkspaceCheckpoint, current: WorkspaceCheckpoint, profile: object
) -> bool:
    admission_members = {member.id: member for member in admission.members}
    current_members = {member.id: member for member in current.members}
    if admission_members.keys() != current_members.keys():
        return True
    for member_id, before in admission_members.items():
        after = current_members[member_id]
        if before.role.value == "reference":
            if before.files != after.files:
                return True
            continue
        patterns = profile.repositories[member_id].protected_paths
        if _protected_entries(before.files, patterns) != _protected_entries(
            after.files, patterns
        ):
            return True
    patterns = profile.workspace_protected_paths
    return _protected_entries(
        admission.workspace_files, patterns
    ) != _protected_entries(current.workspace_files, patterns)


def _protected_entries(
    entries: tuple[CapturedFile, ...], patterns: tuple[str, ...]
) -> tuple[CapturedFile, ...]:
    return tuple(entry for entry in entries if _matches(entry.path, patterns))
