from __future__ import annotations

# ruff: noqa: E501
import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from code_scout import __version__
from code_scout import commands as _commands
from code_scout.commands import (
    CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE,
    COMMANDS,
    POSITIONING_TEXT,
    WORKSPACE_HELP,
    CommandOptions,
    CommandSpec,
)
from code_scout.core.errors import CodeScoutError, CommandUsageError
from code_scout.engines import (
    CodeScoutRequest,
    CodeScoutResult,
    EngineRegistry,
    RuntimeOptions,
    create_engine_registry,
)
from code_scout.evidence.assessment import assess_legacy_evidence

__all__ = [
    "COMMANDS",
    "POSITIONING_TEXT",
    "RootFallbackApp",
    "app",
    "execute_scout",
    "render_scout_result",
    "version_callback",
]

try:
    import typer
except ModuleNotFoundError:
    typer = None


def _run_command(command: CommandSpec, args: Sequence[str]) -> str:
    return _commands.run_command(command, args)


class ScoutUsageError(CodeScoutError):
    """The compact scout invocation contains invalid arguments."""


@dataclass(frozen=True)
class ScoutInvocation:
    query: str
    engine: str
    workspace: Path
    runtime: RuntimeOptions
    output_format: str


class _ScoutArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScoutUsageError(message, "Run `code-scout --help` for usage.")


def _turn_budget(value: str) -> int:
    try:
        turns = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 1 <= turns <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return turns


def _scout_parser() -> argparse.ArgumentParser:
    parser = _ScoutArgumentParser(add_help=False, prog="code-scout")
    parser.add_argument("-q", "--query", required=True)
    parser.add_argument("--engine", default="fast")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--max-turns", type=_turn_budget)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _parse_scout_invocation(args: Sequence[str]) -> ScoutInvocation:
    options = _scout_parser().parse_args(list(args))
    query = options.query.strip()
    if not query:
        raise ScoutUsageError(
            "Query must not be empty.",
            "Pass a natural-language question with `-q` or `--query`.",
        )

    workspace = Path(options.workspace).expanduser()
    if not workspace.exists():
        raise ScoutUsageError(
            f"Workspace does not exist: {options.workspace}.",
            "Pass an existing directory with `--workspace`.",
        )
    if not workspace.is_dir():
        raise ScoutUsageError(
            f"Workspace is not a directory: {options.workspace}.",
            "Pass a directory with `--workspace`.",
        )

    return ScoutInvocation(
        query=query,
        workspace=workspace,
        engine=options.engine,
        runtime=RuntimeOptions(
            timeout_seconds=options.timeout_seconds,
            max_turns_override=options.max_turns,
        ),
        output_format=options.format,
    )


def _execute_scout_invocation(
    invocation: ScoutInvocation,
    *,
    registry: EngineRegistry | None = None,
) -> CodeScoutResult:
    request = CodeScoutRequest(
        query=invocation.query,
        workspace=invocation.workspace,
        engine=invocation.engine,
        runtime=invocation.runtime,
    )
    selected_registry = create_engine_registry() if registry is None else registry
    try:
        return selected_registry.execute(request)
    except CodeScoutError:
        raise
    except Exception as error:
        raise CodeScoutError(
            f"Engine '{invocation.engine}' failed: {error}",
            "Check the engine configuration and try again.",
        ) from error


def execute_scout(
    args: Sequence[str],
    *,
    registry: EngineRegistry | None = None,
) -> CodeScoutResult:
    return _execute_scout_invocation(
        _parse_scout_invocation(args),
        registry=registry,
    )


def render_scout_result(result: CodeScoutResult, output_format: str = "text") -> str:
    if output_format == "json":
        return _render_scout_json_result(result)
    if output_format != "text":
        raise ScoutUsageError(
            f"Unsupported query format {output_format!r}.",
            "Use `--format text` or `--format json`.",
        )

    return _render_scout_text_result(result)


