from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import code_scout
from code_scout.commands import (
    CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE,
    CommandOptions,
    execute_command,
    options_from_invocation,
    run_command,
)
from code_scout.commands import command as command_spec
from code_scout.commands.cli import COMMANDS, POSITIONING_TEXT, RootFallbackApp, app
from code_scout.core.errors import ConfigError, ExtractionError
from code_scout.models.graph import (
    Concept,
    Contract,
    Edge,
    Evidence,
    File,
    Repository,
    Service,
)
from code_scout.pipeline.watch import WatchFileEvent
from code_scout.storage import (
    ChangeEvent,
    database_path,
    graph_summary_counts,
    list_active_facts,
    open_storage,
    record_change_event,
    upsert_concept,
    upsert_contract,
    upsert_edge,
    upsert_file,
    upsert_repository,
    upsert_service,
    upsert_workspace,
)
from code_scout.workspace.config import DEFAULT_CONFIG_PATH, load_workspace_config
from tests.identity_boundaries import RETIRED_ENVIRONMENT_PREFIX

FIXTURE = Path(__file__).parent / "fixtures" / "typescript-monorepo"


@pytest.mark.parametrize("fallback", [False, True])
def test_scan_lock_is_actionable_without_traceback(tmp_path, monkeypatch, fallback):
    from code_scout.storage import schema
    from code_scout.workspace.config import create_workspace_config

    create_workspace_config(tmp_path)
    opened = open_storage(tmp_path)
    opened.close()
    blocker = sqlite3.connect(database_path(tmp_path))
    blocker.execute("BEGIN EXCLUSIVE")
    connect = sqlite3.connect

    def short_timeout_connect(*args, **kwargs):
        kwargs["timeout"] = 0.01
        return connect(*args, **kwargs)

    monkeypatch.setattr(schema.sqlite3, "connect", short_timeout_connect)
    if fallback:
        monkeypatch.setattr("tests.test_cli.app", RootFallbackApp())
    arguments = ["scan", "--workspace", str(tmp_path), "--profile", "docs"]
    try:
        code, stdout, stderr = invoke_app(arguments)
        assert code == 1
        assert "database is busy" in stderr
        assert "Wait" in stderr
        assert "Traceback" not in stdout + stderr
        assert "Snapshot:" not in stdout
    finally:
        blocker.rollback()
        blocker.close()
    code, stdout, stderr = invoke_app(arguments)
    assert code == 0, stderr
    assert "Snapshot:" in stdout


def invoke_app(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = app(args)

    return (0 if result is None else result), stdout.getvalue(), stderr.getvalue()


def test_root_help_exits_successfully_with_positioning_text() -> None:
    exit_code, output, _ = invoke_app(["--help"])

    assert exit_code == 0
    assert POSITIONING_TEXT in output
    assert "-q, --query TEXT" in output
    assert "[required]" in output
    assert "--engine NAME" in output
    assert "[default: fast]" in output
    assert "--workspace PATH" in output
    assert "[default: .]" in output
    assert "[--format text|json]" in output
    assert "--format text|json" in output
    assert "--version" in output
    for command in COMMANDS:
        assert " ".join(command.path) in output
    assert "benchmark" not in output
    assert "export okf" not in output
    assert "export fastcontext" not in output
    assert "query fastcontext" not in output
    assert "query semantic" not in output
    assert "docs generate" not in output


def test_version_exits_successfully_with_package_version() -> None:
    exit_code, output, _ = invoke_app(["--version"])

    assert exit_code == 0
    assert output.strip() == code_scout.__version__


def test_root_query_threads_format_through_typer_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fake_execute(invocation: object, **_kwargs: object) -> object:
        calls.append(invocation)
        return object()

    def fake_render(_result: object, output_format: str = "text") -> str:
        return f"rendered {output_format}"

    monkeypatch.setattr(
        "code_scout.commands.cli._execute_scout_invocation", fake_execute
    )
    monkeypatch.setattr("code_scout.commands.cli.render_scout_result", fake_render)

    exit_code, output, stderr = invoke_app(
        ["-q", "Find the router", "--format", "json"],
    )

    assert exit_code == 0
    assert output == "rendered json\n"
    assert stderr == ""
    assert len(calls) == 1
    invocation = calls[0]
    assert invocation.query == "Find the router"
    assert invocation.output_format == "json"


def test_workspace_defaults_to_code_scout_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE, "/tmp/scouted")
    monkeypatch.setenv(f"{RETIRED_ENVIRONMENT_PREFIX}_WORKSPACE", "/tmp/retired")

    options = options_from_invocation(["scan"])

    assert options.workspace == "/tmp/scouted"


