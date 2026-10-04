from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_scout.commands import CommandOptions, command, execute_command, run_command
from code_scout.core.errors import CommandUsageError, StorageError
from code_scout.models.graph import Snapshot
from code_scout.pipeline.scan import scan_workspace
from code_scout.pipeline.snapshot import snapshot_workspace
from code_scout.pipeline.watch import WatchFileEvent, watch_workspace
from code_scout.storage import (
    SnapshotPayload,
    create_snapshot,
    database_path,
    get_snapshot_payload,
    list_snapshots,
    open_storage,
    upsert_workspace,
)
from code_scout.workspace.config import create_workspace_config


def _scan_package(workspace: Path, name: str) -> None:
    create_workspace_config(workspace)
    (workspace / "package.json").write_text(
        json.dumps({"name": name}) + "\n", encoding="utf-8"
    )
    scan_workspace(str(workspace), extractor="package_metadata")


def test_snapshot_checkpoints_committed_watch_state_and_preserves_watch_identity(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "before")
    with open_storage(tmp_path) as connection:
        connection.execute(
            """
            INSERT INTO change_events VALUES
            ('change-event:latest', 'modified', 'package.json', '[]', 1, 0, 1,
             0, '{}', '2026-09-27T12:00:00Z')
            """
        )
        connection.commit()

    checkpoint = snapshot_workspace(str(tmp_path))

    with open_storage(tmp_path) as connection:
        snapshots = list_snapshots(connection)
        stored = next(item for item in snapshots if item.id == checkpoint.snapshot_id)
    assert stored.content_sha256 == checkpoint.content_sha256
    assert stored.metadata["latest_change_event_id"] == "change-event:latest"
    assert stored.metadata["known_scan_scope"]["extractor"] == "package_metadata"


def test_snapshot_of_unchanged_index_reuses_content_digest(tmp_path: Path) -> None:
    _scan_package(tmp_path, "stable")
    with open_storage(tmp_path) as connection:
        previous = list_snapshots(connection)[-1]

    checkpoint = snapshot_workspace(str(tmp_path))

    assert checkpoint.content_sha256 == previous.content_sha256


def test_unchanged_checkpoints_keep_bounded_predecessor_metadata(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "stable")
    with open_storage(tmp_path) as connection:
        scan_snapshot = list_snapshots(connection)[-1]
        scan_payload = get_snapshot_payload(
            connection, scan_snapshot.content_sha256 or ""
        )
        connection.execute(
            """
            INSERT INTO change_events VALUES
            ('change-event:latest', 'modified', 'package.json', '[]', 1, 0, 1,
             0, '{}', '2026-09-27T12:00:00Z')
            """
        )
        connection.commit()
    assert scan_payload is not None

    checkpoints = [snapshot_workspace(str(tmp_path)) for _ in range(25)]

    with open_storage(tmp_path) as connection:
        snapshots = list_snapshots(connection)
        checkpoint_rows = snapshots[-25:]
        payload_count = connection.execute(
            "SELECT COUNT(*) FROM snapshot_payloads"
        ).fetchone()[0]

    assert [item.content_sha256 for item in checkpoint_rows] == [
        scan_snapshot.content_sha256
    ] * 25
    assert payload_count == 1
    assert [item.metadata["predecessor_snapshot_id"] for item in checkpoint_rows] == [
        scan_snapshot.id,
        *[checkpoint.snapshot_id for checkpoint in checkpoints[:-1]],
    ]
    sizes = [len(json.dumps(item.metadata, sort_keys=True)) for item in checkpoint_rows]
    assert max(sizes) - min(sizes) <= 1
    for item in checkpoint_rows:
        assert "prior_snapshot_metadata" not in item.metadata
        assert item.metadata["latest_change_event_id"] == "change-event:latest"
        assert item.metadata["known_scan_scope"] == scan_payload.scan_scope
        assert item.metadata["known_coverage"] == scan_payload.coverage


