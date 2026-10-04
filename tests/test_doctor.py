from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from code_scout.commands.cli import RootFallbackApp, app
from code_scout.commands.doctor import doctor
from code_scout.extractors.treesitter import GrammarLoadResult
from code_scout.models.graph import File
from code_scout.runtime.config import CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE
from code_scout.storage import database_path, open_storage, upsert_file
from code_scout.workspace.config import DEFAULT_CONFIG_PATH


def _invoke(args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = app(args)
    return (0 if result is None else result), stdout.getvalue(), stderr.getvalue()


def _init_ready_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exit_code, _, _ = _invoke(["init", "--workspace", str(workspace)])
    assert exit_code == 0
    config_path = workspace / DEFAULT_CONFIG_PATH
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  lsp:\n    enabled: true",
            "  lsp:\n    enabled: false",
        ),
        encoding="utf-8",
    )
    exit_code, _, _ = _invoke(["scan", "--workspace", str(workspace)])
    assert exit_code == 0
    return workspace


def _global_config(*, fast_key: bool) -> str:
    key_line = 'api_key_env = "DOCTOR_FAST_KEY"' if fast_key else ""
    return f"""
[engines.fast]
max_turns = 8

[engines.fast.model]
base_url = "http://127.0.0.1:8000/v1"
model = "OpenHands/CodeScout-4B"
{key_line}
timeout_seconds = 30
max_tokens = 1000

[engines.verify]
isolation = "local"

[engines.verify.root_model]
base_url = "http://127.0.0.1:8001/v1"
model = "verify-root"
timeout_seconds = 30
max_tokens = 1000

[engines.verify.sub_model]
base_url = "http://127.0.0.1:8001/v1"
model = "verify-sub"
timeout_seconds = 30
max_tokens = 1000
"""


def _checks_by_id(workspace: Path) -> dict[str, object]:
    return {check.id: check for check in doctor(workspace).checks}


def test_ready_local_configuration_keeps_static_engine_usable_without_models(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "home"))

    report = doctor(workspace)
    checks = {check.id: check for check in report.checks}

    assert report.exit_code == 0
    assert checks["workspace.configuration"].severity == "ok"
    assert checks["index.readability"].severity == "ok"
    assert checks["global.configuration"].severity == "warning"
    assert checks["provider.fast"].severity == "unknown"
    assert "static engine remains usable" in checks["provider.verify"].reason
    assert checks["integration.serena"].severity == "ok"
    assert checks["integration.dede"].severity == "ok"


def test_unknown_historical_scan_scope_prevents_freshness_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "home"))
    with sqlite3.connect(database_path(workspace)) as connection:
        connection.execute("UPDATE snapshots SET metadata_json = '{}' ")
        connection.commit()
    database_before = database_path(workspace).read_bytes()

    report = doctor(workspace)
    check = {item.id: item for item in report.checks}["index.freshness"]

    assert check.severity == "warning"
    assert check.reason == "Workspace index freshness is unknown."
    assert "matches the current workspace content" not in check.reason
    assert check.remediation == (
        f"Run `code-scout scan --workspace {workspace}` to refresh it."
    )
    assert "[warning] index.freshness" in report.render("text")
    json_checks = {
        item["id"]: item for item in json.loads(report.render("json"))["checks"]
    }
    assert json_checks["index.freshness"]["severity"] == "warning"
    assert database_path(workspace).read_bytes() == database_before


def test_missing_configured_executable_is_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    config_path = workspace / DEFAULT_CONFIG_PATH
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  lsp:\n    enabled: false",
            "  lsp:\n    enabled: true\n    command:\n      - doctor-missing-lsp",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "home"))

    report = doctor(workspace)
    check = {item.id: item for item in report.checks}["extractor.lsp.executable"]

    assert report.exit_code == 1
    assert check.severity == "error"
    assert "doctor-missing-lsp" in check.reason
    assert "Install" in check.remediation

    exit_code, stdout, stderr = _invoke(
        ["doctor", "--workspace", str(workspace), "--format", "json"]
    )
    assert exit_code == 1
    assert json.loads(stdout)["summary"]["exit_code"] == 1
    assert stderr == ""