def test_workspace_option_overrides_code_scout_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE, "/tmp/scouted")

    options = options_from_invocation(["scan", "--workspace", "/tmp/explicit"])

    assert options.workspace == "/tmp/explicit"


def test_command_help_paths_show_workspace_option_and_concise_help() -> None:
    for command in COMMANDS:
        exit_code, output, _ = invoke_app([*command.path, "--help"])

        assert exit_code == 0
        assert command.help in output
        if command.workspace:
            assert "--workspace PATH" in output
            assert "Defaults to CODE_SCOUT_WORKSPACE" in output
        else:
            assert "--workspace PATH" not in output
            assert "Defaults to CODE_SCOUT_WORKSPACE" not in output

        if command.path == ("scan",):
            assert "--extractor PATH" in output
            assert "--profile NAME" in output
        if command.path == ("changes",):
            assert "--output PATH" not in output
        if command.path == ("graph", "inspect"):
            assert "--format text|json" in output
        if command.path == ("watch",):
            assert "--dry-run" in output
            assert "--repo ID" in output
            assert "--profile NAME" in output
            assert "--debounce SEC" in output
            assert "--changed-only" in output
            assert "--export NAME" not in output
            assert "--log PATH" in output

        assert "--manifest PATH" not in output
        assert "--recording PATH" not in output
        assert "--live" not in output
        assert "--provider NAME" not in output


