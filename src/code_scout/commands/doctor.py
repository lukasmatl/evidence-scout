from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal

from code_scout.core.errors import CommandUsageError, ConfigError
from code_scout.engines.availability import (
    AvailabilityState,
    ProviderAvailability,
    check_provider_availability,
)
from code_scout.engines.verify import RLM_PROVIDER_PACKAGE
from code_scout.extractors.lsp import DEFAULT_TYPESCRIPT_LSP_COMMAND
from code_scout.runtime.config import (
    global_config_path,
    load_global_config,
    resolve_model_target,
)
from code_scout.workspace.config import (
    DEFAULT_CONFIG_PATH,
    ExtractorConfig,
    ExtractorEntryConfig,
    WorkspaceConfig,
    load_workspace_config,
)
from code_scout.workspace.status import (
    WorkspaceStatusReport,
    workspace_status,
)

DOCTOR_REPORT_SCHEMA_VERSION = "1"

Severity = Literal["ok", "warning", "error", "unknown"]
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|authorization|credential|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,}\]]+)",
)


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    severity: Severity
    reason: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "severity": self.severity,
            "reason": self.reason,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DoctorReport:
    workspace: str
    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        return 1 if any(check.severity == "error" for check in self.checks) else 0

    def to_dict(self) -> dict[str, Any]:
        counts = {severity: 0 for severity in ("ok", "warning", "error", "unknown")}
        for check in self.checks:
            counts[check.severity] += 1
        return {
            "schema_version": DOCTOR_REPORT_SCHEMA_VERSION,
            "workspace": self.workspace,
            "summary": {"exit_code": self.exit_code, "counts": counts},
            "checks": [check.to_dict() for check in self.checks],
        }

    def render(self, output_format: str = "text") -> str:
        if output_format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True)
        if output_format != "text":
            raise CommandUsageError(
                f"Unsupported doctor format {output_format!r}.",
                "Use `--format text` or `--format json`.",
            )
        lines = ["Code Scout doctor:", f"Workspace: {self.workspace}"]
        for check in self.checks:
            lines.extend(
                (
                    f"- [{check.severity}] {check.id}: {check.reason}",
                    f"  Remediation: {check.remediation}",
                )
            )
        lines.append(f"Exit code: {self.exit_code}")
        return "\n".join(lines)


def doctor(workspace_path: str | Path = ".") -> DoctorReport:
    environment = dict(os.environ)
    root = Path(workspace_path).expanduser()
    if not root.exists():
        raise CommandUsageError(
            f"Workspace does not exist: {root}.",
            "Pass an existing directory with `--workspace`.",
        )
    if not root.is_dir():
        raise CommandUsageError(
            f"Workspace is not a directory: {root}.",
            "Pass a directory with `--workspace`.",
        )
    root = root.resolve()

    checks: list[DoctorCheck] = []
    workspace_config = _workspace_configuration_check(root, checks, environment)
    status = _status_checks(root, workspace_config, checks, environment)
    _extractor_checks(root, workspace_config, checks)
    _integration_checks(root, workspace_config, checks)
    _grammar_checks(status, checks, environment)
    _global_configuration_checks(checks, environment)
    return DoctorReport(str(root), tuple(checks))


def _workspace_configuration_check(
    root: Path,
    checks: list[DoctorCheck],
    environment: Mapping[str, str],
) -> WorkspaceConfig | None:
    path = root / DEFAULT_CONFIG_PATH
    try:
        config = load_workspace_config(root)
    except ConfigError as error:
        checks.append(
            _check(
                "workspace.configuration",
                "error",
                _error_reason(error, environment),
                error.hint or f"Run `code-scout init --workspace {root}`.",
                environment,
            )
        )
        return None
    checks.append(
        _check(
            "workspace.configuration",
            "ok",
            f"Workspace configuration is valid at {path}.",
            "No action required.",
            environment,
        )
    )
    return config


