"""Read-only inspection of retained snapshots and source revisions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from code_scout.core.errors import CommandUsageError, StorageError
from code_scout.pipeline.snapshot_diff import (
    SnapshotComparisonError,
    compare_snapshot_payloads,
)
from code_scout.storage import (
    database_path,
    get_snapshot_payload,
    get_source_blob,
    get_source_revision,
    list_snapshots,
)
from code_scout.storage.schema import SCHEMA_VERSION

DEFAULT_LIST_LIMIT = 20
DEFAULT_DETAIL_LIMIT = 100


def inspect_snapshots(
    workspace_path: str = ".", *, limit: int = DEFAULT_LIST_LIMIT
) -> dict[str, Any]:
    """List retained snapshot occurrences without opening or migrating storage."""
    _validate_limit(limit)
    with _open_read_only_index(database_path(workspace_path)) as connection:
        snapshots = list_snapshots(connection)
    items = [_snapshot_summary(snapshot) for snapshot in reversed(snapshots)]
    return _bounded(items, limit, key="snapshots")


def inspect_snapshot(
    workspace_path: str,
    snapshot_id: str,
    *,
    limit: int = DEFAULT_DETAIL_LIMIT,
) -> dict[str, Any]:
    """Read one snapshot's metadata and bounded historical detail."""
    _validate_limit(limit)
    with _open_read_only_index(database_path(workspace_path)) as connection:
        snapshot = next(
            (item for item in list_snapshots(connection) if item.id == snapshot_id),
            None,
        )
        if snapshot is None:
            raise StorageError(
                f"Snapshot not found: {snapshot_id}.",
                "Run `code-scout snapshot list` to find retained snapshot IDs.",
            )
        result: dict[str, Any] = _snapshot_summary(snapshot)
        if snapshot.content_sha256 is None:
            return result
        payload = get_snapshot_payload(connection, snapshot.content_sha256)
        if payload is None:  # pragma: no cover - incompatible databases are rejected
            raise StorageError(
                f"Snapshot payload is unavailable: {snapshot.content_sha256}.",
                "Run `code-scout scan --workspace PATH` to create a new snapshot.",
            )

    payload_dict = payload.to_dict()
    entities = payload_dict["resolved_entities"]
    result["counts"] = {
        "active_facts": len(payload.active_facts),
        "raw_evidence": len(payload.raw_evidence),
        "revision_references": len(payload.revision_references),
        "resolved_edges": len(payload.resolved_edges),
        "resolved_entities": {
            name: len(values) for name, values in sorted(entities.items())
        },
    }
    result["scan_scope"] = payload_dict["scan_scope"]
    result["coverage"] = payload_dict["coverage"]
    result.update(_bounded(list(payload.active_facts), limit, key="facts"))
    result.update(_bounded(list(payload.raw_evidence), limit, key="evidence"))
    return result


def inspect_snapshot_diff(
    workspace_path: str,
    from_snapshot_id: str,
    to_snapshot_id: str,
) -> dict[str, Any]:
    """Compare two retained snapshots through the read-only history boundary."""
    with _open_read_only_index(database_path(workspace_path)) as connection:
        snapshots = {item.id: item for item in list_snapshots(connection)}
        before = snapshots.get(from_snapshot_id)
        after = snapshots.get(to_snapshot_id)
        if before is None:
            raise StorageError(
                f"Snapshot not found: {from_snapshot_id}.",
                "Run `code-scout snapshot list` to find retained snapshot IDs.",
            )
        if after is None:
            raise StorageError(
                f"Snapshot not found: {to_snapshot_id}.",
                "Run `code-scout snapshot list` to find retained snapshot IDs.",
            )
        if before.workspace_id != after.workspace_id:
            raise StorageError(
                "Snapshots have incompatible workspace identities: "
                f"{before.workspace_id!r} and {after.workspace_id!r}.",
                "Choose snapshots created for the same workspace identity.",
            )
        if from_snapshot_id == to_snapshot_id:
            return _empty_snapshot_diff(before)
        before_payload = (
            get_snapshot_payload(connection, before.content_sha256)
            if before.content_sha256 is not None
            else None
        )
        after_payload = (
            get_snapshot_payload(connection, after.content_sha256)
            if after.content_sha256 is not None
            else None
        )
    try:
        return compare_snapshot_payloads(
            before_payload,
            after_payload,
            before_snapshot_id=before.id,
            before_content_sha256=before.content_sha256,
            after_snapshot_id=after.id,
            after_content_sha256=after.content_sha256,
        )
    except SnapshotComparisonError as error:
        raise StorageError(
            str(error),
            "Choose snapshots with retained historical content.",
        ) from error


