from __future__ import annotations

import contextlib
import io
from pathlib import Path

import pytest

from code_scout.commands.cli import RootFallbackApp
from code_scout.core.errors import ConfigError
from code_scout.engines import (
    CODESCOUT_MODEL_IDENTIFIERS,
    CodeScoutEngine,
    VerifyEngine,
    create_engine_registry,
)
from code_scout.runtime.config import (
    CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE,
    create_global_config,
    global_config_path,
    load_global_config,
    render_global_config,
    resolve_model_target,
    validate_global_config,
)


def _environment(home: Path, **values: str) -> dict[str, str]:
    return {CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE: str(home), **values}


def _write_config(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _complete_config(
    *,
    fast_url: str = "https://models.example.test/v1",
    fast_key: str = 'api_key_env = "FAST_KEY"',
    fast_model: str = "OpenHands/CodeScout-4B",
) -> str:
    return f"""
[engines.fast]
max_turns = 9

[engines.fast.model]
base_url = "{fast_url}"
model = "{fast_model}"
{fast_key}
timeout_seconds = 15
max_tokens = 1000
parameters = {{ temperature = 0.2, top_p = 0.9 }}

[engines.verify.root_model]
base_url = "http://127.0.0.1:8002/v1"
model = "verify-root"
timeout_seconds = 45
max_tokens = 3000

[engines.verify.sub_model]
base_url = "http://[::1]:8003/v1"
model = "verify-sub"
timeout_seconds = 10
max_tokens = 500
parameters = {{ seed = 7 }}
"""


def test_global_path_uses_code_scout_home_override(tmp_path: Path) -> None:
    home = tmp_path / "portable-home"

    assert global_config_path(_environment(home)) == home / "config.toml"


def test_init_creates_directory_and_never_overwrites_without_force(
    tmp_path: Path,
) -> None:
    home = tmp_path / "global"
    environment = _environment(home)

    path = create_global_config(environment=environment)
    original = path.read_text(encoding="utf-8")
    path.write_text("sentinel", encoding="utf-8")
    with pytest.raises(ConfigError, match="already exists"):
        create_global_config(environment=environment)
    assert path.read_text(encoding="utf-8") == "sentinel"

    assert create_global_config(force=True, environment=environment) == path
    assert path.read_text(encoding="utf-8") == original


def test_default_global_config_uses_neutral_fast_alias_wording(tmp_path: Path) -> None:
    home = tmp_path / "global"
    path = create_global_config(environment=_environment(home))
    text = path.read_text(encoding="utf-8")

    assert (
        "# `fast` is the CodeScout-backed quick repository localization provider."
        in text
    )
    assert "[engines.fast.model]" in text
    assert "FastContext" not in text
    assert "benchmark-qualified" not in text


def test_separate_engine_models_and_verify_roles_are_loaded(tmp_path: Path) -> None:
    home = tmp_path / "global"
    _write_config(home, _complete_config())
    environment = _environment(home, FAST_KEY="remote-secret")

    config = load_global_config(environment=environment)

    assert config.fast.model.model == "OpenHands/CodeScout-4B"
    assert config.fast.max_turns == 9
    assert config.verify.root_model.model == "verify-root"
    assert config.verify.sub_model.model == "verify-sub"
    assert config.target("verify", "root_model") is config.verify.root_model
    assert config.fast.model.parameters == {
        "temperature": 0.2,
        "top_p": 0.9,
    }
    assert config.selection.fast == "fast"
    assert config.selection.fallbacks == {}


def test_selection_keeps_fast_engine_and_configures_opt_in_fallback_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "global"
    selection = """
[selection]
fast = "fast"
fallbacks = { fast = ["verify"] }
"""
    _write_config(home, selection + _complete_config())
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    monkeypatch.setenv("FAST_KEY", "remote-secret")

    config = load_global_config()
    registry = create_engine_registry()
    rendered = render_global_config(config)

    assert config.selection.fast == "fast"
    assert config.selection.fallbacks == {"fast": ("verify",)}
    assert isinstance(registry.get("fast"), CodeScoutEngine)
    assert "fast: fast" in rendered
    assert "fast -> verify" in rendered


@pytest.mark.parametrize(
    ("selection", "expected"),
    (
        ('fast = "missing"', "Selection.fast must be 'fast'"),
        (
            'fast = "fast"\nfallbacks = { fast = ["fast"] }',
            "must not select itself",
        ),
        (
            'fast = "fast"\nfallbacks = { fast = ["verify", "verify"] }',
            "contains duplicates",
        ),
    ),
)
def test_selection_rejects_ambiguous_or_unknown_policy(
    tmp_path: Path,
    selection: str,
    expected: str,
) -> None:
    home = tmp_path / "global"
    _write_config(home, f"[selection]\n{selection}\n" + _complete_config())

    with pytest.raises(ConfigError) as caught:
        load_global_config(environment=_environment(home, FAST_KEY="remote-secret"))

    assert expected in (caught.value.hint or "")


def test_workspace_yaml_cannot_select_global_models(tmp_path: Path) -> None:
    global_home = tmp_path / "global"
    workspace = tmp_path / "workspace"
    workspace_config = workspace / ".code-scout" / "config.yaml"
    workspace_config.parent.mkdir(parents=True)
    workspace_config.write_text(
        "engines:\n  fast:\n    model: workspace-model\n",
        encoding="utf-8",
    )
    _write_config(global_home, _complete_config())

    config = load_global_config(
        environment=_environment(global_home, FAST_KEY="remote-secret")
    )

    assert config.fast.model.model == "OpenHands/CodeScout-4B"


def test_default_registry_routes_placeholder_models_from_global_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "global"
    _write_config(home, _complete_config())
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    monkeypatch.setenv("FAST_KEY", "remote-secret")

    registry = create_engine_registry()

    fast = registry.get("fast")
    verify = registry.get("verify")
    assert isinstance(fast, CodeScoutEngine)
    assert isinstance(verify, VerifyEngine)
    assert fast.target is not None
    assert fast.max_turns == 9
    assert fast.target.model == "OpenHands/CodeScout-4B"
    assert verify.config.root_model.model == "verify-root"
    assert verify.config.sub_model.model == "verify-sub"


@pytest.mark.parametrize("model_identifier", CODESCOUT_MODEL_IDENTIFIERS)
def test_global_fast_section_supports_released_codescout_checkpoint_identifiers(
    tmp_path: Path,
    model_identifier: str,
) -> None:
    home = tmp_path / "global"
    config_text = _complete_config().replace(
        'model = "OpenHands/CodeScout-4B"', f'model = "{model_identifier}"'
    )
    _write_config(home, config_text)

    config = load_global_config(
        environment=_environment(home, FAST_KEY="remote-secret")
    )

    assert config.fast.model.model == model_identifier


@pytest.mark.parametrize(
    ("model_identifier", "checkpoint_size"),
    (
        ("OpenHands/CodeScout-1.7B", "1.7B"),
        ("OpenHands/CodeScout-4B", "4B"),
        ("OpenHands/CodeScout-14B", "14B"),
        ("custom-served-alias", "<custom/unknown>"),
    ),
)
def test_fast_diagnostics_report_codescout_checkpoint_size_and_endpoint_readiness(
    tmp_path: Path,
    model_identifier: str,
    checkpoint_size: str,
) -> None:
    home = tmp_path / "global"
    config_text = _complete_config().replace(
        'model = "OpenHands/CodeScout-4B"', f'model = "{model_identifier}"'
    )
    _write_config(home, config_text)
    missing_environment = _environment(home)
    ready_environment = _environment(home, FAST_KEY="redacted-secret")
    config = load_global_config(
        environment=missing_environment,
        require_secrets=False,
    )

    blocked = render_global_config(config, environment=missing_environment)
    ready = render_global_config(config, environment=ready_environment)

    assert f"checkpoint_size: {checkpoint_size}" in blocked
    assert "max_turns: 9" in blocked
    assert "endpoint_readiness: blocked (missing FAST_KEY)" in blocked
    assert (
        "endpoint_readiness: ready (configuration only; endpoint not probed)" in ready
    )
    assert "redacted-secret" not in ready


def test_secret_lookup_is_environment_only_and_diagnostics_are_redacted(
    tmp_path: Path,
) -> None:
    home = tmp_path / "global"
    _write_config(home, _complete_config())
    environment = _environment(home, FAST_KEY="do-not-print-this")
    config = validate_global_config(environment=environment)

    resolved = resolve_model_target(
        config.fast.model,
        name="fast.model",
        environment=environment,
    )
    rendered = render_global_config(config, environment=environment)

    assert resolved.api_key == "do-not-print-this"
    assert "do-not-print-this" not in rendered
    assert "set (redacted)" in rendered
    assert "FAST_KEY" in rendered


def test_missing_config_section_secret_and_malformed_toml_are_actionable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "global"
    environment = _environment(home)

    with pytest.raises(ConfigError, match="not found") as missing:
        load_global_config(environment=environment)
    assert "config init" in (missing.value.hint or "")

    _write_config(home, "[engines.fast.model\nmodel = 'broken'")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_global_config(environment=environment)

    _write_config(home, "[engines]\n")
    with pytest.raises(ConfigError, match="invalid schema") as section:
        load_global_config(environment=environment)
    assert "missing engine section" in (section.value.hint or "").lower()

    _write_config(home, _complete_config())
    with pytest.raises(ConfigError, match="missing secret") as secret:
        load_global_config(environment=environment)
    assert "FAST_KEY" in secret.value.message


@pytest.mark.parametrize(
    ("fast_url", "fast_key", "expected"),
    [
        ("not-a-url", "", "absolute http(s) URL"),
        ("http://localhost:11434/api/generate", "", "OpenAI-compatible root"),
        ("http://localhost:11434/api/chat", "", "OpenAI-compatible root"),
        ("https://models.example.test/v1", "", "must set api_key_env"),
        ("http://localhost:8001/v1", "", None),
        (
            "https://models.example.test/v1",
            'api_key_env = "FAST_KEY"',
            None,
        ),
    ],
)
def test_local_and_remote_endpoint_validation(
    tmp_path: Path,
    fast_url: str,
    fast_key: str,
    expected: str | None,
) -> None:
    home = tmp_path / "global"
    _write_config(home, _complete_config(fast_url=fast_url, fast_key=fast_key))
    environment = _environment(
        home,
        FAST_KEY="fast-secret",
    )

    if expected is None:
        assert load_global_config(environment=environment).fast.model.model
    else:
        with pytest.raises(ConfigError) as caught:
            load_global_config(environment=environment)
        assert expected in (caught.value.hint or "")


def test_config_fallback_commands_use_global_home_and_redact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "global"
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    monkeypatch.setenv("FAST_KEY", "cli-secret")
    app = RootFallbackApp()

    assert _invoke(app, ["config", "path"]) == (0, f"{home / 'config.toml'}\n", "")
    assert _invoke(app, ["config", "init"])[0] == 0
    _write_config(home, _complete_config())

    exit_code, stdout, stderr = _invoke(app, ["config", "show"])
    assert exit_code == 0
    assert stderr == ""
    assert "cli-secret" not in stdout
    assert "OpenHands/CodeScout-4B" in stdout
    assert _invoke(app, ["config", "validate"])[0] == 0


def _invoke(app: RootFallbackApp, args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = app(args)
    return exit_code, stdout.getvalue(), stderr.getvalue()