def _status_checks(
    root: Path,
    config: WorkspaceConfig | None,
    checks: list[DoctorCheck],
    environment: Mapping[str, str],
) -> WorkspaceStatusReport | None:
    if config is None:
        checks.append(
            DoctorCheck(
                "index.readability",
                "unknown",
                "Index readiness cannot be interpreted until workspace "
                "configuration is valid.",
                "Fix workspace.configuration, then rerun `code-scout doctor`.",
            )
        )
        return None
    try:
        status = workspace_status(root)
    except ConfigError as error:
        checks.append(
            _check(
                "index.readability",
                "error",
                _error_reason(error, environment),
                error.hint or f"Run `code-scout scan --workspace {root}`.",
                environment,
            )
        )
        return None

    state = str(status.index["state"])
    if state == "ready":
        checks.append(
            DoctorCheck(
                "index.readability",
                "ok",
                "The workspace index exists, is readable, and has a compatible schema.",
                "No action required.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "index.readability",
                "error",
                f"The workspace index is {state}.",
                status.recommendations[0]
                if status.recommendations
                else f"Run `code-scout scan --workspace {root}`.",
            )
        )

    freshness = str(status.freshness["state"])
    checks.append(
        DoctorCheck(
            "index.freshness",
            "ok"
            if freshness == "fresh"
            else "warning"
            if state == "ready"
            else "unknown",
            (
                "Indexed evidence matches the current workspace content."
                if freshness == "fresh"
                else f"Workspace index freshness is {freshness}."
            ),
            (
                "No action required."
                if freshness == "fresh"
                else f"Run `code-scout scan --workspace {root}` to refresh it."
            ),
        )
    )
    return status


def _extractor_checks(
    root: Path,
    config: WorkspaceConfig | None,
    checks: list[DoctorCheck],
) -> None:
    if config is None:
        checks.append(
            DoctorCheck(
                "extractors.configuration",
                "unknown",
                "Extractor dependencies cannot be evaluated until workspace "
                "configuration is valid.",
                "Fix workspace.configuration, then rerun `code-scout doctor`.",
            )
        )
        return

    for definition in fields(ExtractorConfig):
        name = definition.name
        entry: ExtractorEntryConfig = getattr(config.extractors, name)
        command = entry.command
        if name == "lsp" and entry.enabled and entry.connect is None:
            command = command or DEFAULT_TYPESCRIPT_LSP_COMMAND
        check_id = f"extractor.{name}.executable"
        if not entry.enabled:
            checks.append(
                DoctorCheck(
                    check_id,
                    "ok",
                    f"Extractor {name} is disabled; no executable is required.",
                    "No action required while this extractor remains disabled.",
                )
            )
        elif not command:
            checks.append(
                DoctorCheck(
                    check_id,
                    "ok",
                    f"Extractor {name} has no external executable configured.",
                    "No action required.",
                )
            )
        else:
            checks.append(_executable_check(check_id, command[0], root))

        if entry.watch is not None and entry.watch.command:
            watch_id = f"extractor.{name}.watch_executable"
            if entry.enabled:
                checks.append(_executable_check(watch_id, entry.watch.command[0], root))
            else:
                checks.append(
                    DoctorCheck(
                        watch_id,
                        "ok",
                        f"Extractor {name} watch integration is disabled.",
                        "No action required while this extractor remains disabled.",
                    )
                )


def _integration_checks(
    root: Path,
    config: WorkspaceConfig | None,
    checks: list[DoctorCheck],
) -> None:
    if config is None:
        return
    serena = config.navigation.serena
    if not serena.enabled:
        checks.append(
            DoctorCheck(
                "integration.serena",
                "ok",
                "The optional Serena integration is disabled.",
                "No action required while this integration remains disabled.",
            )
        )
    elif serena.command:
        checks.append(_executable_check("integration.serena", serena.command[0], root))
    else:
        checks.append(
            DoctorCheck(
                "integration.serena",
                "ok",
                "Serena endpoint configuration is ready; reachability was not tested.",
                "No action required for configuration readiness.",
            )
        )

    dede = config.enrichment.dede
    if not dede.enabled:
        checks.append(
            DoctorCheck(
                "integration.dede",
                "ok",
                "The optional DEDE integration is disabled.",
                "No action required while this integration remains disabled.",
            )
        )
    else:
        graph_path = Path(dede.graph_path)
        if not graph_path.is_absolute():
            graph_path = root / graph_path
        if graph_path.is_file():
            checks.append(
                DoctorCheck(
                    "integration.dede",
                    "ok",
                    "The configured DEDE graph is available locally.",
                    "No action required.",
                )
            )
        else:
            checks.append(_executable_check("integration.dede", dede.executable, root))


