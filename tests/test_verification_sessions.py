from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from code_scout.commands import command, run_command
from code_scout.verification import (
    AttemptLifecycleError,
    MemberRole,
    RunEvidenceError,
    SessionAdmissionError,
    VerificationServiceError,
    abandon_attempt,
    begin_attempt,
    begin_session,
    check_session_integrity,
    finish_attempt,
    finish_session,
    show_session,
    verify,
)
from code_scout.verification import sessions as session_module
from code_scout.verification.attempts import _load_session


def test_session_admits_dirty_sibling_and_nested_members_with_frozen_inputs(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    nested = _repo(api / "tools")
    schemas = _repo(tmp_path / "schemas")
    _write(api / "dirty.txt", "dirty")
    _write(nested / "tool.txt", "tool")
    _write(schemas / "schema.txt", "schema")
    policy = _policy(
        tmp_path / "policy.toml",
        repositories=("api", "tools", "schemas"),
        protected=True,
    )

    session = begin_session(
        workspace_root=tmp_path,
        repositories=(
            ("api", "api", MemberRole.EDITABLE),
            ("tools", "api/tools", MemberRole.EDITABLE),
            ("schemas", "schemas", MemberRole.REFERENCE),
        ),
        policy_path=policy,
        profile_id="default",
    )

    saved = json.loads(
        (
            tmp_path / ".code-scout/verification/sessions" / f"{session.id}.json"
        ).read_text()
    )
    checkpoint = saved["initial_checkpoint"]
    api_files = {item["path"] for item in checkpoint["members"][0]["files"]}
    tools_files = {item["path"] for item in checkpoint["members"][2]["files"]}
    assert api_files == {"dirty.txt"}
    assert tools_files == {"tool.txt"}
    assert {member.role for member in session.members} == {
        MemberRole.EDITABLE,
        MemberRole.REFERENCE,
    }
    assert session.protected_input_digest
    assert session.frozen_policy_digest


def test_session_rejects_undeclared_nested_repository_and_gitlink(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    _repo(api / "nested")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    with pytest.raises(SessionAdmissionError, match="undeclared nested"):
        begin_session(
            workspace_root=tmp_path,
            repositories=(("api", "api", MemberRole.EDITABLE),),
            policy_path=policy,
            profile_id="default",
        )

    _git(api, "add", "nested")
    with pytest.raises(SessionAdmissionError, match="gitlink"):
        begin_session(
            workspace_root=tmp_path,
            repositories=(("api", "api", MemberRole.EDITABLE),),
            policy_path=policy,
            profile_id="default",
        )


def test_same_worktree_identity_is_shared_across_workspace_roots(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    policy = _policy(tmp_path / "policy.toml", repositories=("repo",))
    outer = begin_session(
        workspace_root=tmp_path,
        repositories=(("repo", "repo", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    inner = begin_session(
        workspace_root=repo,
        repositories=(("repo", ".", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )

    assert outer.members[0].worktree_identity == inner.members[0].worktree_identity
    assert Path(
        outer.members[0].worktree_identity, "code-scout-verification.lock"
    ).is_file()


def test_change_session_begin_cli_returns_frozen_session_id(tmp_path: Path) -> None:
    _repo(tmp_path / "api")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))

    output = run_command(
        command(("change", "session", "begin")),
        [
            "change",
            "session",
            "begin",
            "--workspace",
            str(tmp_path),
            "--repo",
            "api=api",
            "--policy",
            policy.name,
            "--profile",
            "default",
            "--format",
            "json",
        ],
    )

    payload = json.loads(output)
    assert payload["session_id"].startswith("session-")
    assert payload["members"] == [
        {
            "id": "api",
            "role": "editable",
            "root": str((tmp_path / "api").resolve()),
        }
    ]


def test_sequential_overlapping_attempts_retain_current_baselines_and_comparisons(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    _write(api / "shared.txt", "head")
    _git(api, "add", "shared.txt")
    _git(api, "commit", "-qm", "content")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    first = begin_attempt(
        workspace_root=tmp_path, session_id=session.id, label="repeat"
    )
    _write(api / "shared.txt", "task-a")
    with pytest.raises(AttemptLifecycleError, match="active"):
        begin_attempt(workspace_root=tmp_path, session_id=session.id, label="blocked")
    abandoned = abandon_attempt(workspace_root=tmp_path, attempt_id=first.id)
    assert abandoned.lifecycle.value == "abandoned"
    assert (api / "shared.txt").read_text() == "task-a"
    _write(api / "shared.txt", "manual-between-tasks")
    second = begin_attempt(
        workspace_root=tmp_path, session_id=session.id, label="repeat"
    )
    assert second.previous_attempt_id == first.id
    assert second.label == first.label
    saved = json.loads(
        (
            tmp_path / ".code-scout/verification/attempts" / f"{second.id}.json"
        ).read_text()
    )
    assert saved["comparisons"]["task_window"]["label"] == "task-window"
    assert saved["comparisons"]["cumulative_session"]["label"] == "cumulative-session"
    head = saved["comparisons"]["recorded_head"][0]
    assert head["label"] == "recorded-HEAD"
    delta = head["comparison"]["deltas"][0]
    assert delta["before_source"]["sha256"]
    assert delta["after_source"]["sha256"]
    assert delta["binary"] is True


def test_inaccessible_member_during_capture_leaves_no_session_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path / "api")
    schemas = _repo(tmp_path / "schemas")
    policy = _policy(tmp_path / "policy.toml")
    original = session_module.capture_workspace_checkpoint
    calls = 0

    def disappear(**kwargs: object):
        nonlocal calls
        calls += 1
        result = original(**kwargs)
        if calls == 1:
            schemas.rename(tmp_path / "missing-schemas")
        return result

    monkeypatch.setattr(session_module, "capture_workspace_checkpoint", disappear)
    with pytest.raises(SessionAdmissionError, match="accessible"):
        begin_session(
            workspace_root=tmp_path,
            repositories=(
                ("api", "api", MemberRole.EDITABLE),
                ("schemas", "schemas", MemberRole.REFERENCE),
            ),
            policy_path=policy,
            profile_id="default",
        )
    assert not list((tmp_path / ".code-scout/verification/sessions").glob("*.json"))


def test_multi_repository_capture_retries_after_source_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _repo(tmp_path / "api")
    _repo(tmp_path / "schemas")
    _write(api / "value.txt", "before")
    policy = _policy(tmp_path / "policy.toml")
    original = session_module.capture_workspace_checkpoint
    calls = 0

    def edit_once(**kwargs: object):
        nonlocal calls
        calls += 1
        result = original(**kwargs)
        if calls == 1:
            _write(api / "value.txt", "after")
        return result

    monkeypatch.setattr(session_module, "capture_workspace_checkpoint", edit_once)
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(
            ("api", "api", MemberRole.EDITABLE),
            ("schemas", "schemas", MemberRole.REFERENCE),
        ),
        policy_path=policy,
        profile_id="default",
    )

    assert calls >= 4
    payload = json.loads(
        (
            tmp_path / ".code-scout/verification/sessions" / f"{session.id}.json"
        ).read_text()
    )
    digest = next(
        entry["content_sha256"]
        for member in payload["initial_checkpoint"]["members"]
        if member["id"] == "api"
        for entry in member["files"]
        if entry["path"] == "value.txt"
    )
    assert (
        tmp_path / ".code-scout/verification/blobs" / digest[:2] / digest[2:]
    ).read_bytes() == b"after"


def test_changed_gate_policy_cannot_be_legitimized_by_a_later_attempt(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    first = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    policy.write_text(policy.read_text().replace('argv = ["true"]', 'argv = ["false"]'))
    abandon_attempt(workspace_root=tmp_path, attempt_id=first.id)

    with pytest.raises(AttemptLifecycleError, match="policy source changed"):
        begin_attempt(workspace_root=tmp_path, session_id=session.id, label="B")
    assert api.is_dir()


def test_integrity_rechecks_reference_hidden_and_new_protected_inputs(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    reference = _repo(tmp_path / "reference")
    _write(api / "gate.sh", "original")
    _write(reference / "schema.txt", "original")
    policy = _integrity_policy(tmp_path / "policy.toml")
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(
            ("api", "api", MemberRole.EDITABLE),
            ("reference", "reference", MemberRole.REFERENCE),
        ),
        policy_path=policy,
        profile_id="default",
    )
    _, candidate, _ = _load_session(tmp_path, session.id)
    assert not check_session_integrity(
        workspace_root=tmp_path, session_id=session.id, candidate=candidate
    ).drifted

    _git(api, "add", "gate.sh")
    _git(api, "update-index", "--assume-unchanged", "gate.sh")
    _write(api / "gate.sh", "hidden edit")
    _write(api / "gate-added.sh", "new protected input")
    _write(reference / "schema.txt", "reference drift")
    observed = check_session_integrity(
        workspace_root=tmp_path, session_id=session.id, candidate=candidate
    )

    assert observed.protected_input_drift
    assert observed.candidate_drift


def test_integrity_detects_deleted_protected_helper(tmp_path: Path) -> None:
    api = _repo(tmp_path / "api")
    _write(api / "helper.sh", "helper")
    policy = _integrity_policy(tmp_path / "policy.toml", helper=True, reference=False)
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    _, candidate, _ = _load_session(tmp_path, session.id)
    (api / "helper.sh").unlink()

    assert check_session_integrity(
        workspace_root=tmp_path, session_id=session.id, candidate=candidate
    ).protected_input_drift


def test_integrity_detects_workspace_gate_helper_drift(tmp_path: Path) -> None:
    _repo(tmp_path / "api")
    workspace_gate = tmp_path / "verify.sh"
    _write(workspace_gate, "original")
    policy = tmp_path / "policy.toml"
    policy.write_text(
        """schema_version = 1
[profiles.default]
workspace_protected_paths = ["verify.sh"]
[profiles.default.repositories.api]
[[profiles.default.steps]]
id = "check"
scope = "repository"
repository = "api"
argv = ["true"]
"""
    )
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    _, candidate, _ = _load_session(tmp_path, session.id)
    _write(workspace_gate, "changed")

    assert check_session_integrity(
        workspace_root=tmp_path, session_id=session.id, candidate=candidate
    ).protected_input_drift


def test_finish_retains_historical_receipt_for_overlapping_tasks(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    _write(api / "shared.txt", "before")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    first = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    _write(api / "shared.txt", "first")
    run = verify(workspace_root=tmp_path, session_id=session.id, attempt_id=first.id)

    completed = finish_attempt(
        workspace_root=tmp_path, attempt_id=first.id, run_id=run["run_id"]
    )
    assert completed["historical"] is True
    assert completed["current_applicability"]["status"] == "matches"
    assert (
        json.loads(
            (
                tmp_path / ".code-scout/verification/attempts" / f"{first.id}.json"
            ).read_text()
        )["attempt"]["lifecycle"]
        == "completed"
    )

    second = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="B")
    _write(api / "shared.txt", "second")
    retried = finish_attempt(
        workspace_root=tmp_path, attempt_id=first.id, run_id=run["run_id"]
    )
    assert retried["receipt"] == completed["receipt"]
    assert retried["current_applicability"]["status"] == "different"
    assert second.previous_attempt_id == first.id


def test_finish_rejects_wrong_or_incomplete_or_drifted_evidence(tmp_path: Path) -> None:
    api = _repo(tmp_path / "api")
    _write(api / "value.txt", "before")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    attempt = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    _write(api / "value.txt", "candidate")
    run = verify(workspace_root=tmp_path, session_id=session.id, attempt_id=attempt.id)

    with pytest.raises(VerificationServiceError, match="could not read"):
        finish_attempt(
            workspace_root=tmp_path, attempt_id=attempt.id, run_id="run-missing"
        )
    _write(api / "value.txt", "changed")
    with pytest.raises(VerificationServiceError, match="no longer matches"):
        finish_attempt(
            workspace_root=tmp_path, attempt_id=attempt.id, run_id=run["run_id"]
        )
    _write(api / "value.txt", "candidate")
    policy.write_text(policy.read_text().replace('argv = ["true"]', 'argv = ["false"]'))
    with pytest.raises(VerificationServiceError, match="no longer matches"):
        finish_attempt(
            workspace_root=tmp_path, attempt_id=attempt.id, run_id=run["run_id"]
        )


def test_finish_rejects_missing_evidence(tmp_path: Path) -> None:
    _repo(tmp_path / "api")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    attempt = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    run = verify(workspace_root=tmp_path, session_id=session.id, attempt_id=attempt.id)
    run_root = tmp_path / ".code-scout/verification/runs" / run["run_id"]
    (run_root / "candidate.json").unlink()
    with pytest.raises(RunEvidenceError, match="missing artifact"):
        finish_attempt(
            workspace_root=tmp_path, attempt_id=attempt.id, run_id=run["run_id"]
        )


def test_finish_rechecks_the_recorded_worktree(tmp_path: Path) -> None:
    api = _repo(tmp_path / "api")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    attempt = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    run = verify(workspace_root=tmp_path, session_id=session.id, attempt_id=attempt.id)
    api.rename(tmp_path / "moved-api")

    with pytest.raises(SessionAdmissionError, match="accessible"):
        finish_attempt(
            workspace_root=tmp_path, attempt_id=attempt.id, run_id=run["run_id"]
        )


def test_session_finish_requires_a_fresh_aggregate_run_after_overlapping_work(
    tmp_path: Path,
) -> None:
    api = _repo(tmp_path / "api")
    _write(api / "shared.txt", "before")
    policy = _policy(tmp_path / "policy.toml", repositories=("api",))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    first = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    _write(api / "shared.txt", "first")
    first_run = verify(
        workspace_root=tmp_path, session_id=session.id, attempt_id=first.id
    )
    finish_attempt(
        workspace_root=tmp_path, attempt_id=first.id, run_id=first_run["run_id"]
    )
    stale_final_run = verify(workspace_root=tmp_path, session_id=session.id)

    second = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="B")
    _write(api / "shared.txt", "regression")
    second_run = verify(
        workspace_root=tmp_path, session_id=session.id, attempt_id=second.id
    )
    finish_attempt(
        workspace_root=tmp_path, attempt_id=second.id, run_id=second_run["run_id"]
    )
    with pytest.raises(VerificationServiceError, match="fresh verified session run"):
        finish_session(
            workspace_root=tmp_path,
            session_id=session.id,
            run_id=stale_final_run["run_id"],
        )

    final_run = verify(workspace_root=tmp_path, session_id=session.id)
    completed = finish_session(
        workspace_root=tmp_path, session_id=session.id, run_id=final_run["run_id"]
    )
    assert completed["current_applicability"]["status"] == "matches"
    with pytest.raises(AttemptLifecycleError, match="finished session"):
        begin_attempt(workspace_root=tmp_path, session_id=session.id, label="blocked")
    with pytest.raises(VerificationServiceError, match="finished session"):
        verify(workspace_root=tmp_path, session_id=session.id)
    _write(api / "shared.txt", "later-dirty-work")
    next_session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    assert next_session.id != session.id
    assert (
        show_session(workspace_root=tmp_path, session_id=session.id)[
            "completion_receipt"
        ]["run_id"]
        == final_run["run_id"]
    )


def test_session_finish_rejects_task_runs_and_final_integration_failure(
    tmp_path: Path,
) -> None:
    _repo(tmp_path / "api")
    policy = tmp_path / "policy.toml"
    policy.write_text(
        """schema_version = 1
[profiles.default]
[profiles.default.repositories.api]
[[profiles.default.steps]]
id = "api-check"
scope = "repository"
repository = "api"
argv = ["true"]
[[profiles.default.steps]]
id = "integration"
scope = "workspace"
repositories = ["api"]
argv = ["false"]
"""
    )
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(("api", "api", MemberRole.EDITABLE),),
        policy_path=policy,
        profile_id="default",
    )
    attempt = begin_attempt(workspace_root=tmp_path, session_id=session.id, label="A")
    task_run = verify(
        workspace_root=tmp_path, session_id=session.id, attempt_id=attempt.id
    )
    assert task_run["outcome"] == "FAILED"
    abandon_attempt(workspace_root=tmp_path, attempt_id=attempt.id)
    final_run = verify(workspace_root=tmp_path, session_id=session.id)
    with pytest.raises(VerificationServiceError, match="fresh verified session run"):
        finish_session(
            workspace_root=tmp_path, session_id=session.id, run_id=task_run["run_id"]
        )
    with pytest.raises(VerificationServiceError, match="fresh verified session run"):
        finish_session(
            workspace_root=tmp_path, session_id=session.id, run_id=final_run["run_id"]
        )
    shown = show_session(workspace_root=tmp_path, session_id=session.id)
    assert shown["attempts"][0]["lifecycle"] == "abandoned"
    assert "completion_receipt" not in shown["attempts"][0]


def test_session_finish_detects_reference_drift_and_allows_unchanged_commit(
    tmp_path: Path,
) -> None:
    _repo(tmp_path / "api")
    reference = _repo(tmp_path / "reference")
    _write(reference / "schema.txt", "same")
    policy = _policy(tmp_path / "policy.toml", repositories=("api", "reference"))
    session = begin_session(
        workspace_root=tmp_path,
        repositories=(
            ("api", "api", MemberRole.EDITABLE),
            ("reference", "reference", MemberRole.REFERENCE),
        ),
        policy_path=policy,
        profile_id="default",
    )
    run = verify(workspace_root=tmp_path, session_id=session.id)
    _write(reference / "schema.txt", "drift")
    with pytest.raises(VerificationServiceError, match="no longer matches"):
        finish_session(
            workspace_root=tmp_path, session_id=session.id, run_id=run["run_id"]
        )
    _write(reference / "schema.txt", "same")
    _git(reference, "add", "schema.txt")
    _git(reference, "commit", "-qm", "same-content")
    completed = json.loads(
        run_command(
            command(("change", "session", "finish")),
            [
                "change",
                "session",
                "finish",
                session.id,
                "--run",
                run["run_id"],
                "--workspace",
                str(tmp_path),
                "--format",
                "json",
            ],
        )
    )
    assert completed["current_applicability"]["status"] == "matches"


def _policy(
    path: Path,
    repositories: tuple[str, ...] = ("api", "schemas"),
    *,
    protected: bool = False,
) -> Path:
    repository_tables = "\n".join(
        f"[profiles.default.repositories.{repository}]\n"
        + (
            'protected_paths = ["schema.txt"]\n'
            if protected and repository == "schemas"
            else ""
        )
        for repository in repositories
    )
    steps = "\n".join(
        "[[profiles.default.steps]]\n"
        f'id = "{repository}-check"\n'
        'scope = "repository"\n'
        f'repository = "{repository}"\n'
        'argv = ["true"]\n'
        for repository in repositories
    )
    path.write_text(
        "schema_version = 1\n[profiles.default]\n" + repository_tables + steps
    )
    return path


def _integrity_policy(
    path: Path, *, helper: bool = False, reference: bool = True
) -> Path:
    api_protected = (
        'protected_paths = ["helper.sh"]'
        if helper
        else 'protected_paths = ["gate*.sh"]'
    )
    repositories = f"""[profiles.default.repositories.api]
{api_protected}
"""
    steps = """[[profiles.default.steps]]
id = "api-check"
scope = "repository"
repository = "api"
argv = ["true"]
"""
    if reference:
        repositories += "[profiles.default.repositories.reference]\n"
        steps += """[[profiles.default.steps]]
id = "reference-check"
scope = "repository"
repository = "reference"
argv = ["true"]
"""
    path.write_text("schema_version = 1\n[profiles.default]\n" + repositories + steps)
    return path


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(
        path,
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "user.name=Test",
        "commit",
        "--allow-empty",
        "-qm",
        "initial",
    )
    return path


def _write(path: Path, text: str) -> None:
    path.write_text(text)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
