"""Lifecycle orchestration and read-only inspection for verification receipts."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_scout.core.errors import ConfigError
from code_scout.verification.attempts import (
    _attempt_from_payload,
    _attempt_records,
    _checkpoint_from_payload,
    _load_attempt,
    _load_session,
    _scope_from_checkpoints,
)
from code_scout.verification.contracts import (
    Applicability,
    AttemptLifecycle,
    CompletionReceipt,
    ResultInputs,
    RunScope,
    WorkspaceCheckpoint,
    reduce_result,
)
from code_scout.verification.execution import execute_frozen_profile
from code_scout.verification.impact import generate_task_and_session_impact
from code_scout.verification.integrity import check_session_integrity
from code_scout.verification.sessions import (
    _capture_stably,
    _recheck_members,
    _session_locks,
)
from code_scout.verification.store import RunEvidenceError, RunEvidenceStore


class VerificationServiceError(ConfigError):
    """A requested verification lifecycle operation is invalid or unreadable."""


def verify(
    *, workspace_root: Path, session_id: str, attempt_id: str | None = None
) -> dict[str, Any]:
    """Capture one candidate, execute the frozen profile, and publish one receipt."""

    workspace = workspace_root.resolve()
    session, initial, profile = _load_session(workspace, session_id)
    scope = RunScope.SESSION if attempt_id is None else RunScope.TASK
    before = initial
    if attempt_id is None:
        if any(
            item["attempt"]["lifecycle"] == AttemptLifecycle.ACTIVE.value
            for item in _attempt_records(workspace, session_id)
        ):
            raise VerificationServiceError(
                "session verify rejects an active task attempt"
            )
    else:
        _path, payload = _load_attempt(workspace, attempt_id)
        attempt = _attempt_from_payload(payload["attempt"])
        if attempt.session_id != session_id:
            raise VerificationServiceError("task attempt does not belong to session")
        if attempt.lifecycle is not AttemptLifecycle.ACTIVE:
            raise VerificationServiceError(
                "only an active task attempt can be verified"
            )
        before = _checkpoint_from_payload(
            payload["before_checkpoint"], attempt.before_checkpoint_id
        )

    with _session_locks(workspace, session.members):
        _recheck_members(session.members)
        if _session_completion_receipt(workspace, session_id) is not None:
            raise VerificationServiceError(
                "finished session rejects new verification runs"
            )
        lifecycle_digest = _session_lifecycle_digest(workspace, session_id)
        candidate = _capture_stably(
            workspace,
            session.members,
            profile,
            _scope_from_checkpoints([initial, before]),
        )
        candidate = replace(candidate, id=f"checkpoint-{uuid.uuid4().hex}")
        _recheck_members(session.members)
        run_id = f"run-{uuid.uuid4().hex}"
        store = RunEvidenceStore(workspace, profile.limits)
        writer = store.begin(run_id, candidate)
        try:
            execution = execute_frozen_profile(
                profile=profile,
                members=session.members,
                workspace_root=workspace,
                candidate=candidate,
                artifact_writer=writer,
                integrity_check=lambda: check_session_integrity(
                    workspace_root=workspace,
                    session_id=session_id,
                    candidate=candidate,
                    already_locked=True,
                ),
            )
            impacts = generate_task_and_session_impact(
                workspace_root=workspace,
                members=session.members,
                task_before=before,
                session_initial=initial,
                candidate=candidate,
            )
            artifacts = [
                writer.write_bytes("candidate.json", _json(candidate)),
                writer.write_bytes(
                    "impact.json",
                    _json({k: v.to_dict() for k, v in impacts.items()}),
                    essential=False,
                ),
            ]
            for gate in execution.gates:
                for record in (gate.stdout, gate.stderr):
                    if record and "name" in record and "sha256" in record:
                        artifacts.append(dict(record))
            reduced = reduce_result(
                ResultInputs(
                    gates=execution.gates,
                    policy_drift=any(
                        item.policy_drift for item in execution.integrity_checks
                    ),
                    protected_input_drift=any(
                        item.protected_input_drift
                        for item in execution.integrity_checks
                    ),
                    candidate_drift=any(
                        item.candidate_drift for item in execution.integrity_checks
                    ),
                    executable_drift=any(
                        gate.executable_sha256_before != gate.executable_sha256_after
                        for gate in execution.gates
                        if gate.executable_path
                    ),
                )
            )
            manifest = writer.publish(
                {
                    "session_id": session.id,
                    "scope": scope.value,
                    "attempt_id": attempt_id,
                    "session_lifecycle_digest": lifecycle_digest,
                    "policy_digest": session.frozen_policy_digest,
                    "outcome": reduced.outcome.value,
                    "result": asdict(reduced),
                    "gates": [asdict(gate) for gate in execution.gates],
                    "integrity": [asdict(item) for item in execution.integrity_checks],
                    "impact": {key: value.to_dict() for key, value in impacts.items()},
                    "artifacts": artifacts,
                }
            )
        except BaseException as error:
            # A captured candidate must leave an explicit failed receipt when an
            # external interruption arrives during gates, impact, or publication.
            # The original interruption still propagates to the CLI caller.
            try:
                writer.publish(
                    {
                        "session_id": session.id,
                        "scope": scope.value,
                        "attempt_id": attempt_id,
                        "session_lifecycle_digest": lifecycle_digest,
                        "policy_digest": session.frozen_policy_digest,
                        "outcome": "ERROR",
                        "result": {
                            "outcome": "ERROR",
                            "required_gate_count": 0,
                            "required_gate_pass_count": 0,
                            "findings": ["interrupted_run"],
                        },
                        "interrupted": True,
                        "error": str(error),
                        "artifacts": [],
                    }
                )
            except Exception:
                writer.discard()
            writer.discard()
            raise
    return manifest


def inspect_run(*, workspace_root: Path, run_id: str) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    manifest = _inspect_run_evidence(workspace, run_id)
    session_id = str(manifest["session_id"])
    candidate = _candidate_from_run(workspace, run_id)
    return {
        **manifest,
        "current_applicability": _applicability(workspace, session_id, candidate),
    }


def _inspect_run_evidence(workspace: Path, run_id: str) -> dict[str, Any]:
    session_id = _run_session_id(workspace, run_id)
    _session, _initial, profile = _load_session(workspace, session_id)
    return RunEvidenceStore(workspace, profile.limits).inspect(run_id)


def finish_attempt(
    *, workspace_root: Path, attempt_id: str, run_id: str
) -> dict[str, Any]:
    """Record one historical task completion without changing workspace or Git.

    A finish is deliberately stricter than inspection: it binds an active
    attempt to one already-published, successful task run whose candidate is
    still the locked live workspace.  Later edits never rewrite this receipt.
    """

    workspace = workspace_root.resolve()
    path, payload = _load_attempt(workspace, attempt_id)
    attempt = _attempt_from_payload(payload["attempt"])
    existing = payload.get("completion_receipt")
    if existing is not None:
        receipt = _receipt_from_payload(existing, attempt_id)
        if receipt.run_id != run_id:
            raise VerificationServiceError(
                "completed task attempt is bound to a different verification run"
            )
        # Retained evidence remains essential even for an idempotent retry.
        inspected = inspect_run(workspace_root=workspace, run_id=receipt.run_id)
        _validate_receipt_run(workspace, receipt, inspected, attempt)
        return _historical_receipt(receipt, inspected["current_applicability"])
    if attempt.lifecycle is not AttemptLifecycle.ACTIVE:
        raise VerificationServiceError(f"task attempt is not active: {attempt_id}")

    session, _initial, _profile = _load_session(workspace, attempt.session_id)
    with _session_locks(workspace, session.members):
        _recheck_members(session.members)
        # Inspect before capture so corruption is never converted into a finish.
        inspected = _inspect_run_evidence(workspace, run_id)
        receipt = _new_receipt(session.id, attempt, inspected)
        _validate_receipt_run(workspace, receipt, inspected, attempt)
        candidate = _candidate_from_run(workspace, run_id)
        integrity = check_session_integrity(
            workspace_root=workspace,
            session_id=session.id,
            candidate=candidate,
            already_locked=True,
        )
        _recheck_members(session.members)
        if integrity.drifted:
            raise VerificationServiceError(
                "live workspace no longer matches the verified task candidate"
            )
        # ``check_session_integrity`` includes the frozen policy, protected
        # inputs, reference members, all candidate members, and stable capture.
        payload["attempt"] = _attempt_json(
            replace(attempt, lifecycle=AttemptLifecycle.COMPLETED)
        )
        payload["completion_receipt"] = _receipt_json(receipt)
        payload["completed_at"] = datetime.now(UTC).isoformat()
        _replace_attempt_payload(path, payload)
    return _historical_receipt(receipt, {"status": Applicability.MATCHES.value})


def finish_session(
    *, workspace_root: Path, session_id: str, run_id: str
) -> dict[str, Any]:
    """Bind a fresh aggregate run to one immutable session completion receipt."""

    workspace = workspace_root.resolve()
    session, _initial, _profile = _load_session(workspace, session_id)
    existing = _session_completion_receipt(workspace, session_id)
    if existing is not None:
        if existing.run_id != run_id:
            raise VerificationServiceError(
                "finished session is bound to a different verification run"
            )
        inspected = inspect_run(workspace_root=workspace, run_id=run_id)
        _validate_session_receipt_run(workspace, session, existing, inspected)
        return _historical_receipt(existing, inspected["current_applicability"])

    if any(
        item["attempt"]["lifecycle"] == AttemptLifecycle.ACTIVE.value
        for item in _attempt_records(workspace, session_id)
    ):
        raise VerificationServiceError("session finish rejects an active task attempt")

    with _session_locks(workspace, session.members):
        _recheck_members(session.members)
        inspected = _inspect_run_evidence(workspace, run_id)
        receipt = _new_receipt(session.id, None, inspected)
        _validate_session_receipt_run(workspace, session, receipt, inspected)
        candidate = _candidate_from_run(workspace, run_id)
        integrity = check_session_integrity(
            workspace_root=workspace,
            session_id=session.id,
            candidate=candidate,
            already_locked=True,
        )
        _recheck_members(session.members)
        if integrity.drifted:
            raise VerificationServiceError(
                "live workspace no longer matches the verified session candidate"
            )
        _replace_session_completion_receipt(workspace, session_id, receipt)
    return _historical_receipt(receipt, {"status": Applicability.MATCHES.value})


def _new_receipt(
    session_id: str, attempt: Any | None, manifest: dict[str, Any]
) -> CompletionReceipt:
    return CompletionReceipt(
        id=f"receipt-{uuid.uuid4().hex}",
        session_id=session_id,
        run_id=str(manifest.get("run_id", "")),
        checkpoint_id=str(manifest.get("checkpoint_id", "")),
        policy_digest=str(manifest.get("policy_digest", "")),
        attempt_id=attempt.id if attempt is not None else None,
    )


def _validate_receipt_run(
    workspace: Path,
    receipt: CompletionReceipt,
    manifest: dict[str, Any],
    attempt: Any,
) -> None:
    if receipt.attempt_id != attempt.id or receipt.session_id != attempt.session_id:
        raise VerificationServiceError("completion receipt has the wrong task identity")
    session, _initial, _profile = _load_session(workspace, receipt.session_id)
    if (
        manifest.get("run_id") != receipt.run_id
        or manifest.get("session_id") != receipt.session_id
        or manifest.get("scope") != RunScope.TASK.value
        or manifest.get("attempt_id") != receipt.attempt_id
        or manifest.get("checkpoint_id") != receipt.checkpoint_id
        or manifest.get("candidate_digest") is None
        or manifest.get("policy_digest") != receipt.policy_digest
        or receipt.policy_digest != session.frozen_policy_digest
        or manifest.get("outcome") != "VERIFIED"
    ):
        raise VerificationServiceError(
            "verification run is not a verified task run for this attempt"
        )
    candidate = _candidate_from_run(workspace, receipt.run_id)
    if (
        candidate.id != receipt.checkpoint_id
        or candidate.workspace_digest != manifest.get("candidate_digest")
    ):
        raise VerificationServiceError("verification run candidate is inconsistent")


def _validate_session_receipt_run(
    workspace: Path,
    session: Any,
    receipt: CompletionReceipt,
    manifest: dict[str, Any],
) -> None:
    if (
        receipt.session_id != session.id
        or receipt.attempt_id is not None
        or manifest.get("run_id") != receipt.run_id
        or manifest.get("session_id") != session.id
        or manifest.get("scope") != RunScope.SESSION.value
        or manifest.get("attempt_id") is not None
        or manifest.get("checkpoint_id") != receipt.checkpoint_id
        or manifest.get("candidate_digest") is None
        or manifest.get("policy_digest") != receipt.policy_digest
        or receipt.policy_digest != session.frozen_policy_digest
        or manifest.get("outcome") != "VERIFIED"
        or manifest.get("session_lifecycle_digest")
        != _session_lifecycle_digest(workspace, session.id)
    ):
        raise VerificationServiceError(
            "verification run is not a fresh verified session run"
        )
    candidate = _candidate_from_run(workspace, receipt.run_id)
    if (
        candidate.id != receipt.checkpoint_id
        or candidate.workspace_digest != manifest.get("candidate_digest")
    ):
        raise VerificationServiceError("verification run candidate is inconsistent")


def _receipt_from_payload(value: object, attempt_id: str) -> CompletionReceipt:
    if not isinstance(value, dict):
        raise VerificationServiceError("invalid task completion receipt")
    try:
        receipt = CompletionReceipt(
            id=value["id"],
            session_id=value["session_id"],
            run_id=value["run_id"],
            checkpoint_id=value["checkpoint_id"],
            policy_digest=value["policy_digest"],
            attempt_id=value.get("attempt_id"),
        )
    except (KeyError, TypeError) as error:
        raise VerificationServiceError("invalid task completion receipt") from error
    if receipt.attempt_id != attempt_id:
        raise VerificationServiceError("completion receipt has the wrong task identity")
    return receipt


def _historical_receipt(
    receipt: CompletionReceipt, applicability: dict[str, Any]
) -> dict[str, Any]:
    return {
        "receipt": _receipt_json(receipt),
        "historical": True,
        "current_applicability": applicability,
    }


def _receipt_json(receipt: CompletionReceipt) -> dict[str, Any]:
    return asdict(receipt)


def _attempt_json(attempt: Any) -> dict[str, Any]:
    value = asdict(attempt)
    value["lifecycle"] = attempt.lifecycle.value
    return value


def _replace_attempt_payload(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".attempt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        Path(temporary).unlink(missing_ok=True)
        raise VerificationServiceError(
            f"could not publish task completion receipt: {error}"
        ) from error


def show_attempt(*, workspace_root: Path, attempt_id: str) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    _path, payload = _load_attempt(workspace, attempt_id)
    attempt = payload["attempt"]
    runs = _runs_for(workspace, attempt["session_id"], attempt_id)
    return {
        "attempt": attempt,
        "comparisons": payload.get("comparisons", {}),
        "runs": runs,
    }


def show_session(*, workspace_root: Path, session_id: str) -> dict[str, Any]:
    workspace = workspace_root.resolve()
    session, _initial, _profile = _load_session(workspace, session_id)
    attempts = [item["attempt"] for item in _attempt_records(workspace, session_id)]
    runs = _runs_for(workspace, session_id, None)
    receipt = _session_completion_receipt(workspace, session_id)
    return {
        "session": asdict(session),
        "attempts": attempts,
        "runs": runs,
        "completion_receipt": _receipt_json(receipt) if receipt is not None else None,
    }


def _session_lifecycle_digest(workspace: Path, session_id: str) -> str:
    attempts = [
        {
            "id": item["attempt"]["id"],
            "lifecycle": item["attempt"]["lifecycle"],
            "previous_attempt_id": item["attempt"].get("previous_attempt_id"),
        }
        for item in _attempt_records(workspace, session_id)
    ]
    return hashlib.sha256(
        json.dumps(attempts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _session_completion_receipt(
    workspace: Path, session_id: str
) -> CompletionReceipt | None:
    path = workspace / ".code-scout/verification/sessions" / f"{session_id}.json"
    try:
        value = json.loads(path.read_text()).get("completion_receipt")
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationServiceError(
            f"could not read verification session {session_id}"
        ) from error
    if value is None:
        return None
    return _receipt_from_payload(value, None)


def _replace_session_completion_receipt(
    workspace: Path, session_id: str, receipt: CompletionReceipt
) -> None:
    path = workspace / ".code-scout/verification/sessions" / f"{session_id}.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationServiceError(
            f"could not read verification session {session_id}"
        ) from error
    if payload.get("completion_receipt") is not None:
        raise VerificationServiceError("session completion receipt already exists")
    payload["completion_receipt"] = _receipt_json(receipt)
    _replace_attempt_payload(path, payload)


def _runs_for(
    workspace: Path, session_id: str, attempt_id: str | None
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for manifest in (workspace / ".code-scout/verification/runs").glob(
        "*/manifest.json"
    ):
        try:
            value = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("session_id") != session_id:
            continue
        if attempt_id is not None and value.get("attempt_id") != attempt_id:
            continue
        try:
            inspected = inspect_run(
                workspace_root=workspace, run_id=str(value["run_id"])
            )
            runs.append(
                {
                    "run_id": value["run_id"],
                    "outcome": value.get("outcome"),
                    "current_applicability": inspected["current_applicability"],
                }
            )
        except (RunEvidenceError, VerificationServiceError):
            runs.append(
                {
                    "run_id": value.get("run_id"),
                    "outcome": "unreadable",
                    "current_applicability": {"status": Applicability.UNKNOWN.value},
                }
            )
    return sorted(runs, key=lambda item: str(item["run_id"]))


def _run_session_id(workspace: Path, run_id: str) -> str:
    path = workspace / ".code-scout/verification/runs" / run_id / "manifest.json"
    try:
        session_id = json.loads(path.read_text())["session_id"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise VerificationServiceError(
            f"could not read verification run {run_id}"
        ) from error
    if not isinstance(session_id, str):
        raise VerificationServiceError(f"invalid verification run {run_id}")
    return session_id


def _candidate_from_run(workspace: Path, run_id: str) -> WorkspaceCheckpoint:
    path = workspace / ".code-scout/verification/runs" / run_id / "candidate.json"
    try:
        value = json.loads(path.read_text())
        return _checkpoint_from_payload(value, value["id"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise VerificationServiceError(
            f"run candidate is unreadable: {run_id}"
        ) from error


def _applicability(
    workspace: Path, session_id: str, candidate: WorkspaceCheckpoint
) -> dict[str, str]:
    """Use a disposable blob store: inspection must not publish or mutate receipts."""
    session, initial, profile = _load_session(workspace, session_id)
    try:
        with tempfile.TemporaryDirectory(prefix="code-scout-inspect-") as temporary:
            with _session_locks(workspace, session.members):
                _recheck_members(session.members)
                current = _capture_stably(
                    workspace,
                    session.members,
                    profile,
                    _scope_from_checkpoints([initial, candidate]),
                    Path(temporary) / "blobs",
                )
                _recheck_members(session.members)
        status = (
            Applicability.MATCHES
            if current.workspace_digest == candidate.workspace_digest
            else Applicability.DIFFERENT
        )
        return {"status": status.value}
    except Exception as error:
        return {"status": Applicability.UNKNOWN.value, "reason": str(error)}


def _json(value: object) -> bytes:
    return json.dumps(
        asdict(value) if hasattr(value, "__dataclass_fields__") else value,
        sort_keys=True,
        default=str,
    ).encode()