def _grammar_checks(
    status: WorkspaceStatusReport | None,
    checks: list[DoctorCheck],
    environment: Mapping[str, str],
) -> None:
    if status is None or status.index["state"] != "ready":
        checks.append(
            DoctorCheck(
                "grammar.availability",
                "unknown",
                "Grammar requirements are unknown until a readable index identifies "
                "workspace languages.",
                "Create or repair the index, then rerun `code-scout doctor`.",
            )
        )
        return
    grammars = status.coverage["grammar_support"]
    if not grammars:
        checks.append(
            DoctorCheck(
                "grammar.availability",
                "ok",
                "No indexed files require Tree-sitter grammar checks.",
                "No action required.",
            )
        )
        return
    for grammar in grammars:
        language = str(grammar["grammar_language"])
        check_id = "grammar." + re.sub(r"[^a-z0-9]+", "_", language.casefold()).strip(
            "_"
        )
        state = str(grammar["state"])
        if state == "disabled":
            severity: Severity = "ok"
            reason = f"Tree-sitter extraction for {language} is disabled."
            remediation = (
                "No action required while Tree-sitter extraction remains disabled."
            )
        elif state == "unavailable":
            severity = "error"
            reason = str(
                grammar.get("reason")
                or f"Grammar support for {language} is unavailable."
            )
            package = grammar.get("package")
            remediation = (
                f"Install the optional grammar package `{package}`."
                if package
                else (
                    "Disable Tree-sitter for this language or install a supported "
                    "grammar."
                )
            )
        else:
            severity = "ok"
            reason = f"Tree-sitter grammar support for {language} is installed."
            remediation = "No action required."
        checks.append(_check(check_id, severity, reason, remediation, environment))


def _global_configuration_checks(
    checks: list[DoctorCheck],
    environment: Mapping[str, str],
) -> None:
    try:
        path = global_config_path(environment)
    except ConfigError as error:
        checks.append(
            _check(
                "global.configuration",
                "error",
                _error_reason(error, environment),
                error.hint or "Set CODE_SCOUT_HOME to a valid directory.",
                environment,
            )
        )
        _unconfigured_provider_checks(checks)
        return
    if not path.is_file():
        checks.append(
            DoctorCheck(
                "global.configuration",
                "warning",
                "Global model configuration is absent; the static engine remains "
                "usable.",
                "Run `code-scout config init` only if fast or verify is needed.",
            )
        )
        _unconfigured_provider_checks(checks)
        return
    try:
        config = load_global_config(environment=environment, require_secrets=False)
    except ConfigError as error:
        checks.append(
            _check(
                "global.configuration",
                "error",
                _error_reason(error, environment),
                error.hint or "Fix the global TOML configuration.",
                environment,
            )
        )
        _unconfigured_provider_checks(checks)
        return

    checks.append(
        DoctorCheck(
            "global.configuration",
            "ok",
            f"Global model configuration is valid at {path}.",
            "No action required.",
        )
    )
    _provider_check(
        "provider.fast",
        (("fast.model", config.fast.model),),
        checks,
        environment,
    )
    _provider_check(
        "provider.verify",
        (
            ("verify.root_model", config.verify.root_model),
            ("verify.sub_model", config.verify.sub_model),
        ),
        checks,
        environment,
        dependency=RLM_PROVIDER_PACKAGE,
        executable="docker" if config.verify.isolation == "docker" else None,
        configuration_issue=(
            "Verify Docker isolation requires `isolation_image`."
            if config.verify.isolation == "docker"
            and config.verify.isolation_image is None
            else None
        ),
    )