def test_checkpoint_reads_legacy_nested_metadata_without_republishing_chain(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "legacy-context")
    legacy_scope = {"extractor": "legacy"}
    legacy_coverage = {"discovered_file_count": 3}
    legacy_metadata = {
        "checkpoint": True,
        "prior_snapshot_metadata": {
            "prior_snapshot_metadata": {
                "known_scan_scope": legacy_scope,
                "known_coverage": legacy_coverage,
            }
        },
    }
    with open_storage(tmp_path) as connection:
        connection.execute(
            "INSERT INTO snapshots (id, workspace_id, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            (
                "legacy-checkpoint",
                "workspace",
                "9999-01-01T00:00:00Z",
                json.dumps(legacy_metadata),
            ),
        )
        connection.commit()

    checkpoint = snapshot_workspace(str(tmp_path))

    with open_storage(tmp_path) as connection:
        stored = next(
            item
            for item in list_snapshots(connection)
            if item.id == checkpoint.snapshot_id
        )
    assert stored.metadata["predecessor_snapshot_id"] == "legacy-checkpoint"
    assert stored.metadata["known_scan_scope"] == legacy_scope
    assert stored.metadata["known_coverage"] == legacy_coverage
    assert "prior_snapshot_metadata" not in stored.metadata


def test_snapshot_checkpoints_watch_modification_and_deletion_without_auto_snapshots(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "before")

    class WatchAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (WatchFileEvent("package.json"),)

    package = tmp_path / "package.json"
    package.write_text('{"name": "after"}\n', encoding="utf-8")
    watch_workspace(str(tmp_path), adapter=WatchAdapter())
    after_modification = snapshot_workspace(str(tmp_path))
    package.unlink()
    watch_workspace(str(tmp_path), adapter=WatchAdapter())
    after_deletion = snapshot_workspace(str(tmp_path))

    with open_storage(tmp_path) as connection:
        # The initial scan plus the two explicit checkpoints: watch does not publish.
        snapshots = list_snapshots(connection)
        assert len(snapshots) == 3
    assert any(item.id == after_modification.snapshot_id for item in snapshots)
    assert any(item.id == after_deletion.snapshot_id for item in snapshots)


def test_snapshot_requires_existing_index_without_creating_database(
    tmp_path: Path,
) -> None:
    with pytest.raises(StorageError, match="No initialized index"):
        snapshot_workspace(str(tmp_path))

    assert not database_path(tmp_path).exists()


def test_snapshot_rolls_back_on_publication_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _scan_package(tmp_path, "rollback")
    before = database_path(tmp_path).read_bytes()

    def fail(*_args: object, **_kwargs: object) -> str:
        raise ValueError("publication failed")

    monkeypatch.setattr("code_scout.pipeline.snapshot.create_snapshot", fail)
    with pytest.raises(ValueError, match="publication failed"):
        snapshot_workspace(str(tmp_path))

    assert database_path(tmp_path).read_bytes() == before


def test_snapshot_shared_command_path_and_formats_are_adapter_equivalent(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "adapter")
    command_spec = command(("snapshot", "create"))

    typed = execute_command(
        command_spec, CommandOptions(workspace=str(tmp_path), output_format="json")
    )
    fallback = run_command(
        command_spec,
        ["snapshot", "create", "--workspace", str(tmp_path), "--format", "json"],
    )

    assert set(json.loads(typed)) == {"snapshot_id", "content_sha256"}
    assert set(json.loads(fallback)) == {"snapshot_id", "content_sha256"}


