"""Read-only advisory impact reports over retained workspace checkpoints."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from code_scout.impact.git import GitRepositoryIdentity
from code_scout.impact.mapping import (
    ChangeSeedResult,
    CoverageGap,
    map_checkpoint_ranges_to_symbols,
)
from code_scout.impact.paths import ImpactPathResult, trace_impact_paths
from code_scout.verification.checkpoints import ContentAddressedBlobStore
from code_scout.verification.contracts import (
    CapturedMember,
    WorkspaceCheckpoint,
    WorkspaceMember,
)
from code_scout.workspace.status import (
    WorkspaceStatusReport,
    workspace_status,
)


@dataclass(frozen=True)
class MemberImpactReport:
    """One member's scan-backed result; it is never merged by symbol name."""

    repository_id: str
    repository_root: str
    scan: dict[str, Any]
    seeds: ChangeSeedResult
    impact: ImpactPathResult | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "repository_root": self.repository_root,
            "scan": self.scan,
            "changed_files": [asdict(item) for item in self.seeds.files],
            "changed_symbols": [asdict(item) for item in self.seeds.symbols],
            "paths": [
                asdict(item) for item in (self.impact.paths if self.impact else ())
            ],
            "test_candidates": {
                "evidence_linked": [
                    asdict(item)
                    for item in (self.impact.test_candidates if self.impact else ())
                ],
                "inferred": [
                    asdict(item)
                    for item in (
                        self.impact.inferred_test_candidates if self.impact else ()
                    )
                ],
                "execution_status": "UNKNOWN",
            },
            "coverage_gaps": [
                asdict(item)
                for item in (
                    *self.seeds.coverage_gaps,
                    *((self.impact.coverage_gaps) if self.impact else ()),
                )
            ],
            "truncation": asdict(self.impact)["truncated"] if self.impact else False,
        }


@dataclass(frozen=True)
class CheckpointImpactReport:
    scope: str
    before_checkpoint_id: str
    after_checkpoint_id: str
    members: tuple[MemberImpactReport, ...]
    cross_repository_coverage_gaps: tuple[CoverageGap, ...]

    @property
    def impact_status(self) -> str:
        if not self.members:
            return "unavailable"
        if any(member.seeds.index_state != "ready" for member in self.members):
            return "partial"
        return "partial" if self.cross_repository_coverage_gaps else "available"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "before_checkpoint_id": self.before_checkpoint_id,
            "after_checkpoint_id": self.after_checkpoint_id,
            "impact_status": self.impact_status,
            "members": [member.to_dict() for member in self.members],
            "cross_repository_coverage_gaps": [
                asdict(item) for item in self.cross_repository_coverage_gaps
            ],
            "test_execution": "UNKNOWN",
        }


def generate_checkpoint_impact_report(
    *,
    workspace_root: Path,
    members: tuple[WorkspaceMember, ...],
    before: WorkspaceCheckpoint,
    after: WorkspaceCheckpoint,
    scope: str,
    max_hops: int = 2,
    max_results: int = 100,
) -> CheckpointImpactReport:
    """Compare retained bytes and reuse only each member's persisted graph.

    This performs no Git reads, scans, extractor runs, model calls, or tests.
    Cross-member traversal is intentionally absent: without an explicitly
    persisted qualified edge there is a coverage gap, not a guessed dependency.
    """
    if scope not in {"task-window", "cumulative-session"}:
        raise ValueError(
            "checkpoint impact scope must be task-window or cumulative-session"
        )
    workspace = workspace_root.resolve()
    blobs = ContentAddressedBlobStore(workspace / ".code-scout/verification/blobs")
    before_by_id = {item.id: item for item in before.members}
    after_by_id = {item.id: item for item in after.members}
    reports: list[MemberImpactReport] = []
    for member in sorted(members, key=lambda item: item.id):
        old = before_by_id.get(member.id)
        new = after_by_id.get(member.id)
        if old is None or new is None:
            continue
        status = workspace_status(member.root)
        identity = GitRepositoryIdentity(member.id, member.id, ".", member.root)
        seeds = map_checkpoint_ranges_to_symbols(
            member.root,
            repository=identity,
            before=_member_bytes(old, blobs),
            after=_member_bytes(new, blobs),
            status=status,
        )
        impact = (
            trace_impact_paths(
                member.root, seeds, status, max_hops=max_hops, max_results=max_results
            )
            if status.index.get("state") == "ready"
            else None
        )
        reports.append(
            MemberImpactReport(
                member.id,
                member.root,
                _scan_provenance(status, member),
                seeds,
                impact,
            )
        )
    gaps: tuple[CoverageGap, ...] = ()
    if len(members) > 1:
        gaps = tuple(
            CoverageGap(
                "cross_repository_coverage_unavailable",
                (
                    "No persisted qualified cross-repository edge was supplied; "
                    "integration impact is unknown."
                ),
                member.id,
                "",
            )
            for member in members
        )
    return CheckpointImpactReport(scope, before.id, after.id, tuple(reports), gaps)


def generate_task_and_session_impact(
    *,
    workspace_root: Path,
    members: tuple[WorkspaceMember, ...],
    task_before: WorkspaceCheckpoint,
    session_initial: WorkspaceCheckpoint,
    candidate: WorkspaceCheckpoint,
    max_hops: int = 2,
    max_results: int = 100,
) -> dict[str, CheckpointImpactReport]:
    """Return distinct task-window and cumulative-session advisory views."""
    return {
        "task_window": generate_checkpoint_impact_report(
            workspace_root=workspace_root,
            members=members,
            before=task_before,
            after=candidate,
            scope="task-window",
            max_hops=max_hops,
            max_results=max_results,
        ),
        "cumulative_session": generate_checkpoint_impact_report(
            workspace_root=workspace_root,
            members=members,
            before=session_initial,
            after=candidate,
            scope="cumulative-session",
            max_hops=max_hops,
            max_results=max_results,
        ),
    }


def _member_bytes(
    member: CapturedMember, blobs: ContentAddressedBlobStore
) -> dict[str, tuple[str, bytes] | None]:
    return {
        entry.path: (entry.content_sha256, blobs.get(entry.content_sha256))
        for entry in member.files
    }


def _scan_provenance(
    status: WorkspaceStatusReport, member: WorkspaceMember
) -> dict[str, Any]:
    return {
        "member_id": member.id,
        "member_worktree_identity": member.worktree_identity,
        "scan_root": str(Path(member.root).resolve()),
        "index_state": status.index.get("state", "unknown"),
        "freshness": dict(status.freshness),
    }