def render_snapshot_diff(
    payload: dict[str, Any], output_format: str, *, limit: int = DEFAULT_DETAIL_LIMIT
) -> str:
    """Render bounded comparison details while retaining complete summary counts."""
    _validate_limit(limit)
    bounded = _bounded_snapshot_diff(payload, limit)
    if output_format == "json":
        return json.dumps(bounded, indent=2, sort_keys=True, ensure_ascii=False)
    if output_format != "text":
        raise CommandUsageError(
            f"Unsupported changes format {output_format!r}.",
            "Use `--format text` or `--format json`.",
        )
    return _snapshot_diff_text(bounded)


def inspect_evidence(
    workspace_path: str,
    revision_id: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
) -> dict[str, Any]:
    """Return retained source bytes as text, independent of the live workspace."""
    _validate_range(start_line, end_line)
    with _open_read_only_index(database_path(workspace_path)) as connection:
        revision = get_source_revision(connection, revision_id)
        if revision is None:
            raise StorageError(
                f"Source revision not found: {revision_id}.",
                "Use a revision ID retained in snapshot or citation evidence.",
            )
        try:
            blob = get_source_blob(connection, revision.content_sha256)
        except ValueError as error:
            raise StorageError(
                f"Retained source integrity check failed for revision: {revision_id}.",
                "The retained source bytes no longer match their content digest.",
            ) from error
        if blob is None:  # pragma: no cover - source revision foreign key prevents this
            raise StorageError(
                f"Retained source bytes are unavailable for revision: {revision_id}.",
                "Run `code-scout scan --workspace PATH` to capture new "
                "source evidence.",
            )

    lines = blob.bytes.splitlines(keepends=True)
    selected_start = start_line or 1
    selected_end = end_line if end_line is not None else len(lines)
    if lines and (
        selected_start > len(lines) or (end_line is not None and end_line > len(lines))
    ):
        raise CommandUsageError(
            f"Requested range must not exceed retained line count ({len(lines)}).",
            "Pass a range within the retained source text.",
        )
    if not lines and (start_line is not None or end_line is not None):
        raise CommandUsageError(
            "Cannot select a line range from empty retained source text.",
            "Omit --start-line and --end-line for an empty retained file.",
        )
    selected = lines[selected_start - 1 : selected_end]
    try:
        text = b"".join(selected).decode("utf-8")
    except UnicodeDecodeError as error:
        raise StorageError(
            f"Retained source revision is not UTF-8 text: {revision_id}.",
            "This revision cannot be rendered as text.",
        ) from error
    return {
        "revision_id": revision.id,
        "repository_id": revision.repository_id,
        "path": revision.path,
        "content_sha256": revision.content_sha256,
        "line_count": len(lines),
        "start_line": selected_start,
        "end_line": selected_end,
        "text": text,
    }


