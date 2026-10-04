"""Strict parser for frozen executable verification profiles."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from code_scout.core.errors import ConfigError
from code_scout.verification.contracts import GateScope, MemberRole

POLICY_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DEFAULT_LIMITS = {
    "stdout_bytes": 1024 * 1024,
    "stderr_bytes": 1024 * 1024,
    "run_payload_bytes": 100 * 1024 * 1024,
    "source_file_bytes": 10 * 1024 * 1024,
    "checkpoint_bytes": 1024 * 1024 * 1024,
}


@dataclass(frozen=True)
class VerificationLimits:
    stdout_bytes: int = _DEFAULT_LIMITS["stdout_bytes"]
    stderr_bytes: int = _DEFAULT_LIMITS["stderr_bytes"]
    run_payload_bytes: int = _DEFAULT_LIMITS["run_payload_bytes"]
    source_file_bytes: int = _DEFAULT_LIMITS["source_file_bytes"]
    checkpoint_bytes: int = _DEFAULT_LIMITS["checkpoint_bytes"]


@dataclass(frozen=True)
class RepositoryPolicy:
    id: str
    protected_paths: tuple[str, ...] = ()
    input_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateDefinition:
    id: str
    scope: GateScope
    repository_ids: tuple[str, ...]
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 600
    required: bool = True


@dataclass(frozen=True)
class VerificationProfile:
    id: str
    repositories: Mapping[str, RepositoryPolicy]
    gates: tuple[GateDefinition, ...]
    workspace_input_paths: tuple[str, ...] = ()
    workspace_protected_paths: tuple[str, ...] = ()
    environment_allowlist: tuple[str, ...] = ()
    limits: VerificationLimits = field(default_factory=VerificationLimits)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repositories", MappingProxyType(dict(self.repositories))
        )


@dataclass(frozen=True)
class VerificationPolicy:
    schema_version: int
    profiles: Mapping[str, VerificationProfile]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))


def parse_verification_policy(source: str | bytes) -> VerificationPolicy:
    """Parse TOML without accepting policy extensions or executable shorthand."""

    try:
        raw = tomllib.loads(
            source.decode("utf-8") if isinstance(source, bytes) else source
        )
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _error(f"invalid TOML: {error}") from error
    _unknown(raw, {"schema_version", "profiles"}, "policy")
    if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise _error("schema_version must be integer 1")
    profile_data = _mapping(raw.get("profiles"), "profiles")
    if not profile_data:
        raise _error("profiles must not be empty")
    profiles: dict[str, VerificationProfile] = {}
    for profile_id, value in profile_data.items():
        _identifier(profile_id, "profile id")
        profiles[profile_id] = _profile(
            profile_id, _mapping(value, f"profiles.{profile_id}")
        )
    return VerificationPolicy(schema_version=POLICY_SCHEMA_VERSION, profiles=profiles)


def load_verification_policy(path: Path) -> VerificationPolicy:
    try:
        return parse_verification_policy(path.read_bytes())
    except OSError as error:
        raise _error(f"could not read policy {path}: {error}") from error


def validate_profile_members(
    profile: VerificationProfile,
    members: tuple[object, ...],
    *,
    workspace_root: Path | None = None,
) -> None:
    """Bind a parsed profile to explicitly selected members and safe real paths."""

    member_ids: list[str] = []
    roots: list[Path] = []
    for member in members:
        member_id = getattr(member, "id", None)
        role = getattr(member, "role", None)
        root = getattr(member, "root", None)
        if not isinstance(member_id, str) or not isinstance(role, MemberRole):
            raise _error("members must provide a valid id and MemberRole")
        _identifier(member_id, "member id")
        if member_id in member_ids:
            raise _error(f"duplicate member id: {member_id}")
        member_ids.append(member_id)
        if root:
            roots.append(Path(root).resolve())
    if set(member_ids) != set(profile.repositories):
        raise _error("policy repositories must exactly match selected member IDs")
    if workspace_root is not None:
        workspace = workspace_root.resolve()
        for root in roots:
            _contained(root, workspace, "member root")
        for pattern in (
            *profile.workspace_input_paths,
            *profile.workspace_protected_paths,
        ):
            _workspace_pattern_has_single_owner(pattern, roots, workspace)
        for gate in profile.gates:
            base = (
                workspace
                if gate.scope is GateScope.WORKSPACE
                else roots[member_ids.index(gate.repository_ids[0])]
            )
            _contained((base / gate.cwd).resolve(), base, f"gate {gate.id} cwd")


def _profile(profile_id: str, raw: Mapping[str, Any]) -> VerificationProfile:
    _unknown(
        raw,
        {
            "environment_allowlist",
            "workspace_input_paths",
            "workspace_protected_paths",
            "repositories",
            "steps",
            "limits",
        },
        f"profile {profile_id}",
    )
    repositories_raw = _mapping(
        raw.get("repositories"), f"profile {profile_id}.repositories"
    )
    if not repositories_raw:
        raise _error(f"profile {profile_id} requires repositories")
    repositories = {
        repo_id: _repository(repo_id, value)
        for repo_id, value in repositories_raw.items()
    }
    steps = _array(raw.get("steps"), f"profile {profile_id}.steps")
    gates = tuple(_gate(item, set(repositories)) for item in steps)
    ids = [gate.id for gate in gates]
    if len(ids) != len(set(ids)):
        raise _error(f"profile {profile_id} contains duplicate gate IDs")
    if not any(gate.required for gate in gates):
        raise _error(f"profile {profile_id} must contain at least one required gate")
    return VerificationProfile(
        id=profile_id,
        repositories=repositories,
        gates=gates,
        workspace_input_paths=_paths(
            raw.get("workspace_input_paths", []), "workspace_input_paths"
        ),
        workspace_protected_paths=_paths(
            raw.get("workspace_protected_paths", []), "workspace_protected_paths"
        ),
        environment_allowlist=_environment_names(raw.get("environment_allowlist", [])),
        limits=_limits(raw.get("limits", {})),
    )


def _repository(repo_id: str, value: object) -> RepositoryPolicy:
    _identifier(repo_id, "repository id")
    raw = _mapping(value, f"repository {repo_id}")
    _unknown(raw, {"protected_paths", "input_paths"}, f"repository {repo_id}")
    return RepositoryPolicy(
        id=repo_id,
        protected_paths=_paths(
            raw.get("protected_paths", []), f"repository {repo_id}.protected_paths"
        ),
        input_paths=_paths(
            raw.get("input_paths", []), f"repository {repo_id}.input_paths"
        ),
    )


def _gate(value: object, repository_ids: set[str]) -> GateDefinition:
    raw = _mapping(value, "step")
    _unknown(
        raw,
        {
            "id",
            "scope",
            "repository",
            "repositories",
            "argv",
            "cwd",
            "timeout_seconds",
            "required",
        },
        "step",
    )
    gate_id = _text(raw.get("id"), "step.id")
    _identifier(gate_id, "gate id")
    scope_text = _text(raw.get("scope"), f"gate {gate_id}.scope")
    try:
        scope = GateScope(scope_text)
    except ValueError as error:
        raise _error(f"gate {gate_id} has invalid scope") from error
    has_repository = "repository" in raw
    has_repositories = "repositories" in raw
    if scope is GateScope.REPOSITORY:
        if not has_repository or has_repositories:
            raise _error(f"repository gate {gate_id} requires only repository")
        dependencies = (
            _known_id(raw["repository"], repository_ids, f"gate {gate_id}.repository"),
        )
    else:
        if has_repository or not has_repositories:
            raise _error(f"workspace gate {gate_id} requires only repositories")
        dependencies = _known_ids(
            raw["repositories"], repository_ids, f"gate {gate_id}.repositories"
        )
        if not dependencies:
            raise _error(f"workspace gate {gate_id} requires dependencies")
    argv = _argv(raw.get("argv"), gate_id)
    cwd = _path(
        _text(raw.get("cwd", "."), f"gate {gate_id}.cwd"), f"gate {gate_id}.cwd"
    )
    timeout = _positive(
        raw.get("timeout_seconds", 600), f"gate {gate_id}.timeout_seconds"
    )
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise _error(f"gate {gate_id}.required must be boolean")
    return GateDefinition(gate_id, scope, dependencies, argv, cwd, timeout, required)


def _limits(value: object) -> VerificationLimits:
    raw = _mapping(value, "limits")
    _unknown(raw, set(_DEFAULT_LIMITS), "limits")
    values = {
        name: _positive(raw.get(name, default), f"limits.{name}")
        for name, default in _DEFAULT_LIMITS.items()
    }
    return VerificationLimits(**values)


def _paths(value: object, name: str) -> tuple[str, ...]:
    return tuple(_path(item, name) for item in _array(value, name))


def _path(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise _error(f"{name} must be a non-empty safe relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts if value != ".")
    ):
        raise _error(f"{name} must not be absolute or traverse parents")
    return value


def _argv(value: object, gate_id: str) -> tuple[str, ...]:
    argv = _array(value, f"gate {gate_id}.argv")
    if not argv:
        raise _error(f"gate {gate_id}.argv must not be empty")
    if not all(
        isinstance(arg, str)
        and arg
        and "\x00" not in arg
        and "\n" not in arg
        and "\r" not in arg
        for arg in argv
    ):
        raise _error(f"gate {gate_id}.argv must contain safe non-empty strings")
    executable = argv[0]
    if Path(executable).is_absolute() or ".." in PurePosixPath(executable).parts:
        raise _error(f"gate {gate_id}.argv executable must not escape its root")
    return tuple(argv)


def _environment_names(value: object) -> tuple[str, ...]:
    names = _array(value, "environment_allowlist")
    if not all(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        for name in names
    ):
        raise _error("environment_allowlist contains an invalid environment name")
    if len(set(names)) != len(names):
        raise _error("environment_allowlist contains duplicates")
    return tuple(names)


def _known_id(value: object, known: set[str], name: str) -> str:
    value = _text(value, name)
    if value not in known:
        raise _error(f"{name} references unknown repository {value}")
    return value


def _known_ids(value: object, known: set[str], name: str) -> tuple[str, ...]:
    values = tuple(_known_id(item, known, name) for item in _array(value, name))
    if len(values) != len(set(values)):
        raise _error(f"{name} contains duplicate repositories")
    return values


def _workspace_pattern_has_single_owner(
    pattern: str, roots: list[Path], workspace: Path
) -> None:
    # Broad globs can include member content even before files exist; reject them.
    first = PurePosixPath(pattern).parts[0] if PurePosixPath(pattern).parts else ""
    if first in {"*", "**", "?"} or any(char in first for char in "*?"):
        raise _error(f"workspace input {pattern!r} has ambiguous repository ownership")
    candidate = (workspace / first).resolve()
    if any(
        candidate == root
        or candidate.is_relative_to(root)
        or root.is_relative_to(candidate)
        for root in roots
    ):
        raise _error(f"workspace input {pattern!r} is owned by a selected repository")


def _contained(candidate: Path, root: Path, name: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise _error(f"{name} escapes its declared root") from error


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _error(f"{name} must be a TOML table")
    return value


def _array(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise _error(f"{name} must be an array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{name} must be non-empty text")
    return value


def _positive(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _error(f"{name} must be a positive integer")
    return value


def _identifier(value: str, name: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise _error(f"{name} must use stable lowercase kebab-case")


def _unknown(raw: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise _error(f"{name} contains unknown field {sorted(unknown)[0]}")


def _error(message: str) -> ConfigError:
    return ConfigError("Verification policy is invalid.", message)