def _render_scout_text_result(result: CodeScoutResult) -> str:
    resolved_engine = str(result.metadata.get("resolved_engine", result.engine.name))
    resolved_provider = str(
        result.metadata.get("resolved_provider", result.engine.provider)
    )
    lines = [
        _static_no_match_answer(result, resolved_engine) or result.answer,
        "",
        f"Resolved provider: {resolved_engine} ({resolved_provider})",
    ]
    lines.extend(_static_starting_point_lines(result, resolved_engine))
    lines.extend(_static_needs_review_lines(result, resolved_engine))
    if result.citations:
        lines.extend(("", "Citations:"))
        for citation in result.citations:
            location = f"{citation.path}:{citation.start_line}"
            if citation.end_line != citation.start_line:
                location = f"{location}-{citation.end_line}"
            lines.append(f"- {location}")
            revision = citation.source_revision_id or "revision-unknown"
            lines.append(
                "  Source: "
                f"{revision}; freshness={citation.source_assessment.freshness}"
            )
            for support in citation.supporting_evidence:
                labels = ", ".join(support.assessment.labels)
                lines.append(
                    f"  Evidence {support.evidence_id or 'unmapped'}: {labels}"
                )
    if result.warnings:
        lines.extend(("", "Warnings:"))
        lines.extend(f"- {warning.message}" for warning in result.warnings)
    return "\n".join(lines)


def _static_no_match_answer(
    result: CodeScoutResult,
    resolved_engine: str,
) -> str | None:
    if resolved_engine != "static" or result.metadata.get("result_kind") != "no_match":
        return None
    return (
        f"{result.answer}\n"
        "Refresh static evidence with `code-scout scan --workspace <path>` "
        "before relying on this result."
    )


def _static_starting_point_lines(
    result: CodeScoutResult,
    resolved_engine: str,
) -> list[str]:
    if resolved_engine != "static" or result.metadata.get("result_kind") != "matched":
        return []

    raw_matches = result.metadata.get("matches")
    if not isinstance(raw_matches, Sequence) or isinstance(raw_matches, str):
        return []

    lines: list[str] = []
    for raw_match in raw_matches:
        if not isinstance(raw_match, Mapping):
            continue
        rank = raw_match.get("rank")
        text = raw_match.get("text")
        score = raw_match.get("score")
        reasons = raw_match.get("scoring_reasons")
        location = _static_match_location(raw_match)
        if (
            not isinstance(rank, int)
            or not isinstance(text, str)
            or not text
            or not isinstance(score, int | float)
            or location is None
        ):
            continue
        reason_text = _static_match_reasons(reasons)
        if not lines:
            lines.extend(("", "Likely starting points from static evidence:"))
        lines.append(f"- #{rank} {location}")
        lines.append(f"  Evidence: {text}")
        assessment = assess_legacy_evidence(
            {"assessment": raw_match.get("assessment", {})}
        )
        lines.append(f"  Labels: {', '.join(assessment.labels)}")
        lines.append(f"  Scoring reasons: {reason_text}; score={score:.3f}")
    return lines


def _static_match_location(match: Mapping[object, object]) -> str | None:
    source_file = match.get("source_file")
    line_range = match.get("line_range")
    if isinstance(source_file, str) and source_file:
        if isinstance(line_range, Mapping):
            start = line_range.get("start")
            end = line_range.get("end")
            if isinstance(start, int):
                if isinstance(end, int) and end != start:
                    return f"{source_file}:{start}-{end}"
                return f"{source_file}:{start}"
        return source_file

    owner_entity = match.get("owner_entity")
    if isinstance(owner_entity, Mapping):
        owner_id = owner_entity.get("id")
        if isinstance(owner_id, str) and owner_id:
            return owner_id
    return None


def _static_match_reasons(reasons: object) -> str:
    if not isinstance(reasons, Sequence) or isinstance(reasons, str):
        return "not specified"
    reason_text = [reason for reason in reasons if isinstance(reason, str) and reason]
    if not reason_text:
        return "not specified"
    return ", ".join(reason_text)


def _static_needs_review_lines(
    result: CodeScoutResult,
    resolved_engine: str,
) -> list[str]:
    if resolved_engine != "static" or result.metadata.get("result_kind") != "matched":
        return []

    review_items = _static_review_items(result.metadata)
    if not review_items:
        return []

    lines = ["", "Needs review:"]
    lines.extend(f"- {item}" for item in review_items)
    return lines


def _static_review_items(metadata: Mapping[str, object]) -> list[str]:
    items: list[str] = []
    for field, label in (
        ("needs_review", "Needs review"),
        ("gaps", "Evidence gap"),
        ("warnings", "Warning"),
    ):
        raw_items = metadata.get(field)
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, str):
            continue
        items.extend(
            formatted
            for raw_item in raw_items
            if (formatted := _static_review_item_text(raw_item, label))
        )
    return items