def test_status_doctor_and_impact_help_match_across_both_adapters() -> None:
    def invoke_fallback(arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = RootFallbackApp()(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    for invoke in (invoke_app, invoke_fallback):
        exit_code, root_output, stderr = invoke(["--help"])
        assert (exit_code, stderr) == (0, "")
        for command_name in ("status", "doctor", "impact"):
            assert command_name in root_output

        for command_name in ("status", "doctor"):
            exit_code, output, stderr = invoke([command_name, "--help"])
            assert (exit_code, stderr) == (0, "")
            assert "--workspace" in output
            assert "--format" in output

        exit_code, output, stderr = invoke(["impact", "--help"])
        assert (exit_code, stderr) == (0, "")
        for option in (
            "--workspace",
            "--staged",
            "--base",
            "--format",
            "--max-hops",
            "--max-results",
        ):
            assert option in output


def test_command_registry_omits_benchmark() -> None:
    assert all(command.path != ("benchmark",) for command in COMMANDS)

    with pytest.raises(LookupError, match="Unknown command spec: benchmark"):
        command_spec(("benchmark",))


def test_benchmark_invocation_does_not_route_to_callback() -> None:
    exit_code, output, stderr = invoke_app(
        ["benchmark", "--manifest", "benchmarks/manifest.json"],
    )

    assert exit_code == 2
    assert output == ""
    assert "the following arguments are required: -q/--query" in stderr
    assert "Benchmark manifest is required" not in stderr


def test_init_creates_default_workspace_config(tmp_path) -> None:
    exit_code, output, stderr = invoke_app(["init", "--workspace", str(tmp_path)])

    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_text = config_path.read_text(encoding="utf-8")
    config = load_workspace_config(tmp_path)

    assert exit_code == 0
    assert stderr == ""
    assert f"Created code-scout config at {config_path}." in output
    assert "workspace:" in config_text
    assert "repositories:" in config_text
    assert "extractors:" in config_text
    assert "outputs:" not in config_text
    assert config.repositories[0].path == "."


def test_init_refuses_to_overwrite_existing_config_without_force(tmp_path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text("sentinel: keep\n", encoding="utf-8")

    exit_code, stdout, stderr = invoke_app(["init", "--workspace", str(tmp_path)])

    assert exit_code == 1
    assert stdout == ""
    assert "Error: code-scout config already exists" in stderr
    assert "Hint: Pass `--force` to overwrite it." in stderr
    assert config_path.read_text(encoding="utf-8") == "sentinel: keep\n"


def test_init_force_overwrites_existing_config(tmp_path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text("sentinel: replace\n", encoding="utf-8")

    exit_code, output, stderr = invoke_app(
        ["init", "--workspace", str(tmp_path), "--force"],
    )

    config = load_workspace_config(tmp_path)

    assert exit_code == 0
    assert stderr == ""
    assert f"Created code-scout config at {config_path}." in output
    assert "sentinel" not in config_path.read_text(encoding="utf-8")
    assert config.repositories[0].path == "."


def test_scan_runs_shell_and_parses_extractor_option(tmp_path) -> None:
    invoke_app(["init", "--workspace", str(tmp_path)])

    exit_code, output, stderr = invoke_app(
        ["scan", "--workspace", str(tmp_path), "--extractor", "tree_sitter"],
    )

    assert exit_code == 0
    assert stderr == ""
    assert "Scan complete." in output
    assert "Extractor: tree_sitter" in output
    assert "Files analyzed: 0" in output


def test_scan_runs_shell_and_parses_profile_option(tmp_path) -> None:
    invoke_app(["init", "--workspace", str(tmp_path)])

    exit_code, output, stderr = invoke_app(
        ["scan", "--workspace", str(tmp_path), "--profile", "docs"],
    )

    assert exit_code == 0
    assert stderr == ""
    assert "Scan complete." in output
    assert "Extractor: docs" in output
    assert "Files analyzed: 0" in output


def test_scan_cli_preserves_representative_fixture_graph_behavior(tmp_path) -> None:
    workspace_path = tmp_path / "workspace"
    shutil.copytree(FIXTURE, workspace_path)

    exit_code, output, stderr = invoke_app(
        ["scan", "--workspace", str(workspace_path)],
    )

    with open_storage(workspace_path) as connection:
        counts = graph_summary_counts(connection)
        active_facts = list_active_facts(connection)

    assert exit_code == 0
    assert stderr == ""
    assert "Scan complete." in output
    assert "Files analyzed: 13" in output
    assert "Warnings: 0" in output
    assert counts == {
        "files": 13,
        "symbols": 0,
        "services": 0,
        "contracts": 2,
        "concepts": 5,
        "decisions": 1,
        "edges": 22,
    }
    assert {
        (fact.extractor, fact.kind, fact.source_file)
        for fact in active_facts
        if fact.kind in {"contract", "decision"}
    } == {
        ("adr_markdown", "decision", "docs/adr/0001-use-modular-monorepo.md"),
        ("openapi", "contract", "contracts/openapi.yaml"),
    }
    assert {
        fact.identity["entity_id"] for fact in active_facts if fact.kind == "contract"
    } == {
        "contract:sample:http:get:users",
        "contract:sample:http:get:users-id",
    }


@pytest.mark.parametrize("profile", ["code-only", "docs", "agent", "full"])
def test_scan_parses_named_profile_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    profile: str,
) -> None:
    calls: list[dict[str, str | None]] = []

    class StubResult:
        def summary(self) -> str:
            return "stub scan"

    def fake_scan_workspace(
        workspace: str,
        *,
        extractor: str | None = None,
        profile: str | None = None,
    ) -> StubResult:
        calls.append(
            {
                "workspace": workspace,
                "extractor": extractor,
                "profile": profile,
            },
        )
        return StubResult()

    monkeypatch.setattr(
        "code_scout.commands.dispatch.scan_workspace", fake_scan_workspace
    )

    exit_code, output, stderr = invoke_app(
        ["scan", "--workspace", str(tmp_path), "--profile", profile],
    )

    assert exit_code == 0
    assert output == "stub scan\n"
    assert stderr == ""
    assert calls == [
        {
            "workspace": str(tmp_path),
            "extractor": None,
            "profile": profile,
        },
    ]


def test_scan_shared_command_path_supports_typer_and_fallback_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[dict[str, str | None]] = []

    class StubResult:
        def summary(self) -> str:
            return "stub scan"

    def fake_scan_workspace(
        workspace: str,
        *,
        extractor: str | None = None,
        profile: str | None = None,
    ) -> StubResult:
        calls.append(
            {
                "workspace": workspace,
                "extractor": extractor,
                "profile": profile,
            },
        )
        return StubResult()

    monkeypatch.setattr(
        "code_scout.commands.dispatch.scan_workspace", fake_scan_workspace
    )
    scan_command = command_spec(("scan",))

    typer_output = execute_command(
        scan_command,
        CommandOptions(workspace=str(tmp_path), profile="docs"),
    )
    fallback_output = run_command(
        scan_command,
        ["scan", "--workspace", str(tmp_path), "--profile", "docs"],
    )

    assert typer_output == "stub scan"
    assert fallback_output == "stub scan"
    assert calls == [
        {
            "workspace": str(tmp_path),
            "extractor": None,
            "profile": "docs",
        },
        {
            "workspace": str(tmp_path),
            "extractor": None,
            "profile": "docs",
        },
    ]


def test_status_json_contract_is_equivalent_across_cli_adapters(tmp_path) -> None:
    invoke_app(["init", "--workspace", str(tmp_path)])
    (tmp_path / "README.md").write_text("content\n", encoding="utf-8")
    invoke_app(["scan", "--workspace", str(tmp_path)])
    with sqlite3.connect(database_path(tmp_path)) as connection:
        connection.execute("UPDATE snapshots SET metadata_json = '{}' ")
        connection.commit()
    database_before = database_path(tmp_path).read_bytes()

    typer_exit, typer_output, typer_error = invoke_app(
        ["status", "--workspace", str(tmp_path), "--format", "json"],
    )
    fallback_stdout = io.StringIO()
    fallback_stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(fallback_stdout),
        contextlib.redirect_stderr(fallback_stderr),
    ):
        fallback_exit = RootFallbackApp()(
            ["status", "--workspace", str(tmp_path), "--format", "json"],
        )

    assert typer_exit == fallback_exit == 0
    assert typer_error == fallback_stderr.getvalue() == ""
    assert json.loads(typer_output) == json.loads(fallback_stdout.getvalue())
    payload = json.loads(typer_output)
    assert payload["schema_version"] == "1"
    assert payload["index"]["state"] == "ready"
    assert payload["freshness"]["state"] == "unknown"
    assert payload["freshness"]["coverage"] == "unknown"
    assert payload["freshness"]["coverage_reasons"][0]["code"] == ("scan_scope_unknown")

    typer_text_exit, typer_text, typer_text_error = invoke_app(
        ["status", "--workspace", str(tmp_path)],
    )
    fallback_text_stdout = io.StringIO()
    fallback_text_stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(fallback_text_stdout),
        contextlib.redirect_stderr(fallback_text_stderr),
    ):
        fallback_text_exit = RootFallbackApp()(["status", "--workspace", str(tmp_path)])

    assert typer_text_exit == fallback_text_exit == 0
    assert typer_text_error == fallback_text_stderr.getvalue() == ""
    assert typer_text == fallback_text_stdout.getvalue()
    assert "Freshness: unknown" in typer_text
    assert "Coverage: unknown" in typer_text
    assert "Historical scan metadata" in typer_text
    assert database_path(tmp_path).read_bytes() == database_before


def test_impact_shared_command_path_parses_both_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    calls: list[dict[str, object]] = []

    class StubReport:
        def render(self, output_format: str) -> str:
            return json.dumps({"schema_version": "1", "format": output_format})

    def fake_report(workspace: str, **options: object) -> StubReport:
        calls.append({"workspace": workspace, **options})
        return StubReport()

    monkeypatch.setattr(
        "code_scout.commands.dispatch.generate_impact_report", fake_report
    )
    impact_command = command_spec(("impact",))
    typed_output = execute_command(
        impact_command,
        CommandOptions(
            workspace=str(tmp_path),
            staged=True,
            output_format="json",
            max_hops=3,
            max_results=7,
        ),
    )
    fallback_output = run_command(
        impact_command,
        [
            "impact",
            "--workspace",
            str(tmp_path),
            "--base",
            "main",
            "--format",
            "json",
            "--max-hops",
            "3",
            "--max-results",
            "7",
        ],
    )

    assert json.loads(typed_output)["schema_version"] == "1"
    assert json.loads(fallback_output)["schema_version"] == "1"
    assert calls == [
        {
            "workspace": str(tmp_path),
            "staged": True,
            "base_ref": None,
            "max_hops": 3,
            "max_results": 7,
        },
        {
            "workspace": str(tmp_path),
            "staged": False,
            "base_ref": "main",
            "max_hops": 3,
            "max_results": 7,
        },
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["impact"],
        ["impact", "--staged", "--base", "main"],
        ["impact", "--staged", "--max-hops", "0"],
        ["impact", "--staged", "--max-results", "1001"],
        ["impact", "--staged", "--format", "yaml"],
    ],
)
def test_impact_invalid_arguments_exit_two_without_traceback(arguments) -> None:
    exit_code, stdout, stderr = invoke_app(arguments)

    assert exit_code == 2
    assert stdout == ""
    assert "Error:" in stderr
    assert "Traceback" not in stderr


