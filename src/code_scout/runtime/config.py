from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from code_scout.core.errors import ConfigError
from code_scout.engines.contracts import OpenAIModelTarget

CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE = "CODE_SCOUT_HOME"
GLOBAL_CONFIG_FILENAME = "config.toml"
_ENGINE_NAMES = ("fast", "verify")
_FALLBACK_ENGINE_NAMES = (*_ENGINE_NAMES, "static")
_TARGET_FIELDS = {
    "base_url",
    "model",
    "api_key_env",
    "timeout_seconds",
    "max_tokens",
    "parameters",
}
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_PARAMETER_PARTS = ("api_key", "apikey", "authorization", "secret")
_CODESCOUT_CHECKPOINT = re.compile(r"^OpenHands/CodeScout-(1\.7B|4B|14B)$")


@dataclass(frozen=True)
class EngineModelConfig:
    model: OpenAIModelTarget
    max_turns: int = 8


@dataclass(frozen=True)
class EngineSelectionConfig:
    """Machine-wide alias and explicitly enabled fallback policy."""

    fast: str = "fast"
    fallbacks: Mapping[str, tuple[str, ...]] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fallbacks", MappingProxyType(dict(self.fallbacks)))


@dataclass(frozen=True)
class VerifyModelConfig:
    root_model: OpenAIModelTarget
    sub_model: OpenAIModelTarget
    isolation: str | None = None
    isolation_image: str | None = None
    max_iterations: int = 40
    max_sub_calls: int = 16
    max_output_characters: int = 20_000
    timeout_seconds: float = 300.0
    max_total_tokens: int = 64_000

    def __post_init__(self) -> None:
        if self.isolation is not None:
            object.__setattr__(self, "isolation", self.isolation.casefold())
        if not 0 < self.timeout_seconds <= 3_600:
            raise ValueError("verify timeout_seconds must be between 0 and 3600")
        for name in (
            "max_iterations",
            "max_sub_calls",
            "max_output_characters",
            "max_total_tokens",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"verify {name} must be greater than zero")
        ceilings = {
            "max_iterations": 100,
            "max_sub_calls": 1_000,
            "max_output_characters": 1_000_000,
            "max_total_tokens": 10_000_000,
        }
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise ValueError(f"verify {name} must not exceed {ceiling}")


@dataclass(frozen=True)
class GlobalModelConfig:
    selection: EngineSelectionConfig
    fast: EngineModelConfig
    verify: VerifyModelConfig
    path: Path

    def target(self, engine: str, role: str = "model") -> OpenAIModelTarget:
        normalized = engine.strip().casefold()
        if normalized == "fast":
            return self.fast.model
        if normalized == "verify" and role in {"root_model", "sub_model"}:
            return getattr(self.verify, role)
        if normalized == "verify":
            raise ConfigError(
                "The verify engine requires a model role.",
                "Select `root_model` or `sub_model`.",
            )
        raise ConfigError(
            f"Unknown model-backed engine: {engine}.",
            f"Choose one of: {', '.join(_ENGINE_NAMES)}.",
        )


@dataclass(frozen=True)
class ResolvedModelTarget:
    target: OpenAIModelTarget
    api_key: str | None


