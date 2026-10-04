from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from code_scout.commands.cli import app
from code_scout.pipeline.watch import WatchFileEvent, watch_workspace
from code_scout.runtime.config import CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE
from code_scout.storage import database_path
from code_scout.workspace.config import DEFAULT_CONFIG_PATH

FIXTURE = Path(__file__).parent / "fixtures" / "typescript-monorepo"


def invoke_code_scout(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = app(args)

    return (0 if result is None else result), stdout.getvalue(), stderr.getvalue()


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def test_e2e_mvp_workflow_against_sample_workspace(tmp_path) -> None:
    workspace_path = tmp_path / "workspace"
    shutil.copytree(FIXTURE, workspace_path)
    shutil.rmtree(workspace_path / ".code-scout")

    exit_code, stdout, stderr = invoke_code_scout(
        ["init", "--workspace", str(workspace_path)]
    )
    assert exit_code == 0
    assert stderr == ""
    assert "Created code-scout config" in stdout
    _write_e2e_config(workspace_path)
    _write_graphify_json(workspace_path)

    exit_code, scan_output, stderr = invoke_code_scout(
        ["scan", "--workspace", str(workspace_path)]
    )
    assert exit_code == 0
    assert stderr == ""
    assert "Scan complete." in scan_output
    assert "Extractor: all enabled extractors" in scan_output
    assert "Files analyzed: 13" in scan_output
    assert "Services detected: 0" in scan_output
    assert "Contracts detected: 2" in scan_output
    assert "ADRs detected: 1" in scan_output
    assert database_path(workspace_path).is_file()
    _assert_scan_time_evidence_and_embeddings(workspace_path)

    exit_code, lsp_output, lsp_error = invoke_code_scout(
        ["scan", "--workspace", str(workspace_path), "--extractor", "lsp"]
    )
    assert (exit_code, lsp_output) == (1, "")
    assert "Error: TypeScript language server is unavailable." in lsp_error
    assert "code-scout scan --extractor lsp" in lsp_error

    exit_code, graphify_output, stderr = invoke_code_scout(
        ["scan", "--workspace", str(workspace_path), "--extractor", "graphify"]
    )
    assert exit_code == 0
    assert stderr == ""
    assert "Extractor: graphify" in graphify_output

    _assert_graphify_edge_persisted(workspace_path)
    assert [path.name for path in tmp_path.iterdir()] == ["workspace"]


def test_offline_status_to_impact_workflow_preserves_state_and_refreshes_freshness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "offline-repository"
    workspace.mkdir()
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.email", "code-scout@example.test")
    _git(workspace, "config", "user.name", "Code Scout Test")
    (workspace / ".gitignore").write_text(".code-scout/\n", encoding="utf-8")
    changed = workspace / "changed.ts"
    changed.write_text(
        "export function changed() {\n  return 1;\n}\n", encoding="utf-8"
    )
    (workspace / "caller.ts").write_text(
        "export function caller() {\n  return changed();\n}\n", encoding="utf-8"
    )
    _git(workspace, "add", "--all")
    _git(workspace, "commit", "-q", "-m", "initial")
    monkeypatch.setenv(
        CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "isolated-home")
    )

    exit_code, _, stderr = invoke_code_scout(["init", "--workspace", str(workspace)])
    assert (exit_code, stderr) == (0, "")
    _write_offline_impact_config(workspace)
    _write_offline_impact_graph(workspace)
    exit_code, scan_output, stderr = invoke_code_scout(
        ["scan", "--workspace", str(workspace)]
    )
    assert (exit_code, stderr) == (0, "")
    assert "Scan complete." in scan_output

    exit_code, status_output, stderr = invoke_code_scout(
        ["status", "--workspace", str(workspace), "--format", "json"]
    )
    assert (exit_code, stderr) == (0, "")
    assert json.loads(status_output)["freshness"]["state"] == "fresh"

    changed.write_text(
        "export function changed() {\n  return 2;\n}\n", encoding="utf-8"
    )
    _git(workspace, "add", "changed.ts")
    exit_code, status_output, stderr = invoke_code_scout(
        ["status", "--workspace", str(workspace), "--format", "json"]
    )
    assert (exit_code, stderr) == (0, "")
    stale_status = json.loads(status_output)
    assert stale_status["freshness"]["state"] == "stale"
    assert stale_status["changes"]["modified"] == [
        {"path": "changed.ts", "repository_id": "workspace"}
    ]

    state_before = _read_only_workflow_state(workspace)
    for diagnostic in ("status", "doctor"):
        exit_code, _, stderr = invoke_code_scout(
            [diagnostic, "--workspace", str(workspace), "--format", "json"]
        )
        assert (exit_code, stderr) == (0, "")
    exit_code, impact_output, stderr = invoke_code_scout(
        [
            "impact",
            "--workspace",
            str(workspace),
            "--staged",
            "--format",
            "json",
        ]
    )
    assert (exit_code, stderr) == (0, "")
    impact = json.loads(impact_output)
    assert impact["comparison"]["mode"] == "staged"
    assert impact["index"]["freshness"]["state"] == "stale"
    assert impact["changed_files"][0]["old_path"] == "changed.ts"
    if impact["paths"]:
        assert impact["paths"][0]["citations"][0]["path"] == "caller.ts"
    else:
        assert impact["changed_files"][0]["mapping_status"] in {
            "file_only",
            "unresolved",
        }
    assert _read_only_workflow_state(workspace) == state_before

    class OneChangeAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (WatchFileEvent("changed.ts"),)

    watch_result = watch_workspace(str(workspace), adapter=OneChangeAdapter())
    assert len(watch_result.updates) == 1
    exit_code, status_output, stderr = invoke_code_scout(
        ["status", "--workspace", str(workspace), "--format", "json"]
    )
    assert (exit_code, stderr) == (0, "")
    assert json.loads(status_output)["freshness"]["state"] == "fresh"

    changed.write_text(
        "export function changed() {\n  return 3;\n}\n", encoding="utf-8"
    )
    exit_code, status_output, stderr = invoke_code_scout(
        ["status", "--workspace", str(workspace), "--format", "json"]
    )
    assert (exit_code, stderr) == (0, "")
    assert json.loads(status_output)["freshness"]["state"] == "stale"
    exit_code, _, stderr = invoke_code_scout(["scan", "--workspace", str(workspace)])
    assert (exit_code, stderr) == (0, "")
    exit_code, status_output, stderr = invoke_code_scout(
        ["status", "--workspace", str(workspace), "--format", "json"]
    )
    assert (exit_code, stderr) == (0, "")
    assert json.loads(status_output)["freshness"]["state"] == "fresh"


