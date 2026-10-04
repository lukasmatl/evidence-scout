from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from code_scout.core.errors import ExtractionError
from code_scout.extractors.graphify import GraphifyWatchEvent
from code_scout.pipeline.incremental import replace_changed_file_active_facts
from code_scout.pipeline.scan import ChangedFileReplacementResult, scan_workspace
from code_scout.pipeline.watch import WatchFileEvent, watch_workspace
from code_scout.storage import database_path, list_change_events, open_storage


def test_watch_update_processes_one_routed_change_and_logs_summary(tmp_path) -> None:
    _write_config(
        tmp_path,
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  graphify:
    enabled: false
  lsp:
    enabled: false
""",
    )
    (tmp_path / "package.json").write_text(
        '{"name":"orders","dependencies":{"fastify":"latest"}}\n',
        encoding="utf-8",
    )

    class WatchAdapter:
        def changed_files(
            self,
            *,
            workspace_root: str,
            debounce: float,
            changed_only: bool,
        ) -> tuple[WatchFileEvent, ...]:
            assert workspace_root == str(tmp_path)
            assert debounce == 0.25
            assert changed_only is False
            return (WatchFileEvent("package.json"),)

    result = watch_workspace(
        str(tmp_path),
        adapter=WatchAdapter(),
        log=".code-scout/watch.log",
    )

    with open_storage(tmp_path) as connection:
        change_events = list_change_events(connection)

    summary = result.summary()
    assert "Watch update complete." in summary
    assert "Updates processed: 1" in summary
    assert "Routes: package_json" in summary
    assert "Extractors run: package_metadata" in summary
    assert "Graph: rebuilt" in summary
    assert (tmp_path / ".code-scout" / "watch.log").read_text(encoding="utf-8") == (
        summary + "\n"
    )
    assert len(change_events) == 1
    assert change_events[0].file == "package.json"
    assert change_events[0].extractors_run == ("package_metadata",)
    assert change_events[0].outputs_rebuilt is False


def test_watch_dry_run_filters_route_extractors_by_profile_and_graphify_source(
    tmp_path,
) -> None:
    _write_config(
        tmp_path,
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
""",
    )

    class WatchAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (WatchFileEvent("src/app.ts"),)

    result = watch_workspace(
        str(tmp_path),
        dry_run=True,
        profile="code-only",
        adapter=WatchAdapter(),
    )

    assert not database_path(tmp_path).exists()
    assert result.files[0].routes == ("typescript",)
    assert result.files[0].extractors == (
        "tree_sitter",
        "symbol_extraction",
        "lsp",
    )
    assert result.extractors == (
        "tree_sitter",
        "symbol_extraction",
        "lsp",
    )