def _static_review_item_text(raw_item: object, label: str) -> str | None:
    if isinstance(raw_item, str):
        return f"{label}: {raw_item}"
    if not isinstance(raw_item, Mapping):
        return None

    message = (
        raw_item.get("message") or raw_item.get("detail") or raw_item.get("reason")
    )
    if not isinstance(message, str) or not message:
        return None

    code = raw_item.get("code")
    if isinstance(code, str) and code:
        return f"{label} [{code}]: {message}"
    return f"{label}: {message}"


def _render_scout_json_result(result: CodeScoutResult) -> str:
    resolved_engine = str(result.metadata.get("resolved_engine", result.engine.name))
    resolved_provider = str(
        result.metadata.get("resolved_provider", result.engine.provider)
    )
    if resolved_engine == "static":
        return _render_static_json_result(result, resolved_engine, resolved_provider)

    payload = {
        "answer": result.answer,
        "engine": resolved_engine,
        "provider": resolved_provider,
        "engine_metadata": {
            "name": result.engine.name,
            "provider": result.engine.provider,
            "provider_version": result.engine.provider_version,
            "model": result.engine.model,
            "requires_evidence": result.engine.requires_evidence,
        },
        "citations": [citation.to_dict() for citation in result.citations],
        "warnings": [
            {
                "code": warning.code,
                "message": warning.message,
            }
            for warning in result.warnings
        ],
        "metadata": _json_safe(result.metadata),
        "timing": {
            "duration_seconds": result.timing.duration_seconds,
            "started_at": _json_safe(result.timing.started_at),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _render_static_json_result(
    result: CodeScoutResult,
    resolved_engine: str,
    resolved_provider: str,
) -> str:
    starting_points = _static_json_starting_points(result.metadata)
    payload = {
        "query": _json_safe(result.metadata.get("query", "")),
        "engine": resolved_engine,
        "provider": resolved_provider,
        "likely_starting_points": starting_points,
        "evidence": _static_json_evidence(starting_points),
        "needs_review": _static_json_needs_review(result.metadata),
        "citations": [citation.to_dict() for citation in result.citations],
        "warnings": [
            {
                "code": warning.code,
                "message": warning.message,
            }
            for warning in result.warnings
        ],
        "metadata": {
            "fallback_used": _json_safe(result.metadata.get("fallback_used")),
            "gap_reason": _json_safe(result.metadata.get("gap_reason")),
            "index_path": _json_safe(result.metadata.get("index_path")),
            "match_count": _json_safe(result.metadata.get("match_count", 0)),
            "query_mode": _json_safe(result.metadata.get("query_mode")),
            "requested_engine": _json_safe(result.metadata.get("requested_engine")),
            "resolved_engine": resolved_engine,
            "resolved_provider": resolved_provider,
            "result_kind": _json_safe(result.metadata.get("result_kind")),
            "timing": {
                "duration_seconds": result.timing.duration_seconds,
                "started_at": _json_safe(result.timing.started_at),
            },
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _static_json_starting_points(
    metadata: Mapping[str, object],
) -> list[dict[str, object]]:
    raw_matches = metadata.get("matches")
    if not isinstance(raw_matches, Sequence) or isinstance(raw_matches, str):
        return []

    starting_points: list[dict[str, object]] = []
    for raw_match in raw_matches:
        if not isinstance(raw_match, Mapping):
            continue

        owner_entity = raw_match.get("owner_entity")
        line_range = raw_match.get("line_range")
        point = {
            "rank": raw_match.get("rank"),
            "evidence_id": raw_match.get("evidence_id"),
            "assessment": _json_safe(
                raw_match.get("assessment") or assess_legacy_evidence({}).to_dict()
            ),
            "score": raw_match.get("score"),
            "source_file": raw_match.get("source_file"),
            "start_line": line_range.get("start")
            if isinstance(line_range, Mapping)
            else None,
            "end_line": line_range.get("end")
            if isinstance(line_range, Mapping)
            else None,
            "owner_entity_id": owner_entity.get("id")
            if isinstance(owner_entity, Mapping)
            else None,
            "scoring_reasons": _json_safe(raw_match.get("scoring_reasons", [])),
        }
        text = raw_match.get("text")
        if isinstance(text, str) and text:
            point["evidence_text"] = text
        starting_points.append(point)
    return starting_points


def _static_json_evidence(
    starting_points: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for point in starting_points:
        evidence_text = point.get("evidence_text")
        if not isinstance(evidence_text, str) or not evidence_text:
            continue
        evidence.append(
            {
                "rank": point.get("rank"),
                "evidence_id": point.get("evidence_id"),
                "assessment": point.get("assessment"),
                "source_file": point.get("source_file"),
                "start_line": point.get("start_line"),
                "end_line": point.get("end_line"),
                "text": evidence_text,
            }
        )
    return evidence


def _static_json_needs_review(
    metadata: Mapping[str, object],
) -> list[object]:
    items: list[object] = []
    for field in ("needs_review", "gaps", "warnings"):
        raw_items = metadata.get(field)
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, str):
            continue
        items.extend(_json_safe(item) for item in raw_items)
    return items


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [_json_safe(item) for item in value]
    return str(value)


class RootFallbackApp:
    def __init__(self, registry: EngineRegistry | None = None) -> None:
        self.registry = registry

    def __call__(self, args: Sequence[str] | None = None) -> int:
        args = list(sys.argv[1:] if args is None else args)

        if args in (["--help"], ["-h"]):
            print(_commands.root_help_text())
            return 0

        if args == ["--version"]:
            print(__version__)
            return 0

        if args[-1:] in (["--help"], ["-h"]):
            command = _commands.find_command(args[:-1])
            if command is not None:
                print(_commands.command_help_text(command))
                return 0

        command = _commands.find_command(_commands.command_path_from_invocation(args))
        if command is not None:
            try:
                output = _run_command(command, args)
                print(output)
            except CodeScoutError as error:
                print(
                    _commands.present_error(error, _requested_command_format(args)),
                    file=sys.stderr,
                )
                return 2 if isinstance(error, CommandUsageError) else 1

            return getattr(output, "exit_code", 0)

        try:
            invocation = _parse_scout_invocation(args)
            result = _execute_scout_invocation(invocation, registry=self.registry)
        except CodeScoutError as error:
            print(_commands.present_error(error), file=sys.stderr)
            return 2 if isinstance(error, ScoutUsageError) else 1

        print(render_scout_result(result, invocation.output_format))
        return 0


def version_callback(value: bool) -> None:
    if typer is None:
        return

    if value:
        typer.echo(__version__)
        raise typer.Exit()


def _requested_command_format(args: Sequence[str]) -> str:
    for index, arg in enumerate(args):
        if arg == "--format" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--format="):
            return arg.split("=", 1)[1]
    return "text"


def _run_typer_command(command: CommandSpec, options: CommandOptions) -> None:
    if typer is None:
        return

    try:
        output = _commands.execute_command(command, options)
    except CodeScoutError as error:
        typer.echo(
            _commands.present_error(error, options.output_format or "text"), err=True
        )
        raise typer.Exit(2 if isinstance(error, CommandUsageError) else 1) from error

    typer.echo(output)
    exit_code = getattr(output, "exit_code", 0)
    if exit_code:
        raise typer.Exit(exit_code)


if typer is not None:

    def _workspace_option() -> Any:
        return typer.Option(
            ".",
            "--workspace",
            help=WORKSPACE_HELP,
            envvar=CODE_SCOUT_WORKSPACE_ENVIRONMENT_VARIABLE,
        )

    app: Any = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help=POSITIONING_TEXT,
        no_args_is_help=False,
    )

    @app.callback(invoke_without_command=True)
    def main(
        context: typer.Context,
        query: str | None = typer.Option(
            None,
            "--query",
            "-q",
            help="Workspace question. Required when no subcommand is used.",
        ),
        engine: str = typer.Option(
            "fast",
            "--engine",
            help="Registered exploration engine.",
            show_default=True,
        ),
        scout_workspace: str = typer.Option(
            ".",
            "--workspace",
            help="Workspace to query.",
            show_default=True,
        ),
        timeout_seconds: float | None = typer.Option(
            None,
            "--timeout-seconds",
            help=(
                "Maximum wall-clock seconds for the exploration request. "
                "Overrides the configured model target timeout when set."
            ),
        ),
        max_turns: int | None = typer.Option(
            None,
            "--max-turns",
            help="Override the configured turn budget for this request.",
            min=1,
            max=100,
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
        version: bool = typer.Option(
            False,
            "--version",
            callback=version_callback,
            help="Show the code-scout version and exit.",
            is_eager=True,
        ),
    ) -> None:
        """Keep code-scout architecture memory close to your workspace."""

        if context.invoked_subcommand is not None:
            return

        scout_args = [
            "--engine",
            engine,
            "--workspace",
            scout_workspace,
        ]
        if timeout_seconds is not None:
            scout_args.extend(("--timeout-seconds", str(timeout_seconds)))
        if max_turns is not None:
            scout_args.extend(("--max-turns", str(max_turns)))
        scout_args.extend(("--format", output_format))
        if query is not None:
            scout_args.extend(("--query", query))
        try:
            invocation = _parse_scout_invocation(scout_args)
            result = _execute_scout_invocation(invocation)
        except CodeScoutError as error:
            typer.echo(_commands.present_error(error), err=True)
            exit_code = 2 if isinstance(error, ScoutUsageError) else 1
            raise typer.Exit(exit_code) from error
        typer.echo(render_scout_result(result, invocation.output_format))

    graph_app = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Inspect local architecture memory graph state.",
        no_args_is_help=True,
    )
    config_app = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Manage global OpenAI-compatible model routing.",
        no_args_is_help=True,
    )

    @config_app.command("init", help=_commands.command(("config", "init")).help)
    def config_init(
        force: bool = typer.Option(
            False,
            "--force",
            help="Overwrite an existing global config file.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("config", "init")),
            CommandOptions(force=force),
        )

    @config_app.command("path", help=_commands.command(("config", "path")).help)
    def config_path() -> None:
        _run_typer_command(
            _commands.command(("config", "path")),
            CommandOptions(),
        )

    @config_app.command("show", help=_commands.command(("config", "show")).help)
    def config_show() -> None:
        _run_typer_command(
            _commands.command(("config", "show")),
            CommandOptions(),
        )

    @config_app.command(
        "validate",
        help=_commands.command(("config", "validate")).help,
    )
    def config_validate() -> None:
        _run_typer_command(
            _commands.command(("config", "validate")),
            CommandOptions(),
        )

    @app.command("init", help=_commands.command(("init",)).help)
    def init(
        workspace: str = _workspace_option(),
        force: bool = typer.Option(
            False,
            "--force",
            help="Overwrite an existing config file.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("init",)),
            CommandOptions(workspace=workspace, force=force),
        )

    @app.command("scan", help=_commands.command(("scan",)).help)
    def scan(
        workspace: str = _workspace_option(),
        extractor: str | None = typer.Option(
            None,
            "--extractor",
            help="Run a single extractor path.",
        ),
        profile: str | None = typer.Option(
            None,
            "--profile",
            help="Run a named extractor profile.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("scan",)),
            CommandOptions(workspace=workspace, extractor=extractor, profile=profile),
        )

    @app.command("changes", help=_commands.command(("changes",)).help)
    def changes(
        workspace: str = _workspace_option(),
        from_snapshot_id: str | None = typer.Option(
            None, "--from", help="Earlier retained snapshot to compare."
        ),
        to_snapshot_id: str | None = typer.Option(
            None, "--to", help="Later retained snapshot to compare."
        ),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
        limit: int = typer.Option(
            100, "--limit", min=1, help="Maximum records to include."
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("changes",)),
            CommandOptions(
                workspace=workspace,
                from_snapshot_id=from_snapshot_id,
                to_snapshot_id=to_snapshot_id,
                output_format=output_format,
                limit=limit,
            ),
        )

    snapshot_app = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Create immutable checkpoints of the committed index.",
        no_args_is_help=True,
    )
    evidence_app = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Inspect retained source evidence.",
        no_args_is_help=True,
    )
    change_app = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Capture and verify explicit dirty-workspace changes.",
        no_args_is_help=True,
    )
    change_session_app = typer.Typer(
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
        help="Manage verification sessions.",
        no_args_is_help=True,
    )

    @change_app.command("begin", help=_commands.command(("change", "begin")).help)
    def change_begin(
        task_label: str = typer.Argument(..., metavar="TASK_LABEL"),
        session_id: str = typer.Option(
            ..., "--session", help="Verification session ID."
        ),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "begin")),
            CommandOptions(
                workspace=workspace,
                session_id=session_id,
                task_label=task_label,
                output_format=output_format,
            ),
        )

    @change_app.command("abandon", help=_commands.command(("change", "abandon")).help)
    def change_abandon(
        attempt_id: str = typer.Argument(..., metavar="CHANGE_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "abandon")),
            CommandOptions(
                workspace=workspace,
                attempt_id=attempt_id,
                output_format=output_format,
            ),
        )

    @change_app.command("verify", help=_commands.command(("change", "verify")).help)
    def change_verify(
        attempt_id: str = typer.Argument(..., metavar="CHANGE_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "verify")),
            CommandOptions(
                workspace=workspace, attempt_id=attempt_id, output_format=output_format
            ),
        )

    @change_app.command("finish", help=_commands.command(("change", "finish")).help)
    def change_finish(
        attempt_id: str = typer.Argument(..., metavar="CHANGE_ID"),
        run_id: str = typer.Option(..., "--run", help="Verified task run ID."),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "finish")),
            CommandOptions(
                workspace=workspace,
                attempt_id=attempt_id,
                run_id=run_id,
                output_format=output_format,
            ),
        )

    @change_app.command("show", help=_commands.command(("change", "show")).help)
    def change_show(
        attempt_id: str = typer.Argument(..., metavar="CHANGE_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "show")),
            CommandOptions(
                workspace=workspace, attempt_id=attempt_id, output_format=output_format
            ),
        )

    @change_app.command("evidence", help=_commands.command(("change", "evidence")).help)
    def change_evidence(
        run_id: str = typer.Argument(..., metavar="RUN_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "evidence")),
            CommandOptions(
                workspace=workspace, run_id=run_id, output_format=output_format
            ),
        )

    @change_session_app.command(
        "finish", help=_commands.command(("change", "session", "finish")).help
    )
    def change_session_finish(
        session_id: str = typer.Argument(..., metavar="SESSION_ID"),
        run_id: str = typer.Option(..., "--run", help="Verified session run ID."),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "session", "finish")),
            CommandOptions(
                workspace=workspace,
                session_id=session_id,
                run_id=run_id,
                output_format=output_format,
            ),
        )

    @change_session_app.command(
        "begin", help=_commands.command(("change", "session", "begin")).help
    )
    def change_session_begin(
        workspace: str = _workspace_option(),
        repositories: list[str] = typer.Option(  # noqa: B008
            [], "--repo", help="Editable ID=PATH."
        ),
        reference_repositories: list[str] = typer.Option(  # noqa: B008
            [], "--reference-repo", help="Reference ID=PATH."
        ),
        policy_path: str = typer.Option(..., "--policy", help="Policy TOML file."),
        profile: str = typer.Option(..., "--profile", help="Policy profile ID."),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "session", "begin")),
            CommandOptions(
                workspace=workspace,
                repositories=tuple(repositories),
                reference_repositories=tuple(reference_repositories),
                policy_path=policy_path,
                profile=profile,
                output_format=output_format,
            ),
        )

    @change_session_app.command(
        "verify", help=_commands.command(("change", "session", "verify")).help
    )
    def change_session_verify(
        session_id: str = typer.Argument(..., metavar="SESSION_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "session", "verify")),
            CommandOptions(
                workspace=workspace, session_id=session_id, output_format=output_format
            ),
        )

    @change_session_app.command(
        "show", help=_commands.command(("change", "session", "show")).help
    )
    def change_session_show(
        session_id: str = typer.Argument(..., metavar="SESSION_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option("text", "--format"),
    ) -> None:
        _run_typer_command(
            _commands.command(("change", "session", "show")),
            CommandOptions(
                workspace=workspace, session_id=session_id, output_format=output_format
            ),
        )

    @snapshot_app.command("create", help=_commands.command(("snapshot", "create")).help)
    def snapshot_create(
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("snapshot", "create")),
            CommandOptions(workspace=workspace, output_format=output_format),
        )

    @snapshot_app.command("list", help=_commands.command(("snapshot", "list")).help)
    def snapshot_list(
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
        limit: int = typer.Option(
            20, "--limit", min=1, help="Maximum snapshots to include."
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("snapshot", "list")),
            CommandOptions(
                workspace=workspace, output_format=output_format, limit=limit
            ),
        )

    @snapshot_app.command("show", help=_commands.command(("snapshot", "show")).help)
    def snapshot_show(
        snapshot_id: str = typer.Argument(..., metavar="SNAPSHOT_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
        limit: int = typer.Option(
            100, "--limit", min=1, help="Maximum facts and evidence to include."
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("snapshot", "show")),
            CommandOptions(
                workspace=workspace,
                output_format=output_format,
                snapshot_id=snapshot_id,
                limit=limit,
            ),
        )

    @evidence_app.command("show", help=_commands.command(("evidence", "show")).help)
    def evidence_show(
        revision_id: str = typer.Argument(..., metavar="REVISION_ID"),
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text", "--format", help="Output format: text or json."
        ),
        start_line: int | None = typer.Option(None, "--start-line", min=1),
        end_line: int | None = typer.Option(None, "--end-line", min=1),
    ) -> None:
        _run_typer_command(
            _commands.command(("evidence", "show")),
            CommandOptions(
                workspace=workspace,
                output_format=output_format,
                revision_id=revision_id,
                start_line=start_line,
                end_line=end_line,
            ),
        )

    @app.command("status", help=_commands.command(("status",)).help)
    def status(
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("status",)),
            CommandOptions(workspace=workspace, output_format=output_format),
        )

    @app.command("doctor", help=_commands.command(("doctor",)).help)
    def doctor(
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("doctor",)),
            CommandOptions(workspace=workspace, output_format=output_format),
        )

    @app.command("impact", help=_commands.command(("impact",)).help)
    def impact(
        workspace: str = _workspace_option(),
        staged: bool = typer.Option(
            False,
            "--staged",
            help="Compare the Git index with HEAD.",
        ),
        base_ref: str | None = typer.Option(
            None,
            "--base",
            help="Compare HEAD with the merge base of REF.",
        ),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
        max_hops: int = typer.Option(
            2,
            "--max-hops",
            help="Maximum dependency traversal depth (1-5).",
        ),
        max_results: int = typer.Option(
            100,
            "--max-results",
            help="Maximum cited impact paths (1-1000).",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("impact",)),
            CommandOptions(
                workspace=workspace,
                staged=staged,
                base_ref=base_ref,
                output_format=output_format,
                max_hops=max_hops,
                max_results=max_results,
            ),
        )

    @graph_app.command("inspect", help=_commands.command(("graph", "inspect")).help)
    def graph_inspect(
        workspace: str = _workspace_option(),
        output_format: str = typer.Option(
            "text",
            "--format",
            help="Output format: text or json.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("graph", "inspect")),
            CommandOptions(workspace=workspace, output_format=output_format),
        )

    @app.command("watch", help=_commands.command(("watch",)).help)
    def watch(
        workspace: str = _workspace_option(),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Print planned work without writing graph state.",
        ),
        repo: str | None = typer.Option(
            None,
            "--repo",
            help="Limit watch planning to one configured repository.",
        ),
        profile: str | None = typer.Option(
            None,
            "--profile",
            help="Run a named extractor profile.",
        ),
        debounce: float = typer.Option(
            0.25,
            "--debounce",
            help="Debounce file events before planning.",
        ),
        changed_only: bool = typer.Option(
            False,
            "--changed-only",
            help="Plan incremental extraction for changed files only.",
        ),
        log: str | None = typer.Option(
            None,
            "--log",
            help="Write watch output to a log file.",
        ),
    ) -> None:
        _run_typer_command(
            _commands.command(("watch",)),
            CommandOptions(
                workspace=workspace,
                dry_run=dry_run,
                repo=repo,
                profile=profile,
                debounce=debounce,
                changed_only=changed_only,
                log=log,
            ),
        )

    app.add_typer(config_app, name="config")
    app.add_typer(graph_app, name="graph")
    app.add_typer(snapshot_app, name="snapshot")
    app.add_typer(evidence_app, name="evidence")
    change_app.add_typer(change_session_app, name="session")
    app.add_typer(change_app, name="change")

else:
    app = RootFallbackApp()


if __name__ == "__main__":
    raise SystemExit(app())