def render_inspection(payload: dict[str, Any], output_format: str, *, kind: str) -> str:
    if output_format == "json":
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if output_format != "text":
        raise CommandUsageError(
            f"Unsupported {kind} format {output_format!r}.",
            "Use `--format text` or `--format json`.",
        )
    if kind == "snapshot list":
        lines = [
            f"Snapshots: {payload['total']} total; {payload['returned']} shown"
            + (" (truncated)" if payload["truncated"] else ""),
        ]
        for item in payload["snapshots"]:
            lines.append(
                f"- {item['id']} | {item['created_at']} | {item['content_status']}"
            )
        return "\n".join(lines)
    if kind == "snapshot show":
        lines = [
            f"Snapshot: {payload['id']}",
            f"Workspace: {payload['workspace_id']}",
            f"Created: {payload['created_at']}",
            f"Content: {payload['content_status']}",
            "Metadata: "
            + json.dumps(payload["metadata"], sort_keys=True, ensure_ascii=False),
        ]
        if payload["content_sha256"]:
            lines.append(f"Content digest: {payload['content_sha256']}")
        if "counts" not in payload:
            return "\n".join(lines)
        lines.extend(
            [
                "Counts:",
                f"  Facts: {payload['counts']['active_facts']}",
                f"  Evidence: {payload['counts']['raw_evidence']}",
                f"  Revisions: {payload['counts']['revision_references']}",
                f"  Edges: {payload['counts']['resolved_edges']}",
                "Facts: "
                f"{payload['facts_total']} total; {payload['facts_returned']} shown"
                + (" (truncated)" if payload["facts_truncated"] else ""),
                "Evidence: "
                f"{payload['evidence_total']} total; "
                f"{payload['evidence_returned']} shown"
                + (" (truncated)" if payload["evidence_truncated"] else ""),
            ]
        )
        lines.extend(_snapshot_detail_lines("Facts", payload["facts"]))
        lines.extend(_snapshot_detail_lines("Evidence", payload["evidence"]))
        return "\n".join(lines)
    return "\n".join(
        [
            f"Revision: {payload['revision_id']}",
            f"Repository: {payload['repository_id']}",
            f"Path: {payload['path']}",
            f"Content digest: {payload['content_sha256']}",
            "Lines: "
            f"{payload['start_line']}-{payload['end_line']} of {payload['line_count']}",
            "",
            payload["text"],
        ]
    )