def test_impact_json_errors_remain_machine_readable() -> None:
    exit_code, stdout, stderr = invoke_app(
        ["impact", "--staged", "--base", "main", "--format", "json"]
    )

    assert exit_code == 2
    assert stdout == ""
    assert json.loads(stderr)["error"]["kind"] == "usage"


def test_impact_json_unavailable_errors_remain_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_report(*_args: object, **_kwargs: object) -> None:
        raise ConfigError(
            "The code-scout index is unavailable for impact analysis.",
            "Run `code-scout scan --workspace .` and retry.",
        )

    monkeypatch.setattr(
        "code_scout.commands.dispatch.generate_impact_report", fail_report
    )

    exit_code, stdout, stderr = invoke_app(["impact", "--staged", "--format", "json"])

    assert exit_code == 1
    assert stdout == ""
    error = json.loads(stderr)["error"]
    assert error["kind"] == "unavailable"
    assert "code-scout scan" in error["hint"]


def test_changes_reports_empty_output(tmp_path) -> None:
    exit_code, output, stderr = invoke_app(
        ["changes", "--workspace", str(tmp_path)],
    )

    assert exit_code == 0
    assert output == "No change events recorded.\n"
    assert stderr == ""


def test_changes_lists_multiple_events_in_reverse_chronological_order(tmp_path) -> None:
    first = ChangeEvent(
        id="change-event:1",
        type="incremental_change",
        file="src/api/users.ts",
        extractors_run=("tree_sitter", "lsp"),
        facts_added=3,
        facts_removed=1,
        nodes_updated=2,
        outputs_rebuilt=True,
        timestamp="2026-06-21T10:00:00+00:00",
    )
    second = ChangeEvent(
        id="change-event:2",
        type="incremental_delete",
        file="docs/adr/0001-old.md",
        extractors_run=("adr",),
        facts_added=0,
        facts_removed=4,
        nodes_updated=1,
        outputs_rebuilt=False,
        timestamp="2026-06-21T10:05:00+00:00",
    )
    with open_storage(tmp_path) as connection:
        record_change_event(connection, first)
        record_change_event(connection, second)
        connection.commit()

    exit_code, output, stderr = invoke_app(
        ["changes", "--workspace", str(tmp_path)],
    )

    assert exit_code == 0
    assert stderr == ""
    assert output.startswith("Recent change events:")
    assert output.index("2026-06-21T10:05:00+00:00") < output.index(
        "2026-06-21T10:00:00+00:00",
    )
    assert "Type: incremental_delete" in output
    assert "File: docs/adr/0001-old.md" in output
    assert "Extractors run: adr" in output
    assert "Facts: +0/-4" in output
    assert "Nodes updated: 1" in output
    assert "Type: incremental_change" in output
    assert "File: src/api/users.ts" in output
    assert "Extractors run: tree_sitter, lsp" in output
    assert "Facts: +3/-1" in output
    assert "Nodes updated: 2" in output