def test_watch_update_uses_configured_graphify_watch_once_for_updates(
    tmp_path,
) -> None:
    _write_config(
        tmp_path,
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: false
  lsp:
    enabled: false
  symbol_extraction:
    enabled: false
  graphify:
    enabled: true
    watch:
      command:
        - graphify
        - watch
  adr:
    enabled: false
  docs:
    enabled: false
  infra:
    enabled: false
  openapi:
    enabled: false
  asyncapi:
    enabled: false
  package_metadata:
    enabled: false
""",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export function App() {}\n")
    (tmp_path / "src" / "users.ts").write_text("export function Users() {}\n")

    class WatchAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (
                WatchFileEvent("src/app.ts"),
                WatchFileEvent("src/users.ts"),
            )

    class GraphifyWatchAdapter:
        def __init__(self) -> None:
            self.calls = 0

        def watch_events(self, **_kwargs: object) -> tuple[GraphifyWatchEvent, ...]:
            self.calls += 1
            return (
                GraphifyWatchEvent(
                    graph_json="""
{
  "changed_graph": {
    "symbols": [
      {
        "id": "symbol:workspace:src/app.ts:graphify:App",
        "file_id": "file:workspace:src/app.ts",
        "name": "App",
        "kind": "function",
        "qualified_name": "App"
      },
      {
        "id": "symbol:workspace:src/users.ts:graphify:Users",
        "file_id": "file:workspace:src/users.ts",
        "name": "Users",
        "kind": "function",
        "qualified_name": "Users"
      }
    ]
  }
}
""",
                ),
            )

    graphify_adapter = GraphifyWatchAdapter()

    result = watch_workspace(
        str(tmp_path),
        adapter=WatchAdapter(),
        graphify_watch_adapter=graphify_adapter,
    )

    connection = sqlite3.connect(database_path(tmp_path))
    source_files = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT source_file FROM facts WHERE extractor = 'graphify'",
        ).fetchall()
    }
    connection.close()

    assert graphify_adapter.calls == 1
    assert len(result.updates) == 1
    assert result.updates[0].path == "src/app.ts,src/users.ts"
    assert source_files == {"src/app.ts", "src/users.ts"}


def test_watch_workspace_batches_routed_plans_and_reports_graph_updates(
    monkeypatch,
    tmp_path,
) -> None:
    _write_config(
        tmp_path,
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  graphify:
    enabled: false
  tree_sitter:
    enabled: true
  symbol_extraction:
    enabled: true
  lsp:
    enabled: false
  package_metadata:
    enabled: true
""",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export function App() {}\n")
    (tmp_path / "package.json").write_text('{"name":"orders"}\n')

    replacement_calls: list[dict[str, object]] = []

    def fake_replace(
        workspace_path: str,
        *,
        changed_file_paths: tuple[str, ...],
        deleted_file_paths: tuple[str, ...],
        selected_extractors: tuple[str, ...],
        graphify_watch_adapter: object | None,
    ) -> ChangedFileReplacementResult:
        replacement_calls.append(
            {
                "workspace_path": workspace_path,
                "changed_file_paths": changed_file_paths,
                "deleted_file_paths": deleted_file_paths,
                "selected_extractors": selected_extractors,
                "graphify_watch_adapter": graphify_watch_adapter,
            },
        )
        return ChangedFileReplacementResult(
            facts_added=5,
            facts_removed=2,
            nodes_updated=3,
            change_event_id="change-event:test",
        )

    monkeypatch.setattr(
        "code_scout.pipeline.watch.replace_changed_file_active_facts",
        fake_replace,
    )

    class WatchAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (
                WatchFileEvent("src/app.ts"),
                WatchFileEvent("package.json"),
                WatchFileEvent("notes.txt"),
            )

    result = watch_workspace(
        str(tmp_path),
        adapter=WatchAdapter(),
    )

    assert replacement_calls == [
        {
            "workspace_path": str(tmp_path),
            "changed_file_paths": ("src/app.ts", "package.json"),
            "deleted_file_paths": (),
            "selected_extractors": (
                "tree_sitter",
                "symbol_extraction",
                "package_metadata",
            ),
            "graphify_watch_adapter": None,
        },
    ]
    assert len(result.updates) == 2
    assert result.updates[0].path == "notes.txt"
    assert result.updates[0].skipped is True
    assert result.updates[1].path == "src/app.ts,package.json"
    assert result.updates[1].routes == ("typescript", "package_json")
    assert result.updates[1].extractors == (
        "tree_sitter",
        "symbol_extraction",
        "package_metadata",
    )
    assert result.updates[1].facts_added == 5
    assert result.updates[1].facts_removed == 2
    assert result.updates[1].nodes_updated == 3
    assert "Graph: rebuilt" in result.summary()
    assert "Exports:" not in result.summary()


def test_watch_update_continues_non_graphify_extractors_after_graphify_warning(
    tmp_path,
) -> None:
    _write_config(
        tmp_path,
        """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: true
  lsp:
    enabled: false
  symbol_extraction:
    enabled: true
  graphify:
    enabled: true
    watch:
      command:
        - graphify
        - watch
  adr:
    enabled: false
  docs:
    enabled: false
  infra:
    enabled: false
  openapi:
    enabled: false
  asyncapi:
    enabled: false
  package_metadata:
    enabled: true
""",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export function App() {}\n")
    (tmp_path / "package.json").write_text('{"name":"orders"}\n')

    class WatchAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (
                WatchFileEvent("src/app.ts"),
                WatchFileEvent("package.json"),
            )

    class FailingGraphifyWatchAdapter:
        def watch_events(self, **_kwargs: object) -> object:
            raise ExtractionError(
                "Graphify watch failed.",
                hint="Restart Graphify watch.",
            )

    result = watch_workspace(
        str(tmp_path),
        adapter=WatchAdapter(),
        graphify_watch_adapter=FailingGraphifyWatchAdapter(),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    package_fact_count = connection.execute(
        """
        SELECT count(*)
        FROM facts
        WHERE extractor = 'package_json' AND source_file = 'package.json'
        """,
    ).fetchone()[0]
    graphify_fact_count = connection.execute(
        "SELECT count(*) FROM facts WHERE extractor = 'graphify'",
    ).fetchone()[0]
    connection.close()

    assert result.updates[0].warnings == 1
    assert graphify_fact_count == 0
    assert package_fact_count > 0


def test_watch_update_matches_direct_incremental_replacement(tmp_path) -> None:
    direct_workspace = tmp_path / "direct"
    watch_workspace_path = tmp_path / "watch"
    direct_workspace.mkdir()
    watch_workspace_path.mkdir()
    for workspace_path in (direct_workspace, watch_workspace_path):
        _write_config(
            workspace_path,
            """
workspace:
  id: workspace
  name: Workspace
repositories:
  - id: workspace
    path: .
extractors:
  graphify:
    enabled: false
  lsp:
    enabled: false
""",
        )
        (workspace_path / "package.json").write_text(
            '{"name":"orders","dependencies":{"express":"latest"}}\n',
            encoding="utf-8",
        )
        scan_workspace(str(workspace_path), extractor="package_metadata")
        (workspace_path / "package.json").write_text(
            '{"name":"orders","dependencies":{"next":"latest"}}\n',
            encoding="utf-8",
        )

    direct_result = replace_changed_file_active_facts(
        str(direct_workspace),
        changed_file_paths=("package.json",),
        selected_extractors=("package_metadata",),
    )

    class WatchAdapter:
        def changed_files(self, **_kwargs: object) -> tuple[WatchFileEvent, ...]:
            return (WatchFileEvent("package.json"),)

    watch_result = watch_workspace(
        str(watch_workspace_path),
        adapter=WatchAdapter(),
    )

    direct_event = _single_change_event(direct_workspace)
    watch_event = _single_change_event(watch_workspace_path)
    watch_update = watch_result.updates[0]

    assert watch_update.facts_added == direct_result.facts_added
    assert watch_update.facts_removed == direct_result.facts_removed
    assert watch_update.nodes_updated == direct_result.nodes_updated
    assert _incremental_package_state(watch_workspace_path) == (
        _incremental_package_state(direct_workspace)
    )
    assert {
        "type": watch_event.type,
        "file": watch_event.file,
        "extractors_run": watch_event.extractors_run,
        "facts_added": watch_event.facts_added,
        "facts_removed": watch_event.facts_removed,
        "nodes_updated": watch_event.nodes_updated,
        "outputs_rebuilt": watch_event.outputs_rebuilt,
        "metadata": watch_event.metadata,
    } == {
        "type": direct_event.type,
        "file": direct_event.file,
        "extractors_run": direct_event.extractors_run,
        "facts_added": direct_event.facts_added,
        "facts_removed": direct_event.facts_removed,
        "nodes_updated": direct_event.nodes_updated,
        "outputs_rebuilt": direct_event.outputs_rebuilt,
        "metadata": direct_event.metadata,
    }


def _write_config(tmp_path, text: str) -> None:
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(text.lstrip(), encoding="utf-8")


def _single_change_event(workspace_path: Path):
    with open_storage(workspace_path) as connection:
        (event,) = list_change_events(connection)
        return event


def _incremental_package_state(workspace_path: Path) -> dict[str, object]:
    connection = sqlite3.connect(database_path(workspace_path))
    connection.row_factory = sqlite3.Row
    facts = tuple(
        (
            row["id"],
            row["extractor"],
            row["kind"],
            row["source_file"],
            row["identity_json"],
            _stable_fact_properties(row["properties_json"]),
            row["evidence_json"],
            row["confidence"],
            row["conflict_metadata_json"],
            row["extractor_version"],
        )
        for row in connection.execute(
            """
            SELECT id, extractor, kind, source_file, identity_json, properties_json,
                   evidence_json, confidence, conflict_metadata_json,
                   extractor_version
            FROM facts
            ORDER BY id
            """,
        ).fetchall()
    )
    repositories = tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT id, workspace_id, name, path, language_hints_json,
                   framework_hints_json, module_hints_json
            FROM repositories
            ORDER BY id
            """,
        ).fetchall()
    )
    connection.close()
    return {"facts": facts, "repositories": repositories}


def _stable_fact_properties(properties_json: str) -> str:
    properties = json.loads(properties_json)
    properties.pop("modified_at", None)
    return json.dumps(properties, sort_keys=True)