def _open_read_only_index(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise StorageError(
            f"No initialized index exists at {path.resolve()}.",
            "Run `code-scout scan --workspace PATH` before inspecting "
            "retained history.",
        )
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.Error as error:
        if "connection" in locals():
            connection.close()
        raise StorageError(
            f"Workspace index is unreadable: {path.resolve()}.",
            "Check the database permissions and integrity, then retry.",
        ) from error
    required = {"snapshots", "snapshot_payloads", "source_blobs", "source_revisions"}
    if version != SCHEMA_VERSION or not required <= tables:
        connection.close()
        raise StorageError(
            f"Index at {path.resolve()} is not compatible with retained history.",
            "Run `code-scout scan --workspace PATH` to initialize or upgrade "
            "the index.",
        )
    return connection


def _snapshot_summary(snapshot: Any) -> dict[str, Any]:
    return asdict(snapshot)


def _snapshot_detail_lines(label: str, records: list[Any]) -> list[str]:
    if not records:
        return []
    lines = [f"{label} detail:"]
    for record in records:
        if not isinstance(record, dict):
            lines.append(f"- {record}")
            continue
        identifier = record.get("id", "unknown")
        kind = record.get("kind") or record.get("owner_entity_type") or "unknown"
        source = record.get("source_file", "unknown")
        lines.append(f"- {identifier} | {kind} | {source}")
    return lines


def _empty_snapshot_diff(snapshot: Any) -> dict[str, Any]:
    reference = {"id": snapshot.id, "content_sha256": snapshot.content_sha256}
    return {
        "before_snapshot": reference,
        "after_snapshot": dict(reference),
        "added": [],
        "removed": [],
        "modified": [],
        "scope_difference": None,
        "coverage_difference": None,
        "uncertainties": [],
        "counts": {"added": 0, "removed": 0, "modified": 0},
    }


def _bounded_snapshot_diff(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    result = dict(payload)
    total = 0
    returned = 0
    for key in ("added", "removed", "modified"):
        items = list(payload[key])
        shown = items[:limit]
        result[key] = shown
        result[f"{key}_total"] = len(items)
        result[f"{key}_returned"] = len(shown)
        result[f"{key}_truncated"] = len(items) > limit
        total += len(items)
        returned += len(shown)
    result["total"] = total
    result["returned"] = returned
    result["truncated"] = returned < total
    return result


def _snapshot_diff_text(payload: dict[str, Any]) -> str:
    before = payload["before_snapshot"]
    after = payload["after_snapshot"]
    counts = payload["counts"]
    lines = [
        f"Snapshot changes: {before['id']} -> {after['id']}",
        "Counts:",
        f"  Added: {counts['added']}",
        f"  Removed: {counts['removed']}",
        f"  Modified: {counts['modified']}",
    ]
    for key in ("added", "removed", "modified"):
        label = key.capitalize()
        lines.append(
            f"{label}: {payload[f'{key}_total']} total; "
            f"{payload[f'{key}_returned']} shown"
            + (" (truncated)" if payload[f"{key}_truncated"] else "")
        )
        for item in payload[key]:
            lines.extend(_snapshot_diff_detail_lines(key, item))
    if payload["scope_difference"] is not None:
        lines.append(
            "Scan scope changed: " + _compact_json(payload["scope_difference"])
        )
    if payload["coverage_difference"] is not None:
        lines.append(
            "Coverage changed: " + _compact_json(payload["coverage_difference"])
        )
    if payload["uncertainties"]:
        lines.append("Coverage warnings:")
        lines.extend(f"- {warning['message']}" for warning in payload["uncertainties"])
    return "\n".join(lines)


def _snapshot_diff_detail_lines(kind: str, item: Any) -> list[str]:
    if not isinstance(item, dict):
        return [f"- {item}"]
    lines = [f"- {_compact_json(item.get('identity'))}"]
    if kind == "added":
        lines.append("  After evidence: " + _compact_json(item.get("after")))
    elif kind == "removed":
        lines.append("  Before evidence: " + _compact_json(item.get("before")))
    else:
        lines.append("  Before evidence: " + _compact_json(item.get("before")))
        lines.append("  After evidence: " + _compact_json(item.get("after")))
        changes = item.get("changes", {})
        if isinstance(changes, dict) and changes.get("assessment_transitions"):
            lines.append(
                "  Assessment transitions: "
                + _compact_json(changes["assessment_transitions"])
            )
    return lines


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _bounded(items: list[Any], limit: int, *, key: str) -> dict[str, Any]:
    return {
        key: items[:limit],
        f"{key}_total": len(items),
        f"{key}_returned": min(len(items), limit),
        f"{key}_truncated": len(items) > limit,
        # List uses generic keys for a concise stable contract.
        **(
            {
                "total": len(items),
                "returned": min(len(items), limit),
                "truncated": len(items) > limit,
            }
            if key == "snapshots"
            else {}
        ),
    }


def _validate_limit(limit: int) -> None:
    if limit < 1:
        raise CommandUsageError(
            "--limit must be at least 1.", "Pass a positive integer."
        )


def _validate_range(start_line: int | None, end_line: int | None) -> None:
    if start_line is not None and start_line < 1:
        raise CommandUsageError(
            "--start-line must be at least 1.", "Pass a positive line number."
        )
    if end_line is not None and end_line < 1:
        raise CommandUsageError(
            "--end-line must be at least 1.", "Pass a positive line number."
        )
    if start_line is not None and end_line is not None and end_line < start_line:
        raise CommandUsageError(
            "--end-line must be greater than or equal to --start-line.",
            "Pass an ordered inclusive line range.",
        )