def _write_offline_impact_config(workspace: Path) -> None:
    disabled = (
        "tree_sitter",
        "lsp",
        "adr",
        "docs",
        "infra",
        "openapi",
        "asyncapi",
        "package_metadata",
        "symbol_extraction",
    )
    extractor_lines = "\n".join(
        f"  {extractor}:\n    enabled: false" for extractor in disabled
    )
    (workspace / DEFAULT_CONFIG_PATH).write_text(
        "\n".join(
            (
                "workspace:",
                "  id: workspace",
                "  name: Offline impact fixture",
                "repositories:",
                "  - id: workspace",
                "    path: .",
                "extractors:",
                extractor_lines,
                "  graphify:",
                "    enabled: true",
                "    json_path: .code-scout/impact-graph.json",
                "",
            )
        ),
        encoding="utf-8",
    )


def _write_offline_impact_graph(workspace: Path) -> None:
    graph = {
        "symbols": [
            {
                "id": "symbol:workspace:changed.ts:graphify:changed",
                "file_id": "file:workspace:changed.ts",
                "name": "changed",
                "kind": "function",
                "qualified_name": "changed",
                "start_line": 1,
                "end_line": 3,
            },
            {
                "id": "symbol:workspace:caller.ts:graphify:caller",
                "file_id": "file:workspace:caller.ts",
                "name": "caller",
                "kind": "function",
                "qualified_name": "caller",
                "start_line": 1,
                "end_line": 3,
            },
        ],
        "edges": [
            {
                "source_type": "symbol",
                "source_id": "symbol:workspace:caller.ts:graphify:caller",
                "target_type": "symbol",
                "target_id": "symbol:workspace:changed.ts:graphify:changed",
                "relation_type": "graphify_calls",
                "confidence": 0.95,
                "evidence": [
                    {
                        "file_path": "caller.ts",
                        "start_line": 2,
                        "end_line": 2,
                        "confidence": 0.95,
                        "metadata": {"repository_id": "workspace"},
                    }
                ],
            }
        ],
    }
    (workspace / ".code-scout" / "impact-graph.json").write_text(
        json.dumps(graph, indent=2), encoding="utf-8"
    )


