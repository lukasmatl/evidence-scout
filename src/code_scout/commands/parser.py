from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from code_scout.core.errors import CodeScoutError, CommandUsageError

CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE = "CODE_SCOUT_WORKSPACE"


@dataclass(frozen=True)
class CommandOptions:
    workspace: str = "."
    force: bool = False
    extractor: str | None = None
    profile: str | None = None
    dry_run: bool = False
    repo: str | None = None
    debounce: float = 0.25
    changed_only: bool = False
    log: str | None = None
    output_format: str | None = None
    manifest: str | None = None
    recording: str | None = None
    live: bool = False
    providers: tuple[str, ...] = ()
    staged: bool = False
    base_ref: str | None = None
    max_hops: int = 2
    max_results: int = 100
    limit: int | None = None
    snapshot_id: str | None = None
    from_snapshot_id: str | None = None
    to_snapshot_id: str | None = None
    revision_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    repositories: tuple[str, ...] = ()
    reference_repositories: tuple[str, ...] = ()
    policy_path: str | None = None
    session_id: str | None = None
    task_label: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None


def command_path_from_invocation(args: Sequence[str]) -> tuple[str, ...]:
    path: list[str] = []
    skip_next = False

    for arg in args:
        if skip_next:
            skip_next = False
            if not arg.startswith("--"):
                continue

        if arg == "--workspace":
            skip_next = True
            continue

        if arg.startswith("--workspace="):
            continue

        if arg == "--extractor":
            skip_next = True
            continue

        if arg.startswith("--extractor="):
            continue

        if arg in {
            "--profile",
            "--repo",
            "--debounce",
            "--log",
            "--format",
            "--manifest",
            "--recording",
            "--provider",
            "--base",
            "--max-hops",
            "--max-results",
            "--limit",
            "--start-line",
            "--end-line",
            "--from",
            "--to",
            "--reference-repo",
            "--policy",
            "--session",
            "--run",
        }:
            skip_next = True
            continue

        if arg.startswith(
            (
                "--profile=",
                "--repo=",
                "--debounce=",
                "--log=",
                "--format=",
                "--manifest=",
                "--recording=",
                "--provider=",
                "--base=",
                "--max-hops=",
                "--max-results=",
                "--limit=",
                "--start-line=",
                "--end-line=",
                "--from=",
                "--to=",
                "--reference-repo=",
                "--policy=",
                "--session=",
                "--run=",
            )
        ):
            continue

        if arg in {
            "--force",
            "--dry-run",
            "--changed-only",
            "--live",
            "--staged",
        }:
            continue

        path.append(arg)

    return tuple(path)


def options_from_invocation(args: Sequence[str]) -> CommandOptions:
    return CommandOptions(
        workspace=_workspace_from_invocation(args),
        force=_has_flag(args, "--force"),
        extractor=_option_from_invocation(args, "--extractor"),
        profile=_option_from_invocation(args, "--profile"),
        dry_run=_has_flag(args, "--dry-run"),
        repo=_option_from_invocation(args, "--repo"),
        debounce=_float_option_from_invocation(args, "--debounce", 0.25),
        changed_only=_has_flag(args, "--changed-only"),
        log=_option_from_invocation(args, "--log"),
        output_format=_option_from_invocation(args, "--format"),
        manifest=_option_from_invocation(args, "--manifest"),
        recording=_option_from_invocation(args, "--recording"),
        live=_has_flag(args, "--live"),
        providers=_options_from_invocation(args, "--provider"),
        staged=_has_flag(args, "--staged"),
        base_ref=_option_from_invocation(args, "--base"),
        max_hops=_int_option_from_invocation(args, "--max-hops", 2),
        max_results=_int_option_from_invocation(args, "--max-results", 100),
        limit=_optional_int_option_from_invocation(args, "--limit"),
        snapshot_id=_command_argument(args, ("snapshot", "show")),
        from_snapshot_id=_option_from_invocation(args, "--from"),
        to_snapshot_id=_option_from_invocation(args, "--to"),
        revision_id=_command_argument(args, ("evidence", "show")),
        start_line=_optional_int_option_from_invocation(args, "--start-line"),
        end_line=_optional_int_option_from_invocation(args, "--end-line"),
        repositories=_options_from_invocation(args, "--repo"),
        reference_repositories=_options_from_invocation(args, "--reference-repo"),
        policy_path=_option_from_invocation(args, "--policy"),
        session_id=(
            _option_from_invocation(args, "--session")
            or _command_argument(args, ("change", "session", "verify"))
            or _command_argument(args, ("change", "session", "show"))
            or _command_argument(args, ("change", "session", "finish"))
        ),
        task_label=_command_argument(args, ("change", "begin")),
        attempt_id=(
            _command_argument(args, ("change", "abandon"))
            or _command_argument(args, ("change", "verify"))
            or _command_argument(args, ("change", "show"))
            or _command_argument(args, ("change", "finish"))
        ),
        run_id=(
            _command_argument(args, ("change", "evidence"))
            or _option_from_invocation(args, "--run")
        ),
    )


def _workspace_from_invocation(args: Sequence[str]) -> str:
    for index, arg in enumerate(args):
        if arg == "--workspace" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--workspace="):
            return arg.split("=", 1)[1]

    return os.environ.get(CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE, ".")


def _has_flag(args: Sequence[str], flag: str) -> bool:
    return flag in args


def _option_from_invocation(args: Sequence[str], option: str) -> str | None:
    for index, arg in enumerate(args):
        if arg == option and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(f"{option}="):
            return arg.split("=", 1)[1]

    return None


def _options_from_invocation(args: Sequence[str], option: str) -> tuple[str, ...]:
    values: list[str] = []
    skip_next = False
    for index, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == option and index + 1 < len(args):
            values.append(args[index + 1])
            skip_next = True
            continue
        if arg.startswith(f"{option}="):
            values.append(arg.split("=", 1)[1])
    return tuple(values)


def _float_option_from_invocation(
    args: Sequence[str],
    option: str,
    default: float,
) -> float:
    value = _option_from_invocation(args, option)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as error:
        raise CodeScoutError(
            f"{option} must be a number.",
            f"Pass `{option}` with a numeric value.",
        ) from error


def _int_option_from_invocation(
    args: Sequence[str],
    option: str,
    default: int,
) -> int:
    value = _option_from_invocation(args, option)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise CommandUsageError(
            f"{option} must be an integer.",
            f"Pass `{option}` with an integer value.",
        ) from error


def _optional_int_option_from_invocation(
    args: Sequence[str], option: str
) -> int | None:
    value = _option_from_invocation(args, option)
    if value is None:
        return None
    return _int_option_from_invocation(args, option, 0)


def _command_argument(args: Sequence[str], path: tuple[str, ...]) -> str | None:
    for index in range(len(args) - len(path)):
        if tuple(args[index : index + len(path)]) == path:
            value_index = index + len(path)
            if value_index < len(args) and not args[value_index].startswith("--"):
                return args[value_index]
    return None
