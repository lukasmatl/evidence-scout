from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_scout.pipeline.incremental import replace_changed_file_active_facts
from code_scout.pipeline.scan import scan_workspace
from code_scout.storage import database_path, open_storage
from code_scout.workspace.config import create_workspace_config
from code_scout.workspace.status import workspace_status


def test_status_reports_missing_without_creating_workspace_state(
    tmp_path: Path,
) -> None:
    report = workspace_status(tmp_path).to_dict()

    assert report["schema_version"] == "1"
    assert report["workspace"] == {
        "id": None,
        "name": None,
        "root_path": str(tmp_path),
        "repositories": [],
    }
    assert report["index"]["state"] == "missing"
    assert report["index"]["latest_scan_at"] is None
    assert report["freshness"]["state"] == "unknown"
    assert report["recommendations"] == [f"code-scout init --workspace {tmp_path}"]
    assert not (tmp_path / ".code-scout").exists()


def test_status_reports_initialized_workspace_as_unscanned(tmp_path: Path) -> None:
    create_workspace_config(tmp_path)

    report = workspace_status(tmp_path).to_dict()

    assert report["workspace"]["id"] == "workspace"
    assert report["index"]["state"] == "unscanned"
    assert report["freshness"]["state"] == "unknown"
    assert report["recommendations"] == [f"code-scout scan --workspace {tmp_path}"]
    assert not database_path(tmp_path).exists()


def test_status_reports_database_without_snapshot_as_unscanned(tmp_path: Path) -> None:
    create_workspace_config(tmp_path)
    with open_storage(tmp_path):
        pass
    before = database_path(tmp_path).read_bytes()

    report = workspace_status(tmp_path).to_dict()

    assert report["index"]["state"] == "unscanned"
    assert database_path(tmp_path).read_bytes() == before