def test_graph_inspect_reports_empty_workspace_summary(tmp_path) -> None:
    exit_code, output, stderr = invoke_app(
        ["graph", "inspect", "--workspace", str(tmp_path)],
    )

    assert exit_code == 0
    assert stderr == ""
    assert "Graph inspect summary:" in output
    assert "Repositories: 0" in output
    assert "Services: 0" in output
    assert "Contracts: 0" in output
    assert "Concepts: 0" in output
    assert "Decisions: 0" in output
    assert "Files: 0" in output
    assert "Edges: 0" in output
    assert "Errors: 0" in output
    assert "Warnings: 0" in output
    assert "Info: 0" in output
    assert "No quality findings." in output
    assert "QualityFinding(" not in output
    assert "{" not in output


def test_graph_inspect_reports_representative_findings(tmp_path) -> None:
    _seed_representative_graph(tmp_path)

    exit_code, output, stderr = invoke_app(
        ["graph", "inspect", "--workspace", str(tmp_path)],
    )

    assert exit_code == 0
    assert stderr == ""
    assert "Warnings: 1" in output
    assert "Info: 1" in output
    assert "Quality findings need review." in output
    assert (
        "- warning contract_without_consumer contract contract:get-payments: "
        "Contract has no consuming graph link."
    ) in output
    assert (
        "- info concept_without_relationships concept concept:payments: "
        "Concept has no graph relationships."
    ) in output
    assert output.index("contract_without_consumer") < output.index(
        "concept_without_relationships",
    )
    assert "QualityFinding(" not in output