def code_scout_home(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    override = env.get(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE)
    if override is not None:
        if not override.strip():
            raise ConfigError(
                "CODE_SCOUT_HOME is empty.",
                "Set it to a directory or remove it to use ~/.code-scout.",
            )
        return Path(override).expanduser()
    return Path.home() / ".code-scout"


def global_config_path(environment: Mapping[str, str] | None = None) -> Path:
    return code_scout_home(environment) / GLOBAL_CONFIG_FILENAME


def create_global_config(
    *,
    force: bool = False,
    environment: Mapping[str, str] | None = None,
) -> Path:
    path = global_config_path(environment)
    if path.exists() and not force:
        raise ConfigError(
            f"Global code-scout config already exists at {path}.",
            "Pass `--force` to overwrite it.",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_default_global_config_toml(), encoding="utf-8")
    return path


def load_global_config(
    *,
    environment: Mapping[str, str] | None = None,
    require_secrets: bool = True,
) -> GlobalModelConfig:
    path = global_config_path(environment)
    try:
        config_bytes = path.read_bytes()
    except FileNotFoundError as error:
        raise ConfigError(
            f"Global code-scout config not found at {path}.",
            "Run `code-scout config init` to create it.",
        ) from error
    try:
        raw = tomllib.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(
            f"Global code-scout config at {path} is not valid TOML.",
            f"Fix the TOML syntax: {error}.",
        ) from error

    config = _global_config_from_mapping(raw, path)
    if require_secrets:
        for name, target in _all_targets(config):
            resolve_model_target(target, name=name, environment=environment)
    return config


def resolve_model_target(
    target: OpenAIModelTarget,
    *,
    name: str,
    environment: Mapping[str, str] | None = None,
) -> ResolvedModelTarget:
    env = os.environ if environment is None else environment
    if target.api_key_env is None:
        return ResolvedModelTarget(target=target, api_key=None)
    api_key = env.get(target.api_key_env)
    if api_key is None or not api_key.strip():
        raise ConfigError(
            f"Model target '{name}' references missing secret environment variable "
            f"{target.api_key_env}.",
            f"Set {target.api_key_env} before using this model target.",
        )
    return ResolvedModelTarget(target=target, api_key=api_key)


def validate_global_config(
    *,
    environment: Mapping[str, str] | None = None,
) -> GlobalModelConfig:
    return load_global_config(environment=environment, require_secrets=True)


def render_global_config(
    config: GlobalModelConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Render normalized diagnostics without ever printing secret values."""

    env = os.environ if environment is None else environment
    fallback_text = ", ".join(
        f"{name} -> {' -> '.join(order)}"
        for name, order in config.selection.fallbacks.items()
    )
    lines = [
        f"Global config: {config.path}",
        "Selection:",
        f"  fast: {config.selection.fast}",
        f"  fallbacks: {fallback_text or '<disabled>'}",
        "Engines:",
    ]
    for name, target in _all_targets(config):
        secret_status = "not required"
        if target.api_key_env is not None:
            secret_status = (
                "set (redacted)" if env.get(target.api_key_env) else "missing"
            )
        lines.extend(
            (
                f"  {name}:",
                f"    base_url: {target.base_url}",
                f"    model: {target.model}",
                f"    api_key_env: {target.api_key_env or '<none>'}",
                f"    secret: {secret_status}",
                f"    timeout_seconds: {target.timeout_seconds:g}",
                f"    max_tokens: {target.max_tokens}",
                f"    parameters: {_redacted_parameters(target.parameters)}",
            )
        )
        if name == "fast.model":
            checkpoint = _CODESCOUT_CHECKPOINT.fullmatch(target.model)
            endpoint_ready = target.api_key_env is None or bool(
                env.get(target.api_key_env)
            )
            lines.extend(
                (
                    f"    max_turns: {config.fast.max_turns}",
                    "    checkpoint_size: "
                    f"{checkpoint.group(1) if checkpoint else '<custom/unknown>'}",
                    "    endpoint_readiness: "
                    + (
                        "ready (configuration only; endpoint not probed)"
                        if endpoint_ready
                        else f"blocked (missing {target.api_key_env})"
                    ),
                    "    released_checkpoints: OpenHands/CodeScout-1.7B, "
                    "OpenHands/CodeScout-4B, OpenHands/CodeScout-14B",
                    "    model_lifecycle: endpoint-only; code-scout does not "
                    "download weights or start model servers",
                )
            )
    return "\n".join(lines)


def _global_config_from_mapping(
    raw: Mapping[str, Any],
    path: Path,
) -> GlobalModelConfig:
    unknown_top_level = set(raw) - {"selection", "engines"}
    if unknown_top_level:
        _schema_error(path, f"unknown top-level key: {sorted(unknown_top_level)[0]}")
    selection = _selection_config(raw.get("selection", {}), path)
    engines = _mapping(raw.get("engines"), path, "engines")
    unknown_engines = set(engines) - set(_ENGINE_NAMES)
    if unknown_engines:
        _schema_error(path, f"unknown engine section: {sorted(unknown_engines)[0]}")
    for engine_name in _ENGINE_NAMES:
        if engine_name not in engines:
            _schema_error(path, f"missing engine section [engines.{engine_name}]")

    fast = _fast_engine(engines["fast"], path)
    verify_raw = _mapping(engines["verify"], path, "engines.verify")
    verify_fields = {
        "root_model",
        "sub_model",
        "isolation",
        "isolation_image",
        "max_iterations",
        "max_sub_calls",
        "max_output_characters",
        "timeout_seconds",
        "max_total_tokens",
    }
    unknown_verify = set(verify_raw) - verify_fields
    if unknown_verify:
        _schema_error(path, f"unknown verify model: {sorted(unknown_verify)[0]}")
    for role in ("root_model", "sub_model"):
        if role not in verify_raw:
            _schema_error(path, f"missing section [engines.verify.{role}]")
    try:
        verify = VerifyModelConfig(
            root_model=_model_target(
                verify_raw["root_model"], path, "verify.root_model"
            ),
            sub_model=_model_target(verify_raw["sub_model"], path, "verify.sub_model"),
            isolation=_optional_text(
                verify_raw.get("isolation"), path, "engines.verify.isolation"
            ),
            isolation_image=_optional_text(
                verify_raw.get("isolation_image"),
                path,
                "engines.verify.isolation_image",
            ),
            max_iterations=_positive_integer(
                verify_raw.get("max_iterations", 40),
                path,
                "engines.verify.max_iterations",
            ),
            max_sub_calls=_positive_integer(
                verify_raw.get("max_sub_calls", 16),
                path,
                "engines.verify.max_sub_calls",
            ),
            max_output_characters=_positive_integer(
                verify_raw.get("max_output_characters", 20_000),
                path,
                "engines.verify.max_output_characters",
            ),
            timeout_seconds=_number(
                verify_raw.get("timeout_seconds", 300.0),
                path,
                "engines.verify.timeout_seconds",
            ),
            max_total_tokens=_positive_integer(
                verify_raw.get("max_total_tokens", 64_000),
                path,
                "engines.verify.max_total_tokens",
            ),
        )
    except ValueError as error:
        _schema_error(path, f"invalid verify controller: {error}")
    return GlobalModelConfig(
        selection=selection,
        fast=fast,
        verify=verify,
        path=path,
    )


def _selection_config(value: object, path: Path) -> EngineSelectionConfig:
    raw = _mapping(value, path, "selection")
    unknown = set(raw) - {"fast", "fallbacks"}
    if unknown:
        _schema_error(path, f"unknown key in [selection]: {sorted(unknown)[0]}")
    fast = _text(raw.get("fast", "fast"), path, "selection.fast").casefold()
    if fast != "fast":
        _schema_error(
            path,
            "selection.fast must be 'fast'",
        )
    fallback_raw = _mapping(raw.get("fallbacks", {}), path, "selection.fallbacks")
    fallbacks: dict[str, tuple[str, ...]] = {}
    for provider, order_value in fallback_raw.items():
        normalized_provider = provider.strip().casefold()
        if normalized_provider not in _FALLBACK_ENGINE_NAMES:
            _schema_error(path, f"unknown fallback provider: {provider}")
        if not isinstance(order_value, list) or not all(
            isinstance(item, str) and item.strip() for item in order_value
        ):
            _schema_error(
                path,
                f"selection.fallbacks.{provider} must be an array of provider names",
            )
        order = tuple(item.strip().casefold() for item in order_value)
        invalid = next(
            (item for item in order if item not in _FALLBACK_ENGINE_NAMES),
            None,
        )
        if invalid is not None:
            _schema_error(path, f"unknown fallback provider: {invalid}")
        if normalized_provider in order:
            _schema_error(path, f"fallback order for {provider} must not select itself")
        if len(set(order)) != len(order):
            _schema_error(path, f"fallback order for {provider} contains duplicates")
        if order:
            fallbacks[normalized_provider] = order
    return EngineSelectionConfig(fast=fast, fallbacks=fallbacks)


def _fast_engine(value: object, path: Path) -> EngineModelConfig:
    raw = _mapping(value, path, "engines.fast")
    unknown = set(raw) - {"model", "max_turns"}
    if unknown or "model" not in raw:
        if "model" not in raw:
            _schema_error(path, "missing section [engines.fast.model]")
        _schema_error(
            path,
            f"unknown key in [engines.fast]: {sorted(unknown)[0]}",
        )
    return EngineModelConfig(
        model=_model_target(raw["model"], path, "fast.model"),
        max_turns=_bounded_positive_integer(
            raw.get("max_turns", 12),
            path,
            "engines.fast.max_turns",
            maximum=100,
        ),
    )


def _model_target(value: object, path: Path, name: str) -> OpenAIModelTarget:
    raw = _mapping(value, path, name)
    unknown = set(raw) - _TARGET_FIELDS
    if unknown:
        _schema_error(path, f"unknown field in {name}: {sorted(unknown)[0]}")
    for required in ("base_url", "model"):
        if required not in raw:
            _schema_error(path, f"{name} is missing required field '{required}'")
    base_url = _text(raw["base_url"], path, f"{name}.base_url")
    model = _text(raw["model"], path, f"{name}.model")
    if base_url != base_url.strip():
        _schema_error(path, f"{name}.base_url must not have surrounding whitespace")
    if any(character.isspace() for character in model):
        _schema_error(path, f"{name}.model must not contain whitespace")
    _validate_url(base_url, path, name)
    api_key_env = raw.get("api_key_env")
    if api_key_env is not None:
        api_key_env = _text(api_key_env, path, f"{name}.api_key_env")
        if _ENVIRONMENT_NAME.fullmatch(api_key_env) is None:
            _schema_error(path, f"{name}.api_key_env is not a valid environment name")
    elif not _is_local_url(base_url):
        _schema_error(
            path,
            f"remote model target {name} must set api_key_env; "
            "only local endpoints may be unauthenticated",
        )
    parameters = raw.get("parameters", {})
    if not isinstance(parameters, dict):
        _schema_error(path, f"{name}.parameters must be a TOML table")
    _validate_parameters(parameters, path, name)
    try:
        return OpenAIModelTarget(
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            timeout_seconds=_number(
                raw.get("timeout_seconds", 300.0), path, f"{name}.timeout_seconds"
            ),
            max_tokens=_integer(
                raw.get("max_tokens", 4096), path, f"{name}.max_tokens"
            ),
            parameters=parameters,
        )
    except ValueError as error:
        _schema_error(path, f"invalid {name}: {error}")


def _validate_url(base_url: str, path: Path, name: str) -> None:
    try:
        split = urlsplit(base_url)
        hostname = split.hostname
        port = split.port
    except ValueError:
        hostname = None
        port = None
        split = urlsplit("")
    if split.scheme not in {"http", "https"} or hostname is None:
        _schema_error(path, f"{name}.base_url must be an absolute http(s) URL")
    if port == 0:
        _schema_error(path, f"{name}.base_url must use a valid TCP port")
    if split.username is not None or split.password is not None:
        _schema_error(path, f"{name}.base_url must not contain credentials")
    if split.query or split.fragment:
        _schema_error(path, f"{name}.base_url must not contain a query or fragment")
    if split.path.rstrip("/") in {"/api/generate", "/api/chat"}:
        _schema_error(
            path,
            f"{name}.base_url must use Ollama's OpenAI-compatible root such as "
            "http://localhost:11434/v1, not the native Ollama API path",
        )


def _is_local_url(base_url: str) -> bool:
    hostname = urlsplit(base_url).hostname
    if hostname is None:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_parameters(value: Mapping[str, object], path: Path, name: str) -> None:
    _validate_parameter_mapping(value, path, f"{name}.parameters")


def _validate_parameter_mapping(
    value: Mapping[str, object],
    path: Path,
    prefix: str,
) -> None:
    for key, item in value.items():
        normalized = key.casefold().replace("-", "_")
        if any(part in normalized for part in _SENSITIVE_PARAMETER_PARTS):
            _schema_error(
                path,
                f"{prefix}.{key} may contain a secret; use api_key_env instead",
            )
        if not _is_toml_value(item):
            _schema_error(path, f"{prefix}.{key} has an unsupported value")
        if isinstance(item, dict):
            _validate_parameter_mapping(item, path, f"{prefix}.{key}")


def _is_toml_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_is_toml_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_toml_value(item) for key, item in value.items()
        )
    return False


def _all_targets(
    config: GlobalModelConfig,
) -> tuple[tuple[str, OpenAIModelTarget], ...]:
    return (
        ("fast.model", config.fast.model),
        ("verify.root_model", config.verify.root_model),
        ("verify.sub_model", config.verify.sub_model),
    )


def _mapping(value: object, path: Path, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _schema_error(path, f"{name} must be a TOML table")
    return value


def _text(value: object, path: Path, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _schema_error(path, f"{name} must be non-empty text")
    return value


def _optional_text(value: object, path: Path, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, path, name)


def _number(value: object, path: Path, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _schema_error(path, f"{name} must be a number")
    return float(value)


def _integer(value: object, path: Path, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _schema_error(path, f"{name} must be an integer")
    return value


def _positive_integer(value: object, path: Path, name: str) -> int:
    parsed = _integer(value, path, name)
    if parsed <= 0:
        _schema_error(path, f"{name} must be greater than zero")
    return parsed


def _bounded_positive_integer(
    value: object,
    path: Path,
    name: str,
    *,
    maximum: int,
) -> int:
    parsed = _positive_integer(value, path, name)
    if parsed > maximum:
        _schema_error(path, f"{name} must not exceed {maximum}")
    return parsed


def _schema_error(path: Path, message: str) -> None:
    raise ConfigError(
        f"Global code-scout config at {path} has an invalid schema.",
        message[:1].upper() + message[1:] + ".",
    )


def _redacted_parameters(parameters: Mapping[str, object]) -> str:
    if not parameters:
        return "{}"
    return "{" + ", ".join(f"{key}=<configured>" for key in sorted(parameters)) + "}"


def _default_global_config_toml() -> str:
    return """# Global model routing for code-scout. Secrets stay in the environment.
[selection]
# `fast` is the CodeScout-backed quick repository localization provider.
fast = "fast"
# Fallback is disabled unless an ordered provider list is added explicitly, e.g.:
# fallbacks = { fast = ["verify"] }

[engines.fast]
max_turns = 12

[engines.fast.model]
base_url = "http://localhost:8000/v1"
model = "OpenHands/CodeScout-4B"
timeout_seconds = 300
max_tokens = 4096

[engines.verify]
isolation = "local"
max_iterations = 40
max_sub_calls = 16
max_output_characters = 20000
timeout_seconds = 300
max_total_tokens = 64000

[engines.verify.root_model]
base_url = "http://localhost:8000/v1"
model = "verify-root-model"
timeout_seconds = 300
max_tokens = 4096

[engines.verify.sub_model]
base_url = "http://localhost:8000/v1"
model = "verify-sub-model"
timeout_seconds = 300
max_tokens = 4096
"""
