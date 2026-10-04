"""Versioned, serializable contracts for executable change verification.

These types deliberately describe observations, not task completion claims.  The
reducer is the only place that derives the aggregate result from those facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

CONTRACT_SCHEMA_VERSION = 1


class MemberRole(StrEnum):
    EDITABLE = "editable"
    REFERENCE = "reference"


class AttemptLifecycle(StrEnum):
    ACTIVE = "active"
    ABANDONED = "abandoned"
    COMPLETED = "completed"


class RunScope(StrEnum):
    TASK = "task"
    SESSION = "session"


class GateScope(StrEnum):
    REPOSITORY = "repository"
    WORKSPACE = "workspace"


class GateOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    NOT_RUN = "NOT_RUN"


class VerificationOutcome(StrEnum):
    ERROR = "ERROR"
    INVALIDATED = "INVALIDATED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"
    VERIFIED = "VERIFIED"


class Applicability(StrEnum):
    MATCHES = "matches"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkspaceMember:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    root: str = ""
    git_common_directory: str = ""
    worktree_identity: str = ""
    role: MemberRole = MemberRole.EDITABLE


@dataclass(frozen=True)
class CapturedFile:
    path: str
    content_sha256: str
    mode: int
    size_bytes: int


@dataclass(frozen=True)
class CapturedMember:
    id: str
    role: MemberRole
    worktree_identity: str
    files: tuple[CapturedFile, ...]
    input_scope: tuple[str, ...]
    content_digest: str


@dataclass(frozen=True)
class ExcludedIgnoredPath:
    owner_id: str
    path: str
    rule: str


@dataclass(frozen=True)
class WorkspaceCheckpoint:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    workspace_digest: str = ""
    member_digests: tuple[tuple[str, str], ...] = ()
    captured_at: str = ""
    members: tuple[CapturedMember, ...] = ()
    workspace_files: tuple[CapturedFile, ...] = ()
    workspace_input_scope: tuple[str, ...] = ()
    input_scope_version: str = ""
    excluded_ignored_paths: tuple[ExcludedIgnoredPath, ...] = ()


@dataclass(frozen=True)
class TaskAttempt:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    label: str = ""
    session_id: str = ""
    before_checkpoint_id: str = ""
    previous_attempt_id: str | None = None
    lifecycle: AttemptLifecycle = AttemptLifecycle.ACTIVE


@dataclass(frozen=True)
class VerificationSession:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    members: tuple[WorkspaceMember, ...] = ()
    initial_checkpoint_id: str = ""
    frozen_policy_digest: str = ""
    policy_path: str = ""
    protected_input_digest: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class VerificationRun:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    session_id: str = ""
    scope: RunScope = RunScope.TASK
    candidate_checkpoint_id: str = ""
    candidate_digest: str = ""
    policy_digest: str = ""
    attempt_id: str | None = None


@dataclass(frozen=True)
class GateResult:
    """One gate observation; command output never determines ``outcome``."""

    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    scope: GateScope = GateScope.REPOSITORY
    repository_ids: tuple[str, ...] = ()
    candidate_digest: str = ""
    required: bool = True
    argv: tuple[str, ...] = ()
    cwd: str = ""
    timeout_seconds: int = 600
    outcome: GateOutcome = GateOutcome.NOT_RUN
    exit_code: int | None = None
    evidence_complete: bool = True
    persistence_error: str | None = None
    executable_path: str | None = None
    executable_sha256_before: str | None = None
    executable_sha256_after: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    stdout: dict[str, object] | None = None
    stderr: dict[str, object] | None = None


@dataclass(frozen=True)
class CompletionReceipt:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    id: str = ""
    session_id: str = ""
    run_id: str = ""
    checkpoint_id: str = ""
    policy_digest: str = ""
    attempt_id: str | None = None


@dataclass(frozen=True)
class ApplicabilityResult:
    schema_version: int = CONTRACT_SCHEMA_VERSION
    status: Applicability = Applicability.UNKNOWN
    reason: str | None = None


@dataclass(frozen=True)
class ResultInputs:
    """Facts used by the deterministic outcome reducer."""

    gates: tuple[GateResult, ...] = ()
    setup_error: bool = False
    interrupted: bool = False
    essential_evidence_complete: bool = True
    policy_drift: bool = False
    protected_input_drift: bool = False
    executable_drift: bool = False
    candidate_drift: bool = False


@dataclass(frozen=True)
class ReducedResult:
    schema_version: int
    outcome: VerificationOutcome
    required_gate_count: int
    required_gate_pass_count: int
    findings: tuple[str, ...] = field(default_factory=tuple)


def reduce_result(inputs: ResultInputs) -> ReducedResult:
    """Apply FR-12 precedence without interpreting stdout or agent prose."""

    required = tuple(gate for gate in inputs.gates if gate.required)
    passed = tuple(gate for gate in required if gate.outcome is GateOutcome.PASS)
    findings: list[str] = []
    if inputs.setup_error or inputs.interrupted:
        findings.append("setup_or_execution_error")
        outcome = VerificationOutcome.ERROR
    elif any(
        gate.outcome in {GateOutcome.ERROR, GateOutcome.CANCELLED} for gate in required
    ):
        findings.append("required_gate_execution_error")
        outcome = VerificationOutcome.ERROR
    elif not inputs.essential_evidence_complete or any(
        not gate.evidence_complete or gate.persistence_error for gate in inputs.gates
    ):
        findings.append("essential_evidence_incomplete")
        outcome = VerificationOutcome.ERROR
    elif any(
        (
            inputs.policy_drift,
            inputs.protected_input_drift,
            inputs.executable_drift,
            inputs.candidate_drift,
        )
    ):
        findings.append("identity_or_integrity_drift")
        outcome = VerificationOutcome.INVALIDATED
    elif any(
        gate.outcome in {GateOutcome.FAIL, GateOutcome.TIMEOUT} for gate in required
    ):
        findings.append("required_gate_failed")
        outcome = VerificationOutcome.FAILED
    elif not required or any(gate.outcome is not GateOutcome.PASS for gate in required):
        findings.append("required_gate_incomplete")
        outcome = VerificationOutcome.INCOMPLETE
    else:
        outcome = VerificationOutcome.VERIFIED
    return ReducedResult(
        schema_version=CONTRACT_SCHEMA_VERSION,
        outcome=outcome,
        required_gate_count=len(required),
        required_gate_pass_count=len(passed),
        findings=tuple(findings),
    )