def test_snapshot_inspection_lists_and_shows_bounded_retained_details(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "inspect")
    with open_storage(tmp_path) as connection:
        snapshot = list_snapshots(connection)[-1]

    list_command = command(("snapshot", "list"))
    show_command = command(("snapshot", "show"))
    listed = json.loads(
        execute_command(
            list_command,
            CommandOptions(workspace=str(tmp_path), output_format="json", limit=1),
        )
    )
    shown = json.loads(
        run_command(
            show_command,
            [
                "snapshot",
                "show",
                snapshot.id,
                "--workspace",
                str(tmp_path),
                "--format",
                "json",
                "--limit",
                "1",
            ],
        )
    )

    assert listed["snapshots"][0]["id"] == snapshot.id
    assert listed["total"] >= 1
    assert shown["id"] == snapshot.id
    assert shown["content_sha256"] == snapshot.content_sha256
    assert shown["counts"]["active_facts"] >= 1
    assert shown["facts_returned"] == 1
    assert shown["facts_truncated"] == (shown["facts_total"] > 1)


def test_snapshot_inspection_supports_legacy_metadata_only_snapshot(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "legacy")
    with open_storage(tmp_path) as connection:
        connection.execute(
            "INSERT INTO snapshots (id, workspace_id, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            ("legacy", "workspace", "2020-01-01T00:00:00Z", '{"legacy": true}'),
        )
        connection.commit()

    output = json.loads(
        execute_command(
            command(("snapshot", "show")),
            CommandOptions(
                workspace=str(tmp_path), snapshot_id="legacy", output_format="json"
            ),
        )
    )
    assert output["content_status"] == "historical_content_unavailable"
    assert "counts" not in output


def test_changes_compares_snapshots_read_only_with_adapter_and_truncation_parity(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "before")
    with open_storage(tmp_path) as connection:
        before = list_snapshots(connection)[-1]
    (tmp_path / "package.json").write_text('{"name": "after"}\n', encoding="utf-8")
    scan_workspace(str(tmp_path), extractor="package_metadata")
    with open_storage(tmp_path) as connection:
        after = list_snapshots(connection)[-1]
    database_before = database_path(tmp_path).read_bytes()

    options = CommandOptions(
        workspace=str(tmp_path),
        from_snapshot_id=before.id,
        to_snapshot_id=after.id,
        output_format="json",
        limit=1,
    )
    typed = json.loads(execute_command(command(("changes",)), options))
    fallback = json.loads(
        run_command(
            command(("changes",)),
            [
                "changes",
                "--workspace",
                str(tmp_path),
                "--from",
                before.id,
                "--to",
                after.id,
                "--format",
                "json",
                "--limit",
                "1",
            ],
        )
    )

    assert typed == fallback
    assert typed["counts"]["modified"] >= 1
    assert typed["total"] >= typed["returned"]
    assert typed["truncated"] == (typed["total"] > typed["returned"])
    assert database_path(tmp_path).read_bytes() == database_before


def test_changes_snapshot_comparison_errors_and_self_diff(tmp_path: Path) -> None:
    _scan_package(tmp_path, "snapshots")
    with open_storage(tmp_path) as connection:
        snapshot = list_snapshots(connection)[-1]

    with pytest.raises(CommandUsageError, match="both --from"):
        execute_command(
            command(("changes",)),
            CommandOptions(workspace=str(tmp_path), from_snapshot_id=snapshot.id),
        )
    with pytest.raises(StorageError, match="Snapshot not found"):
        execute_command(
            command(("changes",)),
            CommandOptions(
                workspace=str(tmp_path),
                from_snapshot_id=snapshot.id,
                to_snapshot_id="missing",
            ),
        )

    self_diff = json.loads(
        execute_command(
            command(("changes",)),
            CommandOptions(
                workspace=str(tmp_path),
                from_snapshot_id=snapshot.id,
                to_snapshot_id=snapshot.id,
                output_format="json",
            ),
        )
    )
    assert self_diff["counts"] == {"added": 0, "removed": 0, "modified": 0}