def _read_only_workflow_state(workspace: Path) -> tuple[object, ...]:
    return (
        _git(workspace, "status", "--porcelain=v1"),
        _git(workspace, "rev-parse", "HEAD"),
        _git(workspace, "for-each-ref", "--format=%(refname):%(objectname)"),
        (workspace / ".git" / "index").read_bytes(),
        database_path(workspace).read_bytes(),
    )


def _write_e2e_config(workspace_path: Path) -> None:
    (workspace_path / DEFAULT_CONFIG_PATH).write_text(
        """
workspace:
  id: sample-typescript-monorepo
  name: Sample TypeScript Monorepo
repositories:
  - id: sample
    path: .
    name: sample-monorepo
extractors:
  tree_sitter:
    enabled: true
  lsp:
    enabled: true
    command:
      - code-scout-missing-typescript-language-server
      - --stdio
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
  adr:
    enabled: true
  openapi:
    enabled: true
  package_metadata:
    enabled: true
paths:
  adr:
    - docs/adr
  openapi:
    - contracts
""",
        encoding="utf-8",
    )


def _write_graphify_json(workspace_path: Path) -> None:
    graphify_graph = {
        "symbols": [
            {
                "id": (
                    "symbol:sample:apps/api/src/users/users.controller.ts:"
                    "graphify:UsersController"
                ),
                "file_id": "file:sample:apps/api/src/users/users.controller.ts",
                "name": "UsersController",
                "kind": "graphify_class",
                "qualified_name": "UsersController",
                "start_line": 4,
                "end_line": 12,
            },
            {
                "id": (
                    "symbol:sample:apps/api/src/users/users.service.ts:"
                    "graphify:UsersService"
                ),
                "file_id": "file:sample:apps/api/src/users/users.service.ts",
                "name": "UsersService",
                "kind": "graphify_class",
                "qualified_name": "UsersService",
                "start_line": 1,
                "end_line": 7,
            },
        ],
        "edges": [
            {
                "source_type": "symbol",
                "source_id": (
                    "symbol:sample:apps/api/src/users/users.controller.ts:"
                    "graphify:UsersController"
                ),
                "target_type": "symbol",
                "target_id": (
                    "symbol:sample:apps/api/src/users/users.service.ts:"
                    "graphify:UsersService"
                ),
                "relation_type": "graphify_calls",
                "confidence": 0.82,
                "evidence": [
                    {
                        "file_path": "apps/api/src/users/users.controller.ts",
                        "start_line": 8,
                        "end_line": 8,
                        "confidence": 0.82,
                        "metadata": {"repository_id": "sample"},
                    }
                ],
            }
        ],
    }
    (workspace_path / ".code-scout" / "graphify.json").write_text(
        json.dumps(graphify_graph, indent=2),
        encoding="utf-8",
    )


def _assert_graphify_edge_persisted(workspace_path: Path) -> None:
    connection = sqlite3.connect(database_path(workspace_path))
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT source_id, target_id, evidence_json
        FROM edges
        WHERE relation_type = 'graphify_calls'
        """
    ).fetchall()
    connection.close()

    assert len(rows) == 1
    assert rows[0]["source_id"] == (
        "symbol:sample:apps/api/src/users/users.controller.ts:userscontroller"
    )
    assert rows[0]["target_id"] == (
        "symbol:sample:apps/api/src/users/users.service.ts:usersservice"
    )
    evidence = json.loads(rows[0]["evidence_json"])
    assert evidence[0]["extractor_name"] == "graphify"
    assert evidence[0]["metadata"]["repository_id"] == "sample"


def _assert_scan_time_evidence_and_embeddings(workspace_path: Path) -> None:
    connection = sqlite3.connect(database_path(workspace_path))
    connection.row_factory = sqlite3.Row
    counts = connection.execute(
        """
        SELECT
            (SELECT count(*) FROM raw_evidence_units) AS evidence_units,
            (SELECT count(*) FROM evidence_embeddings) AS embeddings
        """
    ).fetchone()
    embedded_file_unit = connection.execute(
        """
        SELECT raw_evidence_units.id, raw_evidence_units.owner_entity_id
        FROM raw_evidence_units
        INNER JOIN evidence_embeddings
            ON evidence_embeddings.evidence_unit_id = raw_evidence_units.id
        WHERE raw_evidence_units.owner_entity_id = ?
        """,
        ("file:sample:apps/api/src/users/users.controller.ts",),
    ).fetchone()
    connection.close()

    assert counts["evidence_units"] > 0
    assert counts["embeddings"] == counts["evidence_units"]
    assert embedded_file_unit["id"].startswith("evidence-unit:")