def test_graph_inspect_reports_resolver_conflict_findings_successfully(
    tmp_path,
) -> None:
    _seed_conflicted_contract_graph(tmp_path)

    exit_code, output, stderr = invoke_app(
        ["graph", "inspect", "--workspace", str(tmp_path)],
    )

    assert exit_code == 0
    assert stderr == ""
    assert "Errors: 1" in output
    assert "Quality findings need review." in output
    assert (
        "- error resolver_conflict contract contract:get-payments: "
        "Resolver conflict for contract contract:get-payments field operation_id."
    ) in output


def test_graph_inspect_reports_minimal_workspace_summary_json(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection,
            id="workspace",
            name="Workspace",
            root_path=str(tmp_path),
        )
        upsert_repository(
            connection,
            "workspace",
            Repository(id="workspace", name="Workspace", path="."),
        )
        upsert_file(
            connection,
            File(
                id="file:workspace:src/app.ts",
                repository_id="workspace",
                path="src/app.ts",
                extension=".ts",
                language="TypeScript",
            ),
        )
        upsert_service(
            connection,
            Service(
                id="service:workspace:orders-api",
                name="Orders API",
                repository_id="workspace",
                description="Handles orders.",
            ),
        )
        upsert_contract(
            connection,
            Contract(
                id="contract:workspace:http:get:orders",
                name="GET /orders",
                kind="http",
            ),
        )
        upsert_concept(
            connection,
            Concept(id="concept:orders", name="Orders"),
        )
        upsert_edge(
            connection,
            Edge(
                source_type="service",
                source_id="service:workspace:orders-api",
                target_type="contract",
                target_id="contract:workspace:http:get:orders",
                relation_type="produces",
                confidence=0.9,
            ),
        )
        connection.commit()

    exit_code, output, stderr = invoke_app(
        ["graph", "inspect", "--workspace", str(tmp_path), "--format", "json"],
    )

    assert exit_code == 0
    assert stderr == ""
    assert json.loads(output) == {
        "edge_counts": {
            "edges": 1,
        },
        "entity_counts": {
            "repositories": 1,
            "services": 1,
            "contracts": 1,
            "concepts": 1,
            "decisions": 0,
            "files": 1,
        },
        "quality_finding_counts": {
            "error": 0,
            "info": 1,
            "warning": 1,
        },
        "findings": [
            {
                "code": "contract_without_consumer",
                "severity": "warning",
                "entity_type": "contract",
                "entity_id": "contract:workspace:http:get:orders",
                "message": "Contract has no consuming graph link.",
            },
            {
                "code": "concept_without_relationships",
                "severity": "info",
                "entity_type": "concept",
                "entity_id": "concept:orders",
                "message": "Concept has no graph relationships.",
            },
        ],
    }