def test_changes_rejects_legacy_and_incompatible_snapshot_workspaces(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "errors")
    with open_storage(tmp_path) as connection:
        available = list_snapshots(connection)[-1]
        connection.execute(
            "INSERT INTO snapshots (id, workspace_id, created_at, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-diff", "workspace", "2020-01-01T00:00:00Z", "{}"),
        )
        upsert_workspace(
            connection,
            id="other-workspace",
            name="other workspace",
            root_path=str(tmp_path),
        )
        create_snapshot(
            connection,
            Snapshot("other-workspace-snapshot", "other-workspace", "2020-01-02"),
            payload=SnapshotPayload((), {}, (), (), (), (), (), {}, {}),
        )
        connection.commit()

    with pytest.raises(StorageError, match="historical content unavailable"):
        execute_command(
            command(("changes",)),
            CommandOptions(
                workspace=str(tmp_path),
                from_snapshot_id="legacy-diff",
                to_snapshot_id=available.id,
            ),
        )
    with pytest.raises(StorageError, match="incompatible workspace identities"):
        execute_command(
            command(("changes",)),
            CommandOptions(
                workspace=str(tmp_path),
                from_snapshot_id=available.id,
                to_snapshot_id="other-workspace-snapshot",
            ),
        )


def test_evidence_show_reads_retained_bytes_after_live_source_is_deleted(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "retained")
    with open_storage(tmp_path) as connection:
        snapshot = list_snapshots(connection)[-1]
        payload = get_snapshot_payload(connection, snapshot.content_sha256 or "")
    assert payload is not None
    revision_id = payload.revision_references[0]["id"]
    (tmp_path / "package.json").unlink()

    output = json.loads(
        execute_command(
            command(("evidence", "show")),
            CommandOptions(
                workspace=str(tmp_path),
                revision_id=revision_id,
                output_format="json",
                start_line=1,
                end_line=1,
            ),
        )
    )
    assert output["repository_id"] == "workspace"
    assert output["path"] == "package.json"
    assert len(output["content_sha256"]) == 64
    assert "retained" in output["text"]


def test_evidence_show_rejects_corrupted_retained_digest(tmp_path: Path) -> None:
    _scan_package(tmp_path, "integrity")
    with open_storage(tmp_path) as connection:
        snapshot = list_snapshots(connection)[-1]
        payload = get_snapshot_payload(connection, snapshot.content_sha256 or "")
        assert payload is not None
        revision_id = payload.revision_references[0]["id"]
        digest = payload.revision_references[0]["content_sha256"]
        connection.execute("DROP TRIGGER prevent_source_blob_mutation")
        connection.execute(
            "UPDATE source_blobs SET bytes = zeroblob(size_bytes) "
            "WHERE content_sha256 = ?",
            (digest,),
        )
        connection.commit()

    with pytest.raises(StorageError, match="integrity check failed"):
        execute_command(
            command(("evidence", "show")),
            CommandOptions(workspace=str(tmp_path), revision_id=revision_id),
        )


@pytest.mark.parametrize("limit", [0, -1])
def test_snapshot_inspection_rejects_invalid_limits(tmp_path: Path, limit: int) -> None:
    _scan_package(tmp_path, "limits")
    with pytest.raises(CommandUsageError, match="--limit must be at least 1"):
        execute_command(
            command(("snapshot", "list")),
            CommandOptions(workspace=str(tmp_path), limit=limit),
        )


def test_inspection_missing_ids_and_invalid_ranges_have_expected_errors(
    tmp_path: Path,
) -> None:
    _scan_package(tmp_path, "errors")
    with pytest.raises(StorageError, match="Snapshot not found"):
        execute_command(
            command(("snapshot", "show")),
            CommandOptions(workspace=str(tmp_path), snapshot_id="missing"),
        )
    with pytest.raises(CommandUsageError, match="greater than or equal"):
        execute_command(
            command(("evidence", "show")),
            CommandOptions(
                workspace=str(tmp_path), revision_id="missing", start_line=2, end_line=1
            ),
        )


def test_inspection_missing_index_does_not_create_storage(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="No initialized index"):
        execute_command(
            command(("snapshot", "list")), CommandOptions(workspace=str(tmp_path))
        )
    assert not database_path(tmp_path).exists()
