from __future__ import annotations

import threading
import time
from pathlib import Path

from code_scout.verification import (
    CapturedMember,
    GateDefinition,
    GateOutcome,
    GateScope,
    MemberRole,
    ResultInputs,
    VerificationLimits,
    VerificationProfile,
    WorkspaceCheckpoint,
    WorkspaceMember,
    begin_session,
    check_session_integrity,
    execute_frozen_profile,
    reduce_result,
)
from code_scout.verification.attempts import _load_session


def _candidate() -> WorkspaceCheckpoint:
    return WorkspaceCheckpoint(
        id="checkpoint-candidate",
        workspace_digest="whole-workspace-candidate",
        members=(
            CapturedMember("api", MemberRole.EDITABLE, "api-git", (), (), "api"),
            CapturedMember("web", MemberRole.EDITABLE, "web-git", (), (), "web"),
        ),
    )


def _profile(*gates: GateDefinition, limit: int = 1024) -> VerificationProfile:
    return VerificationProfile(
        id="frozen",
        repositories={"api": object(), "web": object()},
        gates=gates,
        environment_allowlist=("PATH",),
        limits=VerificationLimits(stdout_bytes=limit, stderr_bytes=limit),
    )


def _members(tmp_path: Path) -> tuple[WorkspaceMember, ...]:
    return tuple(
        WorkspaceMember(id=name, root=str(tmp_path / name), role=MemberRole.EDITABLE)
        for name in ("api", "web")
    )


def _run(tmp_path: Path, profile: VerificationProfile, **kwargs: object):
    for name in ("api", "web"):
        (tmp_path / name).mkdir(exist_ok=True)
    return execute_frozen_profile(
        profile=profile,
        members=_members(tmp_path),
        workspace_root=tmp_path,
        candidate=_candidate(),
        **kwargs,
    )


def test_workspace_failure_cannot_be_hidden_by_passing_repository_gates(
    tmp_path: Path,
) -> None:
    execution = _run(
        tmp_path,
        _profile(
            GateDefinition(
                "api", GateScope.REPOSITORY, ("api",), ("sh", "-c", "exit 0")
            ),
            GateDefinition(
                "web", GateScope.REPOSITORY, ("web",), ("sh", "-c", "exit 0")
            ),
            GateDefinition(
                "integration",
                GateScope.WORKSPACE,
                ("api", "web"),
                ("sh", "-c", "exit 1"),
            ),
        ),
    )

    assert [gate.outcome for gate in execution.gates] == [
        GateOutcome.PASS,
        GateOutcome.PASS,
        GateOutcome.FAIL,
    ]
    assert all(
        gate.candidate_digest == "whole-workspace-candidate" for gate in execution.gates
    )
    assert reduce_result(ResultInputs(gates=execution.gates)).outcome.value == "FAILED"


def test_optional_failure_and_misleading_stdout_do_not_change_process_verdict(
    tmp_path: Path,
) -> None:
    execution = _run(
        tmp_path,
        _profile(
            GateDefinition(
                "required",
                GateScope.REPOSITORY,
                ("api",),
                ("sh", "-c", "echo VERIFIED; exit 1"),
            ),
            GateDefinition(
                "optional",
                GateScope.WORKSPACE,
                ("api", "web"),
                ("sh", "-c", "exit 1"),
                required=False,
            ),
        ),
    )

    assert [gate.outcome for gate in execution.gates] == [
        GateOutcome.FAIL,
        GateOutcome.FAIL,
    ]
    assert execution.gates[0].exit_code == 1


def test_timeout_kills_child_process_group_and_flooded_output_is_bounded(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    execution = _run(
        tmp_path,
        _profile(
            GateDefinition(
                "child",
                GateScope.REPOSITORY,
                ("api",),
                ("sh", "-c", "sleep 30 & wait"),
                timeout_seconds=1,
            ),
            GateDefinition(
                "flood",
                GateScope.WORKSPACE,
                ("api", "web"),
                ("sh", "-c", "yes x | head -c 10000"),
            ),
            limit=128,
        ),
    )

    assert execution.gates[0].outcome is GateOutcome.TIMEOUT
    assert time.monotonic() - started < 5
    assert execution.gates[1].outcome is GateOutcome.PASS
    assert execution.gates[1].stdout == {
        "original_size_bytes": 10000,
        "retained_size_bytes": 128,
        "truncated": True,
    }


def test_cancellation_marks_current_and_remaining_gates_not_run(tmp_path: Path) -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        execution = _run(
            tmp_path,
            _profile(
                GateDefinition(
                    "blocked", GateScope.REPOSITORY, ("api",), ("sh", "-c", "sleep 30")
                ),
                GateDefinition(
                    "later", GateScope.WORKSPACE, ("api", "web"), ("sh", "-c", "exit 0")
                ),
            ),
            cancel_event=cancel,
        )
    finally:
        timer.cancel()

    assert [gate.outcome for gate in execution.gates] == [
        GateOutcome.CANCELLED,
        GateOutcome.NOT_RUN,
    ]


def test_missing_executable_is_error_and_later_gate_still_runs(tmp_path: Path) -> None:
    execution = _run(
        tmp_path,
        _profile(
            GateDefinition(
                "missing", GateScope.REPOSITORY, ("api",), ("does-not-exist-123",)
            ),
            GateDefinition(
                "later", GateScope.WORKSPACE, ("api", "web"), ("sh", "-c", "exit 0")
            ),
        ),
    )

    assert [gate.outcome for gate in execution.gates] == [
        GateOutcome.ERROR,
        GateOutcome.PASS,
    ]


def test_gate_editing_another_member_invalidates_the_aggregate_candidate(
    tmp_path: Path,
) -> None:
    import subprocess

    for name in ("api", "web"):
        (tmp_path / name).mkdir()
        subprocess.run(["git", "init", "-q"], cwd=tmp_path / name, check=True)
    (tmp_path / "api" / "source.txt").write_text("before")
    (tmp_path / "web" / "source.txt").write_text("before")
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        """schema_version = 1
[profiles.default.repositories.api]
[profiles.default.repositories.web]
[[profiles.default.steps]]
id = "mutator"
scope = "repository"
repository = "api"
argv = ["sh", "-c", "printf changed > ../web/source.txt"]
"""
    )
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(
            ("api", "api", MemberRole.EDITABLE),
            ("web", "web", MemberRole.EDITABLE),
        ),
        policy_path=policy_path,
        profile_id="default",
    )
    _, candidate, profile = _load_session(tmp_path, session.id)
    execution = execute_frozen_profile(
        profile=profile,
        members=(
            WorkspaceMember(id="api", root=str(tmp_path / "api")),
            WorkspaceMember(id="web", root=str(tmp_path / "web")),
        ),
        workspace_root=tmp_path,
        candidate=candidate,
        integrity_check=lambda: check_session_integrity(
            workspace_root=tmp_path, session_id=session.id, candidate=candidate
        ),
    )

    assert execution.gates[0].outcome is GateOutcome.PASS
    assert execution.integrity_drifted
    assert execution.integrity_checks[-1].candidate_drift