def test_missing_referenced_secret_blocks_only_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(_global_config(fast_key=True), encoding="utf-8")
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    monkeypatch.delenv("DOCTOR_FAST_KEY", raising=False)

    checks = _checks_by_id(workspace)

    assert checks["global.configuration"].severity == "ok"
    assert checks["provider.fast"].severity == "error"
    assert "DOCTOR_FAST_KEY" in checks["provider.fast"].reason


@pytest.mark.parametrize("scope", ["workspace", "global"])
def test_malformed_configuration_is_reported_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scope: str,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    if scope == "workspace":
        (workspace / DEFAULT_CONFIG_PATH).write_text("workspace: [\n", encoding="utf-8")
    else:
        home.mkdir()
        (home / "config.toml").write_text("[engines.fast\n", encoding="utf-8")

    report = doctor(workspace)
    checks = {check.id: check for check in report.checks}

    assert report.exit_code == 1
    assert checks[f"{scope}.configuration"].severity == "error"


def test_missing_grammar_from_status_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    config_path = workspace / DEFAULT_CONFIG_PATH
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  lsp:\n    enabled: false",
            "  lsp:\n    enabled: false",
        ),
        encoding="utf-8",
    )
    with open_storage(workspace) as connection:
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
        connection.commit()
    monkeypatch.setattr(
        "code_scout.workspace.status.load_grammar",
        lambda _language: GrammarLoadResult(
            missing_reason="Missing optional package tree-sitter-typescript"
        ),
    )
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "home"))

    checks = _checks_by_id(workspace)

    assert checks["grammar.typescript"].severity == "error"
    assert "tree-sitter-typescript" in checks["grammar.typescript"].remediation


def test_secret_values_are_redacted_from_text_and_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(_global_config(fast_key=True), encoding="utf-8")
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    monkeypatch.setenv("DOCTOR_FAST_KEY", "super-secret-doctor-value")

    report = doctor(workspace)

    checks = {check.id: check for check in report.checks}
    assert checks["provider.fast"].severity == "ok"
    assert "reachability was not tested" in checks["provider.fast"].reason
    assert "super-secret-doctor-value" not in report.render("text")
    assert "super-secret-doctor-value" not in report.render("json")


def test_doctor_exit_codes_and_json_are_equal_across_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "home"))
    args = ["doctor", "--workspace", str(workspace), "--format", "json"]

    typer_exit, typer_output, typer_error = _invoke(args)
    fallback_stdout = io.StringIO()
    fallback_stderr = io.StringIO()
    with (
        contextlib.redirect_stdout(fallback_stdout),
        contextlib.redirect_stderr(fallback_stderr),
    ):
        fallback_exit = RootFallbackApp()(args)

    assert typer_exit == fallback_exit == 0
    assert typer_error == fallback_stderr.getvalue() == ""
    assert json.loads(typer_output) == json.loads(fallback_stdout.getvalue())


@pytest.mark.parametrize("adapter", ["typer", "fallback"])
def test_invalid_doctor_format_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adapter: str,
) -> None:
    workspace = _init_ready_workspace(tmp_path)
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(tmp_path / "home"))
    args = ["doctor", "--workspace", str(workspace), "--format", "xml"]

    if adapter == "typer":
        exit_code, stdout, stderr = _invoke(args)
    else:
        stdout_io = io.StringIO()
        stderr_io = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout_io),
            contextlib.redirect_stderr(stderr_io),
        ):
            exit_code = RootFallbackApp()(args)
        stdout, stderr = stdout_io.getvalue(), stderr_io.getvalue()

    assert exit_code == 2
    assert stdout == ""
    assert "Use `--format text` or `--format json`." in stderr