def test_status_uses_hashes_not_changed_timestamps_for_freshness(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    source = tmp_path / "README.md"
    source.write_text("same content\n", encoding="utf-8")
    scan_workspace(str(tmp_path))
    original = source.stat().st_mtime
    os.utime(source, (original + 60, original + 60))

    report = workspace_status(tmp_path).to_dict()

    assert report["index"]["state"] == "ready"
    assert report["index"]["latest_scan_at"] is not None
    assert report["freshness"] == {
        "state": "fresh",
        "coverage": "complete",
        "coverage_reasons": [],
    }
    assert report["changes"]["counts"] == {
        "added": 0,
        "modified": 0,
        "deleted": 0,
        "unreadable": 0,
    }


def test_status_reports_sorted_repository_relative_changes_and_exclusions(
    tmp_path: Path,
) -> None:
    _write_multi_repository_config(tmp_path)
    api = tmp_path / "services" / "api"
    web = tmp_path / "apps" / "web"
    api.mkdir(parents=True)
    web.mkdir(parents=True)
    (api / "changed.py").write_text("before\n", encoding="utf-8")
    (api / "deleted.py").write_text("delete\n", encoding="utf-8")
    (web / "kept.ts").write_text("keep\n", encoding="utf-8")
    scan_workspace(str(tmp_path))

    (api / "changed.py").write_text("after\n", encoding="utf-8")
    (api / "deleted.py").unlink()
    (web / "z_added.ts").write_text("z\n", encoding="utf-8")
    (web / "a_added.ts").write_text("a\n", encoding="utf-8")
    excluded = web / "generated"
    excluded.mkdir()
    (excluded / "ignored.ts").write_text("ignored\n", encoding="utf-8")

    report = workspace_status(tmp_path).to_dict()

    assert report["freshness"]["state"] == "stale"
    assert report["changes"]["modified"] == [
        {"repository_id": "api", "path": "changed.py"}
    ]
    assert report["changes"]["deleted"] == [
        {"repository_id": "api", "path": "deleted.py"}
    ]
    assert report["changes"]["added"] == [
        {"repository_id": "web", "path": "a_added.ts"},
        {"repository_id": "web", "path": "z_added.ts"},
    ]
    assert all(
        path["path"] != "generated/ignored.ts"
        for kind in ("added", "modified", "deleted", "unreadable")
        for path in report["changes"][kind]
    )


def test_status_uses_file_state_refreshed_by_watch_updates(tmp_path: Path) -> None:
    create_workspace_config(tmp_path)
    source = tmp_path / "README.md"
    source.write_text("before\n", encoding="utf-8")
    scan_workspace(str(tmp_path))
    latest_scan_at = workspace_status(tmp_path).to_dict()["index"]["latest_scan_at"]
    source.write_text("after\n", encoding="utf-8")

    replace_changed_file_active_facts(
        str(tmp_path),
        changed_file_paths=("README.md",),
        selected_extractors=("docs",),
    )
    report = workspace_status(tmp_path).to_dict()

    assert report["freshness"]["state"] == "fresh"
    assert report["changes"]["counts"]["modified"] == 0
    assert report["index"]["latest_scan_at"] == latest_scan_at


def test_status_reports_unknown_coverage_for_partial_scan_and_missing_hash(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "README.md").write_text("content\n", encoding="utf-8")
    scan_workspace(str(tmp_path), extractor="docs")

    partial = workspace_status(tmp_path).to_dict()
    assert partial["freshness"]["state"] == "unknown"
    assert partial["freshness"]["coverage"] == "unknown"
    assert partial["freshness"]["coverage_reasons"][0]["code"] == ("partial_scan_scope")

    scan_workspace(str(tmp_path))
    with sqlite3.connect(database_path(tmp_path)) as connection:
        connection.execute("UPDATE files SET content_hash = NULL")
        connection.commit()

    missing_hash = workspace_status(tmp_path).to_dict()
    assert missing_hash["freshness"]["state"] == "unknown"
    assert missing_hash["freshness"]["coverage_reasons"] == [
        {
            "code": "missing_index_hashes",
            "message": "1 indexed file(s) have no content hash.",
        }
    ]


@pytest.mark.parametrize(
    "scope_metadata",
    [
        {},
        {"selection_type": "", "selected_extractors": []},
        {"selection_type": "all_enabled", "selected_extractors": [42]},
    ],
    ids=("missing", "empty", "malformed"),
)
def test_status_keeps_unknown_historical_scan_scope_visible_in_freshness(
    tmp_path: Path,
    scope_metadata: dict[str, object],
) -> None:
    create_workspace_config(tmp_path)
    source = tmp_path / "README.md"
    source.write_text("same content\n", encoding="utf-8")
    scan_workspace(str(tmp_path))
    with sqlite3.connect(database_path(tmp_path)) as connection:
        metadata = json.loads(
            connection.execute("SELECT metadata_json FROM snapshots").fetchone()[0]
        )
        metadata.pop("selection_type", None)
        metadata.pop("selection", None)
        metadata.pop("selected_extractors", None)
        metadata.update(scope_metadata)
        connection.execute(
            "UPDATE snapshots SET metadata_json = ?",
            (json.dumps(metadata, sort_keys=True),),
        )
        connection.commit()
    database_before = database_path(tmp_path).read_bytes()

    report = workspace_status(tmp_path)
    payload = report.to_dict()

    assert payload["index"]["state"] == "ready"
    assert payload["coverage"]["scan_scope"]["state"] == "unknown"
    assert payload["freshness"] == {
        "state": "unknown",
        "coverage": "unknown",
        "coverage_reasons": [
            {
                "code": "scan_scope_unknown",
                "message": (
                    "Historical scan metadata does not identify the extraction scope."
                ),
                "remediation": f"code-scout scan --workspace {tmp_path}",
            }
        ],
    }
    assert payload["recommendations"] == [f"code-scout scan --workspace {tmp_path}"]
    assert "Freshness: unknown" in report.render("text")
    assert "Coverage: unknown" in report.render("text")
    assert "Historical scan metadata" in report.render("text")
    assert database_path(tmp_path).read_bytes() == database_before

    source.write_text("changed content\n", encoding="utf-8")
    stale = workspace_status(tmp_path).to_dict()

    assert stale["freshness"]["state"] == "stale"
    assert stale["freshness"]["coverage"] == "unknown"
    assert stale["freshness"]["coverage_reasons"][0]["code"] == ("scan_scope_unknown")


def test_known_changes_remain_stale_with_unknown_scope_coverage(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    modified = tmp_path / "modified.py"
    deleted = tmp_path / "deleted.py"
    modified.write_text("before\n", encoding="utf-8")
    deleted.write_text("delete me\n", encoding="utf-8")
    scan_workspace(str(tmp_path))
    with sqlite3.connect(database_path(tmp_path)) as connection:
        connection.execute("UPDATE snapshots SET metadata_json = '{}' ")
        connection.commit()

    modified.write_text("after\n", encoding="utf-8")
    deleted.unlink()
    (tmp_path / "added.py").write_text("new\n", encoding="utf-8")
    report = workspace_status(tmp_path).to_dict()

    assert report["freshness"]["state"] == "stale"
    assert report["freshness"]["coverage"] == "unknown"
    assert report["freshness"]["coverage_reasons"][0]["code"] == ("scan_scope_unknown")
    assert report["changes"]["counts"] == {
        "added": 1,
        "modified": 1,
        "deleted": 1,
        "unreadable": 0,
    }


def test_status_reports_unreadable_workspace_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_workspace_config(tmp_path)
    source = tmp_path / "README.md"
    source.write_text("content\n", encoding="utf-8")
    scan_workspace(str(tmp_path))

    def fail_hash(path: Path) -> str:
        raise PermissionError(f"cannot read {path}")

    monkeypatch.setattr("code_scout.discovery.files._content_hash", fail_hash)
    report = workspace_status(tmp_path).to_dict()

    assert report["index"]["state"] == "ready"
    assert report["freshness"]["state"] == "unknown"
    assert report["changes"]["unreadable"] == [
        {"repository_id": "workspace", "path": "README.md"}
    ]
    assert report["changes"]["counts"]["unreadable"] == 1


def test_status_reports_unreadable_and_incompatible_index_states(
    tmp_path: Path,
) -> None:
    unreadable_workspace = tmp_path / "unreadable"
    unreadable_workspace.mkdir()
    create_workspace_config(unreadable_workspace)
    database_path(unreadable_workspace).write_text("not sqlite", encoding="utf-8")

    unreadable = workspace_status(unreadable_workspace).to_dict()
    assert unreadable["index"]["state"] == "unreadable"
    assert unreadable["freshness"]["state"] == "unknown"

    incompatible_workspace = tmp_path / "incompatible"
    incompatible_workspace.mkdir()
    create_workspace_config(incompatible_workspace)
    with open_storage(incompatible_workspace) as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()

    incompatible = workspace_status(incompatible_workspace).to_dict()
    assert incompatible["index"]["state"] == "incompatible"
    assert incompatible["index"]["schema_version"] == "999"


def test_status_json_is_versioned_and_text_is_default(tmp_path: Path) -> None:
    create_workspace_config(tmp_path)

    report = workspace_status(tmp_path)
    payload = json.loads(report.render("json"))

    assert payload == report.to_dict()
    assert report.render().startswith("Workspace status:\n")
    assert "Index state: unscanned" in report.render()


def test_status_separates_discovered_files_from_observed_extraction(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Notes\n", encoding="utf-8")
    scan_workspace(str(tmp_path))

    report = workspace_status(tmp_path).to_dict()
    coverage = report["coverage"]

    assert coverage["file_discovery"] == {
        "state": "observed",
        "file_count": 2,
        "languages": [
            {"language": "Markdown", "file_count": 1},
            {"language": "Python", "file_count": 1},
        ],
        "unknown_language_file_count": 0,
        "meaning": (
            "Files were discovered; this does not prove symbol or call extraction."
        ),
    }
    assert (
        coverage["extractors"]["observed_evidence"]["by_extractor"]["file_discovery"]
        == 2
    )
    assert coverage["symbol_extraction"]["symbol_count"] >= 0
    assert (
        "file counts do not imply call coverage"
        in coverage["relationship_extraction"]["meaning"]
    )
    text = workspace_status(tmp_path).render()
    assert "Discovered files: 2" in text
    assert "Extracted symbols:" in text
    assert "Extracted relationships:" in text


def test_status_reports_missing_grammar_as_unavailable_with_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    scan_workspace(str(tmp_path))
    monkeypatch.setattr(
        "code_scout.workspace.status.load_grammar",
        lambda _language: SimpleNamespace(
            available=False,
            missing_reason="Missing optional package tree-sitter-python",
        ),
    )

    coverage = workspace_status(tmp_path).to_dict()["coverage"]

    assert coverage["grammar_support"] == [
        {
            "language": "Python",
            "grammar_language": "Python",
            "file_count": 1,
            "package": "tree-sitter-python",
            "installation": "missing",
            "state": "unavailable",
            "reason": "Missing optional package tree-sitter-python",
        }
    ]
    assert coverage["gaps"][0]["code"] == "missing_grammars"
    assert "uv sync --extra tree-sitter-all" in coverage["gaps"][0]["remediation"]


def test_status_reports_disabled_symbol_extractors(tmp_path: Path) -> None:
    create_workspace_config(tmp_path)
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace(
            "  tree_sitter:\n    enabled: true",
            "  tree_sitter:\n    enabled: false",
        ).replace(
            "  symbol_extraction:\n    enabled: true",
            "  symbol_extraction:\n    enabled: false",
        ),
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")
    scan_workspace(str(tmp_path))

    coverage = workspace_status(tmp_path).to_dict()["coverage"]
    states = {item["name"]: item["state"] for item in coverage["extractors"]["states"]}

    assert "tree_sitter" in coverage["extractors"]["disabled"]
    assert "symbol_extraction" in coverage["extractors"]["disabled"]
    assert states["tree_sitter"] == "disabled"
    assert states["symbol_extraction"] == "disabled"
    assert coverage["grammar_support"][0]["state"] == "disabled"
    assert any(gap["code"] == "symbol_extraction_disabled" for gap in coverage["gaps"])


def test_status_describes_partial_profile_and_unknown_historical_scope(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    (tmp_path / "README.md").write_text("# Notes\n", encoding="utf-8")
    scan_workspace(str(tmp_path), profile="docs")

    partial = workspace_status(tmp_path).to_dict()["coverage"]
    assert partial["scan_scope"] == {
        "state": "partial",
        "metadata": "available",
        "selection_type": "profile",
        "selection": "docs",
        "selected_extractors": ["adr", "docs", "openapi"],
    }
    assert partial["gaps"][0]["code"] == "partial_scan_scope"

    with sqlite3.connect(database_path(tmp_path)) as connection:
        connection.execute("UPDATE snapshots SET metadata_json = '{}'")
        connection.commit()

    historical = workspace_status(tmp_path).to_dict()["coverage"]
    assert historical["scan_scope"]["state"] == "unknown"
    assert historical["scan_scope"]["metadata"] == "unavailable"
    assert historical["gaps"][0]["code"] == "scan_scope_unknown"
    assert "code-scout scan" in historical["gaps"][0]["remediation"]


def test_status_reports_empty_scanned_workspace_without_implying_extraction(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    scan_workspace(str(tmp_path))

    coverage = workspace_status(tmp_path).to_dict()["coverage"]

    assert coverage["scan_scope"]["state"] == "full"
    assert coverage["file_discovery"]["file_count"] == 0
    assert coverage["file_discovery"]["languages"] == []
    assert coverage["symbol_extraction"] == {
        "state": "unknown",
        "symbol_count": 0,
        "meaning": "Persisted symbols observed in the resolved graph.",
    }
    assert coverage["relationship_extraction"]["state"] == "unknown"
    assert coverage["grammar_support"] == []


def _write_multi_repository_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: platform
  name: Platform
repositories:
  - id: web
    name: Web
    path: apps/web
  - id: api
    name: API
    path: services/api
paths:
  docs: []
  adr: []
  openapi: []
  asyncapi: []
  infra: []
  package_metadata: []
  exclude:
    - generated
""".lstrip(),
        encoding="utf-8",
    )