def test_graph_inspect_rejects_unsupported_format(tmp_path) -> None:
    exit_code, stdout, stderr = invoke_app(
        ["graph", "inspect", "--workspace", str(tmp_path), "--format", "yaml"],
    )

    assert exit_code == 1
    assert stdout == ""
    assert "Error: Unsupported graph inspect format 'yaml'." in stderr
    assert "Hint: Use `--format text` or `--format json`." in stderr


def test_watch_dry_run_uses_adapter_to_classify_changed_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    invoke_app(["init", "--workspace", str(tmp_path)])
    calls: list[dict[str, object]] = []

    class TestWatchAdapter:
        def changed_files(
            self,
            *,
            workspace_root: str,
            debounce: float,
            changed_only: bool,
        ) -> tuple[WatchFileEvent, ...]:
            calls.append(
                {
                    "workspace_root": workspace_root,
                    "debounce": debounce,
                    "changed_only": changed_only,
                },
            )
            return (
                WatchFileEvent("docs/overview.md"),
                WatchFileEvent("docs/adr/0001-keep-local-memory.md"),
                WatchFileEvent("contracts/openapi.yaml"),
            )

    monkeypatch.setattr("code_scout.pipeline.watch.EmptyWatchAdapter", TestWatchAdapter)

    exit_code, output, stderr = invoke_app(
        [
            "watch",
            "--workspace",
            str(tmp_path),
            "--dry-run",
            "--profile",
            "docs",
            "--debounce",
            "0.5",
            "--changed-only",
        ],
    )

    assert exit_code == 0
    assert stderr == ""
    assert calls == [
        {
            "workspace_root": str(tmp_path),
            "debounce": 0.5,
            "changed_only": True,
        },
    ]
    assert "Watch dry run." in output
    assert "Profile: docs" in output
    assert "Debounce: 0.5s" in output
    assert "Changed only: yes" in output
    assert "Changed files: 3" in output
    assert "docs/overview.md" in output
    assert "Routes: markdown" in output
    assert "Extractors: docs" in output
    assert "docs/adr/0001-keep-local-memory.md" in output
    assert "Routes: adr_markdown" in output
    assert "Extractors: adr" in output
    assert "contracts/openapi.yaml" in output
    assert "Routes: openapi" in output
    assert "Extractors: openapi" in output
    assert "Planned updates:" in output
    assert "active facts by source file: planned" in output
    assert "current graph tables: planned" in output
    assert "change events: planned" in output
    assert "Writes: skipped" in output
    assert not database_path(tmp_path).exists()


def test_watch_dry_run_parses_repo_and_log_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    invoke_app(["init", "--workspace", str(tmp_path)])

    class TestWatchAdapter:
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

    monkeypatch.setattr("code_scout.pipeline.watch.EmptyWatchAdapter", TestWatchAdapter)

    exit_code, output, stderr = invoke_app(
        [
            "watch",
            "--workspace",
            str(tmp_path),
            "--dry-run",
            "--repo",
            "workspace",
            "--log",
            ".code-scout/watch.log",
        ],
    )

    log_path = tmp_path / ".code-scout/watch.log"
    assert exit_code == 0
    assert stderr == ""
    assert "Repository: workspace" in output
    assert "package.json" in output
    assert "Extractors: package_metadata" in output
    assert log_path.read_text(encoding="utf-8") == output
    assert not database_path(tmp_path).exists()