def _provider_check(
    check_id: str,
    targets: tuple[tuple[str, Any], ...],
    checks: list[DoctorCheck],
    environment: Mapping[str, str],
    *,
    dependency: str | None = None,
    executable: str | None = None,
    configuration_issue: str | None = None,
) -> None:
    failures: list[ConfigError] = []
    for name, target in targets:
        try:
            resolve_model_target(target, name=name, environment=environment)
        except ConfigError as error:
            failures.append(error)
    if failures:
        error = failures[0]
        checks.append(
            _check(
                check_id,
                "error",
                _error_reason(error, environment),
                error.hint or "Set the referenced secret environment variable.",
                environment,
            )
        )
        return
    if configuration_issue is not None:
        checks.append(
            DoctorCheck(
                check_id,
                "error",
                configuration_issue,
                "Configure an isolation image or select local isolation.",
            )
        )
        return
    availability = check_provider_availability(
        target=targets[0][1],
        dependency=dependency,
        executable=executable,
        probe_endpoint=False,
        environment=environment,
    )
    if not availability.ready:
        checks.append(
            DoctorCheck(
                check_id,
                "error",
                availability.message,
                _provider_dependency_remediation(availability, dependency, executable),
            )
        )
        return
    checks.append(
        DoctorCheck(
            check_id,
            "ok",
            "Provider configuration is ready; endpoint reachability was not tested.",
            "No action required for configuration readiness.",
        )
    )


def _provider_dependency_remediation(
    availability: ProviderAvailability,
    dependency: str | None,
    executable: str | None,
) -> str:
    if availability.state is AvailabilityState.MISSING_DEPENDENCY:
        return f"Install the optional Python dependency `{dependency}`."
    if availability.state is AvailabilityState.MISSING_EXECUTABLE:
        return f"Install `{executable}` or change the provider configuration."
    return "Complete the provider configuration and rerun `code-scout doctor`."


def _unconfigured_provider_checks(checks: list[DoctorCheck]) -> None:
    for engine in ("fast", "verify"):
        checks.append(
            DoctorCheck(
                f"provider.{engine}",
                "unknown",
                f"The {engine} provider is not configured; the static engine remains "
                "usable.",
                f"Run `code-scout config init` if the {engine} engine is needed.",
            )
        )


def _executable_check(check_id: str, executable: str, root: Path) -> DoctorCheck:
    candidate = Path(executable).expanduser()
    if candidate.parent != Path("."):
        if not candidate.is_absolute():
            candidate = root / candidate
        available = candidate.is_file() and os.access(candidate, os.X_OK)
    else:
        available = shutil.which(executable) is not None
    if available:
        return DoctorCheck(
            check_id,
            "ok",
            f"Configured executable `{executable}` is available.",
            "No action required.",
        )
    return DoctorCheck(
        check_id,
        "error",
        f"Configured executable `{executable}` was not found or is not executable.",
        f"Install `{executable}` or update the corresponding workspace configuration.",
    )


def _error_reason(error: ConfigError, environment: Mapping[str, str]) -> str:
    return _redact(error.message, environment)


def _check(
    check_id: str,
    severity: Severity,
    reason: str,
    remediation: str,
    environment: Mapping[str, str],
) -> DoctorCheck:
    return DoctorCheck(
        check_id,
        severity,
        _redact(reason, environment),
        _redact(remediation, environment),
    )


def _redact(text: str, environment: Mapping[str, str]) -> str:
    redacted = _SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    for name, value in environment.items():
        if value and _looks_sensitive(name):
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def _looks_sensitive(name: str) -> bool:
    normalized = name.casefold()
    return any(
        part in normalized
        for part in ("key", "token", "secret", "password", "credential")
    )
