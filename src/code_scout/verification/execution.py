"""Bounded, sequential execution of a frozen verification profile."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from code_scout.verification.contracts import (
    GateOutcome,
    GateResult,
    GateScope,
    WorkspaceCheckpoint,
    WorkspaceMember,
)
from code_scout.verification.integrity import IntegrityCheck
from code_scout.verification.policy import GateDefinition, VerificationProfile
from code_scout.verification.store import RunArtifactWriter

_CLEANUP_SECONDS = 5.0
_POLL_SECONDS = 0.02


@dataclass(frozen=True)
class ProfileExecution:
    """Observations from exactly one complete profile execution."""

    candidate_digest: str
    gates: tuple[GateResult, ...]
    integrity_checks: tuple[IntegrityCheck, ...] = ()

    @property
    def integrity_drifted(self) -> bool:
        return any(check.drifted for check in self.integrity_checks)


class _Output:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.chunks: list[bytes] = []
        self.size = 0
        self.retained = 0

    def add(self, chunk: bytes) -> None:
        self.size += len(chunk)
        remaining = self.limit - self.retained
        if remaining > 0:
            kept = chunk[:remaining]
            self.chunks.append(kept)
            self.retained += len(kept)


def execute_frozen_profile(
    *,
    profile: VerificationProfile,
    members: tuple[WorkspaceMember, ...],
    workspace_root: Path,
    candidate: WorkspaceCheckpoint,
    artifact_writer: RunArtifactWriter | None = None,
    cancel_event: threading.Event | None = None,
    integrity_check: Callable[[], IntegrityCheck] | None = None,
) -> ProfileExecution:
    """Run every declared gate once, against one already-captured candidate.

    The caller supplies the retained candidate rather than an attempt or prior
    receipt.  Consequently every result is bound to the same aggregate digest
    and cannot be assembled from earlier repository-only runs.
    """

    roots = {member.id: Path(member.root).resolve() for member in members}
    if set(roots) != set(profile.repositories):
        raise ValueError("profile members do not match the frozen session")
    workspace = workspace_root.resolve()
    results: list[GateResult] = []
    integrity_checks: list[IntegrityCheck] = []
    stopped = False
    for gate in profile.gates:
        if integrity_check is not None:
            observed = integrity_check()
            integrity_checks.append(observed)
            if observed.drifted:
                stopped = True
        if stopped or (cancel_event is not None and cancel_event.is_set()):
            results.append(_not_run(gate, candidate.workspace_digest))
            stopped = True
            continue
        result = _execute_gate(
            gate=gate,
            roots=roots,
            workspace=workspace,
            candidate_digest=candidate.workspace_digest,
            profile=profile,
            artifact_writer=artifact_writer,
            cancel_event=cancel_event,
        )
        results.append(result)
        if integrity_check is not None:
            observed = integrity_check()
            integrity_checks.append(observed)
            if observed.drifted:
                stopped = True
        if result.outcome is GateOutcome.CANCELLED:
            stopped = True
    # A final capture is deliberately separate from the after-gate checks: it
    # closes the small interval between the final receipt and publication.
    if integrity_check is not None:
        integrity_checks.append(integrity_check())
    return ProfileExecution(
        candidate.workspace_digest, tuple(results), tuple(integrity_checks)
    )


def _execute_gate(
    *,
    gate: GateDefinition,
    roots: dict[str, Path],
    workspace: Path,
    candidate_digest: str,
    profile: VerificationProfile,
    artifact_writer: RunArtifactWriter | None,
    cancel_event: threading.Event | None,
) -> GateResult:
    cwd = _cwd(gate, roots, workspace)
    started_at = _now()
    started = time.monotonic()
    env = _environment(profile.environment_allowlist)
    executable = _find_executable(gate.argv[0], env)
    before = _binary_digest(Path(executable)) if executable else None
    stdout = _Output(profile.limits.stdout_bytes)
    stderr = _Output(profile.limits.stderr_bytes)
    outcome = GateOutcome.ERROR
    exit_code: int | None = None
    if executable is not None:
        try:
            process = subprocess.Popen(
                gate.argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            stdout_thread = _drain(process.stdout, stdout)
            stderr_thread = _drain(process.stderr, stderr)
            outcome, exit_code = _wait(process, gate.timeout_seconds, cancel_event)
            stdout_thread.join(timeout=_CLEANUP_SECONDS)
            stderr_thread.join(timeout=_CLEANUP_SECONDS)
        except OSError as error:
            stderr.add(str(error).encode("utf-8", "replace"))
            outcome = GateOutcome.ERROR
    after = _binary_digest(Path(executable)) if executable else None
    stdout_record, stderr_record, persistence_error = _persist_logs(
        artifact_writer, gate.id, stdout, stderr, profile
    )
    finished = time.monotonic()
    return GateResult(
        id=gate.id,
        scope=gate.scope,
        repository_ids=gate.repository_ids,
        candidate_digest=candidate_digest,
        required=gate.required,
        argv=gate.argv,
        cwd=str(cwd),
        timeout_seconds=gate.timeout_seconds,
        outcome=outcome,
        exit_code=exit_code,
        evidence_complete=persistence_error is None,
        persistence_error=persistence_error,
        executable_path=executable,
        executable_sha256_before=before,
        executable_sha256_after=after,
        started_at=started_at,
        finished_at=_now(),
        duration_seconds=finished - started,
        stdout=stdout_record,
        stderr=stderr_record,
    )


def _cwd(gate: GateDefinition, roots: dict[str, Path], workspace: Path) -> Path:
    root = (
        workspace
        if gate.scope is GateScope.WORKSPACE
        else roots[gate.repository_ids[0]]
    )
    cwd = (root / gate.cwd).resolve()
    try:
        cwd.relative_to(root)
    except ValueError as error:
        raise ValueError(f"gate cwd escapes declared root: {gate.id}") from error
    if not cwd.is_dir():
        raise ValueError(f"gate cwd is not a directory: {gate.id}")
    return cwd


def _environment(allowlist: tuple[str, ...]) -> dict[str, str]:
    required = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
    names = set(required) | set(allowlist)
    return {name: os.environ[name] for name in names if name in os.environ}


def _find_executable(command: str, env: dict[str, str]) -> str | None:
    candidate = Path(command)
    if candidate.parent != Path("."):
        return str(candidate.resolve()) if os.access(candidate, os.X_OK) else None
    for directory in os.get_exec_path(env):
        path = Path(directory) / command
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    return None


def _drain(stream: object, output: _Output) -> threading.Thread:
    def read() -> None:
        assert hasattr(stream, "read")
        while chunk := stream.read(65536):
            output.add(chunk)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


def _wait(
    process: subprocess.Popen[bytes], timeout: int, cancel: threading.Event | None
) -> tuple[GateOutcome, int | None]:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if cancel is not None and cancel.is_set():
            _stop_process_group(process)
            return GateOutcome.CANCELLED, process.returncode
        if time.monotonic() >= deadline:
            _stop_process_group(process)
            return GateOutcome.TIMEOUT, process.returncode
        time.sleep(_POLL_SECONDS)
    return (
        GateOutcome.PASS if process.returncode == 0 else GateOutcome.FAIL,
        process.returncode,
    )


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=_CLEANUP_SECONDS)


def _persist_logs(
    writer: RunArtifactWriter | None,
    gate_id: str,
    stdout: _Output,
    stderr: _Output,
    profile: VerificationProfile,
) -> tuple[dict[str, object] | None, dict[str, object] | None, str | None]:
    if writer is None:
        return _record(stdout), _record(stderr), None
    try:
        out = writer.write_stream(
            f"gates/{gate_id}.stdout.log",
            stdout.chunks,
            stream_limit=profile.limits.stdout_bytes,
        )
        err = writer.write_stream(
            f"gates/{gate_id}.stderr.log",
            stderr.chunks,
            stream_limit=profile.limits.stderr_bytes,
        )
        out["original_size_bytes"] = stdout.size
        out["truncated"] = stdout.size > profile.limits.stdout_bytes
        err["original_size_bytes"] = stderr.size
        err["truncated"] = stderr.size > profile.limits.stderr_bytes
        return out, err, None
    except Exception as error:
        return None, None, str(error)


def _record(output: _Output) -> dict[str, object]:
    return {
        "original_size_bytes": output.size,
        "retained_size_bytes": output.retained,
        "truncated": output.size > output.limit,
    }


def _binary_digest(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _not_run(gate: GateDefinition, candidate_digest: str) -> GateResult:
    return GateResult(
        id=gate.id,
        scope=gate.scope,
        repository_ids=gate.repository_ids,
        candidate_digest=candidate_digest,
        required=gate.required,
        argv=gate.argv,
        timeout_seconds=gate.timeout_seconds,
        outcome=GateOutcome.NOT_RUN,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