def test_watch_runs_update_loop_without_dry_run(tmp_path) -> None:
    invoke_app(["init", "--workspace", str(tmp_path)])

    exit_code, stdout, stderr = invoke_app(
        ["watch", "--workspace", str(tmp_path)],
    )

    assert exit_code == 0
    assert "Watch update complete." in stdout
    assert "Updates processed: 0" in stdout
    assert stderr == ""


def test_cli_presents_expected_config_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_config(*_args: object) -> str:
        raise ConfigError(
            "Workspace is missing code-scout configuration.",
            "Run `code-scout init --workspace .` before scanning.",
        )

    monkeypatch.setattr("code_scout.commands.cli._run_command", fail_config)

    exit_code, stdout, stderr = invoke_app(["scan"])

    assert exit_code == 1
    assert stdout == ""
    assert "Error: Workspace is missing code-scout configuration." in stderr
    assert "Hint: Run `code-scout init --workspace .` before scanning." in stderr


def test_cli_presents_expected_extraction_setup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_extraction(*_args: object) -> str:
        raise ExtractionError(
            "TypeScript language server is unavailable.",
            "Install `typescript-language-server` and rerun the scan.",
        )

    monkeypatch.setattr("code_scout.commands.cli._run_command", fail_extraction)

    exit_code, stdout, stderr = invoke_app(["scan", "--extractor", "lsp"])

    assert exit_code == 1
    assert stdout == ""
    assert "Error: TypeScript language server is unavailable." in stderr
    assert "Hint: Install `typescript-language-server`" in stderr


def test_unexpected_cli_exceptions_are_not_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_unexpected(*_args: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("code_scout.commands.cli._run_command", fail_unexpected)

    with pytest.raises(RuntimeError, match="boom"):
        invoke_app(["scan"])


def _seed_representative_graph(tmp_path: Path) -> None:
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection,
            id="workspace",
            name="Workspace",
            root_path=str(tmp_path),
        )
        upsert_repository(
            connection,
            "workspace",
            Repository(
                id="workspace",
                name="workspace",
                path=".",
                language_hints=("TypeScript",),
                framework_hints=("NestJS",),
            ),
        )
        upsert_service(
            connection,
            Service(
                id="service:payments",
                name="Payments API",
                repository_id="workspace",
                description="Handles payment workflow requests.",
                evidence=(
                    Evidence(
                        file_path="src/payments.ts",
                        extractor_name="graphify",
                        confidence=0.95,
                        start_line=3,
                    ),
                ),
            ),
        )
        upsert_contract(
            connection,
            Contract(
                id="contract:get-payments",
                name="GET /payments",
                kind="http",
                path="/payments",
                method="GET",
                producer_hint="Payments API",
                operation_id="listPayments",
                evidence=(
                    Evidence(
                        file_path="contracts/openapi.yaml",
                        extractor_name="openapi",
                        confidence=1.0,
                    ),
                ),
            ),
        )
        upsert_concept(
            connection,
            Concept(
                id="concept:payments",
                name="Payments",
                description="Payment processing domain language.",
            ),
        )
        upsert_edge(
            connection,
            Edge(
                source_type="service",
                source_id="service:payments",
                target_type="contract",
                target_id="contract:get-payments",
                relation_type="exposes",
                confidence=0.95,
            ),
        )
        connection.commit()


def _seed_conflicted_contract_graph(tmp_path: Path) -> None:
    with open_storage(tmp_path) as connection:
        upsert_workspace(
            connection,
            id="workspace",
            name="Workspace",
            root_path=str(tmp_path),
        )
        upsert_contract(
            connection,
            Contract(
                id="contract:get-payments",
                name="GET /payments",
                kind="http",
                path="/payments",
                method="GET",
                operation_id="listPayments",
                evidence=(
                    Evidence(
                        file_path="contracts/openapi.yaml",
                        extractor_name="openapi",
                        confidence=1.0,
                        metadata={
                            "resolver_conflicts": (
                                {
                                    "field": "operation_id",
                                    "conflicting_value": "listLegacyPayments",
                                    "resolved_value": "listPayments",
                                },
                            ),
                        },
                    ),
                ),
            ),
        )
        connection.commit()
