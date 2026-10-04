from __future__ import annotations

from pathlib import Path

from code_scout.impact.git import (
    GitComparison,
    GitRevisionIdentity,
)
from code_scout.impact.mapping import ChangeSeedResult, MappingStatus
from code_scout.verification.checkpoints import ContentAddressedBlobStore
from code_scout.verification.contracts import (
    CapturedFile,
    CapturedMember,
    MemberRole,
    WorkspaceCheckpoint,
    WorkspaceMember,
)
from code_scout.verification.impact import generate_task_and_session_impact
from code_scout.workspace.status import WorkspaceStatusReport


def _status(root: Path, state: str = "ready") -> WorkspaceStatusReport:
    return WorkspaceStatusReport(
        workspace={},
        index={"state": state},
        freshness={"state": "stale"},
        changes={},
        coverage={},
    )


def _checkpoint(
    identifier: str, members: tuple[CapturedMember, ...]
) -> WorkspaceCheckpoint:
    return WorkspaceCheckpoint(
        id=identifier, workspace_digest=identifier, members=members
    )


def _member(identifier: str, files: tuple[CapturedFile, ...]) -> CapturedMember:
    return CapturedMember(
        identifier,
        MemberRole.EDITABLE,
        identifier,
        files,
        tuple(item.path for item in files),
        identifier,
    )


def _file(store: ContentAddressedBlobStore, path: str, source: bytes) -> CapturedFile:
    return CapturedFile(path, store.put(source), 0o100644, len(source))


def test_checkpoint_impact_keeps_scopes_members_and_cross_repo_gap(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    api, client = root / "api", root / "client"
    api.mkdir()
    client.mkdir()
    store = ContentAddressedBlobStore(root / ".code-scout/verification/blobs")
    initial = _checkpoint(
        "initial",
        (
            _member("api", (_file(store, "same.py", b"old"),)),
            _member("client", (_file(store, "same.py", b"old"),)),
        ),
    )
    candidate = _checkpoint(
        "candidate",
        (_member("api", (_file(store, "same.py", b"new"),)), _member("client", ())),
    )
    members = (
        WorkspaceMember(id="api", root=str(api), role=MemberRole.EDITABLE),
        WorkspaceMember(id="client", root=str(client), role=MemberRole.EDITABLE),
    )
    observed: list[
        tuple[
            str,
            dict[str, tuple[str, bytes] | None],
            dict[str, tuple[str, bytes] | None],
        ]
    ] = []

    def fake_mapping(workspace, *, repository, before, after, status):
        observed.append((repository.id, before, after))
        return ChangeSeedResult(
            repository,
            GitComparison(
                "checkpoint",
                GitRevisionIdentity("old", "checkpoint", None),
                GitRevisionIdentity("new", "checkpoint", None),
            ),
            str(status.index["state"]),
            dict(status.freshness),
            (),
            (),
            (),
        )

    monkeypatch.setattr(
        "code_scout.verification.impact.workspace_status",
        lambda path: _status(Path(path)),
    )
    monkeypatch.setattr(
        "code_scout.verification.impact.map_checkpoint_ranges_to_symbols", fake_mapping
    )
    monkeypatch.setattr(
        "code_scout.verification.impact.trace_impact_paths",
        lambda *args, **kwargs: None,
    )

    reports = generate_task_and_session_impact(
        workspace_root=root,
        members=members,
        task_before=initial,
        session_initial=initial,
        candidate=candidate,
    )

    assert set(reports) == {"task_window", "cumulative_session"}
    task = reports["task_window"].to_dict()
    assert task["test_execution"] == "UNKNOWN"
    assert {member["repository_id"] for member in task["members"]} == {"api", "client"}
    assert all(
        gap["code"] == "cross_repository_coverage_unavailable"
        for gap in task["cross_repository_coverage_gaps"]
    )
    assert (
        observed[0][0] == "api"
        and observed[0][1]["same.py"] != observed[0][2]["same.py"]
    )
    assert observed[1][0] == "client" and observed[1][2] == {}


def test_checkpoint_impact_reports_unavailable_member_index_without_scanning(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    store = ContentAddressedBlobStore(root / ".code-scout/verification/blobs")
    before = _checkpoint(
        "before", (_member("repo", (_file(store, "gone.py", b"old"),)),)
    )
    after = _checkpoint("after", (_member("repo", ()),))
    member = WorkspaceMember(id="repo", root=str(repo), role=MemberRole.EDITABLE)
    monkeypatch.setattr(
        "code_scout.verification.impact.workspace_status",
        lambda path: _status(Path(path), "unscanned"),
    )

    report = generate_task_and_session_impact(
        workspace_root=root,
        members=(member,),
        task_before=before,
        session_initial=before,
        candidate=after,
    )["task_window"]

    assert report.impact_status == "partial"
    changed = report.to_dict()["members"][0]["changed_files"][0]
    assert changed["old_content_sha256"] and changed["new_content_sha256"] is None
    assert changed["mapping_status"] is MappingStatus.UNRESOLVED
    assert changed["mapping_reason"] == "index_unavailable"
