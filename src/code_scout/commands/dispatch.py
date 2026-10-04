from __future__ import annotations

# ruff: noqa: E501
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from code_scout.core.errors import CodeScoutError
from code_scout.exporters.output_graph import OutputGraphData, load_docs_graph
from code_scout.impact.report import generate_impact_report
from code_scout.models.quality import (
    SUPPORTED_QUALITY_SEVERITIES,
    analyze_graph_quality,
)
from code_scout.pipeline.inspect import (
    DEFAULT_DETAIL_LIMIT,
    DEFAULT_LIST_LIMIT,
    inspect_evidence,
    inspect_snapshot,
    inspect_snapshot_diff,
    inspect_snapshots,
    render_inspection,
    render_snapshot_diff,
)
from code_scout.pipeline.scan import scan_workspace
from code_scout.pipeline.snapshot import snapshot_workspace
from code_scout.pipeline.watch import watch_workspace
from code_scout.runtime.config import (
    create_global_config,
    global_config_path,
    load_global_config,
    render_global_config,
    validate_global_config,
)
from code_scout.storage import (
    ChangeEvent,
    database_path,
    list_change_events,
    open_storage,
)
from code_scout.verification import (
    MemberRole,
    abandon_attempt,
    begin_attempt,
    begin_session,
    finish_attempt,
    finish_session,
    inspect_run,
    show_attempt,
    show_session,
    verify,
)
from code_scout.verification.attempts import _load_attempt
from code_scout.workspace.config import create_workspace_config
from code_scout.workspace.status import workspace_status

from .doctor import doctor
from .parser import CommandOptions, options_from_invocation
from .specs import CommandSpec


class CommandOutput(str):
    """Rendered command output carrying an adapter-independent exit status."""

    exit_code: int

    def __new__(cls, value: str, *, exit_code: int = 0) -> CommandOutput:
        instance = super().__new__(cls, value)
        instance.exit_code = exit_code
        return instance


def run_command(command: CommandSpec, args: Sequence[str]) -> str:
    if command.path == ("doctor",):
        _validate_doctor_invocation(args)
    if command.path == ("impact",):
        _validate_impact_invocation(args)
    if command.path == ("changes",):
        _validate_changes_invocation(args)
    if command.path == ("change", "session", "begin"):
        _validate_session_begin_invocation(args)
    if command.path == ("change", "begin"):
        _validate_attempt_begin_invocation(args)
    return execute_command(command, options_from_invocation(args))


def execute_command(command: CommandSpec, options: CommandOptions) -> str:
    if command.path == ("change", "begin"):
        if options.session_id is None or options.task_label is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change begin requires TASK_LABEL and --session.")
        attempt = begin_attempt(
            workspace_root=Path(options.workspace),
            session_id=options.session_id,
            label=options.task_label,
        )
        if (options.output_format or "text") == "json":
            return json.dumps(
                {
                    "change_id": attempt.id,
                    "session_id": attempt.session_id,
                    "label": attempt.label,
                    "before_checkpoint_id": attempt.before_checkpoint_id,
                    "previous_attempt_id": attempt.previous_attempt_id,
                    "lifecycle": attempt.lifecycle.value,
                },
                sort_keys=True,
            )
        return (
            f"Task attempt started: {attempt.id}\n"
            f"Checkpoint: {attempt.before_checkpoint_id}"
        )
    if command.path == ("change", "verify"):
        if options.attempt_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change verify requires CHANGE_ID.")
        _path, payload = _load_attempt(Path(options.workspace), options.attempt_id)
        manifest = verify(
            workspace_root=Path(options.workspace),
            session_id=payload["attempt"]["session_id"],
            attempt_id=options.attempt_id,
        )
        return _verification_output(manifest, options.output_format)
    if command.path == ("change", "finish"):
        if options.attempt_id is None or options.run_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                "change finish requires CHANGE_ID and --run RUN_ID."
            )
        return _inspection_output(
            finish_attempt(
                workspace_root=Path(options.workspace),
                attempt_id=options.attempt_id,
                run_id=options.run_id,
            ),
            options.output_format,
        )
    if command.path == ("change", "session", "verify"):
        if options.session_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change session verify requires SESSION_ID.")
        return _verification_output(
            verify(
                workspace_root=Path(options.workspace), session_id=options.session_id
            ),
            options.output_format,
        )
    if command.path == ("change", "session", "finish"):
        if options.session_id is None or options.run_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                "change session finish requires SESSION_ID and --run RUN_ID."
            )
        return _inspection_output(
            finish_session(
                workspace_root=Path(options.workspace),
                session_id=options.session_id,
                run_id=options.run_id,
            ),
            options.output_format,
        )
    if command.path == ("change", "show"):
        if options.attempt_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change show requires CHANGE_ID.")
        return _inspection_output(
            show_attempt(
                workspace_root=Path(options.workspace), attempt_id=options.attempt_id
            ),
            options.output_format,
        )
    if command.path == ("change", "session", "show"):
        if options.session_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change session show requires SESSION_ID.")
        return _inspection_output(
            show_session(
                workspace_root=Path(options.workspace), session_id=options.session_id
            ),
            options.output_format,
        )
    if command.path == ("change", "evidence"):
        if options.run_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change evidence requires RUN_ID.")
        return _inspection_output(
            inspect_run(workspace_root=Path(options.workspace), run_id=options.run_id),
            options.output_format,
        )
    if command.path == ("change", "abandon"):
        if options.attempt_id is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("change abandon requires CHANGE_ID.")
        attempt = abandon_attempt(
            workspace_root=Path(options.workspace), attempt_id=options.attempt_id
        )
        if (options.output_format or "text") == "json":
            return json.dumps(
                {"change_id": attempt.id, "lifecycle": attempt.lifecycle.value},
                sort_keys=True,
            )
        return f"Task attempt abandoned: {attempt.id}"
    if command.path == ("change", "session", "begin"):
        if options.policy_path is None or options.profile is None:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                "change session begin requires --policy and --profile.",
                "Pass an explicit policy file and profile ID.",
            )
        session = begin_session(
            workspace_root=Path(options.workspace),
            repositories=tuple(
                _member_option(value, MemberRole.EDITABLE)
                for value in options.repositories
            )
            + tuple(
                _member_option(value, MemberRole.REFERENCE)
                for value in options.reference_repositories
            ),
            policy_path=(Path(options.workspace) / options.policy_path).resolve()
            if not Path(options.policy_path).is_absolute()
            else Path(options.policy_path),
            profile_id=options.profile,
        )
        if (options.output_format or "text") == "json":
            return json.dumps(
                {
                    "session_id": session.id,
                    "initial_checkpoint_id": session.initial_checkpoint_id,
                    "policy_digest": session.frozen_policy_digest,
                    "protected_input_digest": session.protected_input_digest,
                    "members": [
                        {
                            "id": member.id,
                            "role": member.role.value,
                            "root": member.root,
                        }
                        for member in session.members
                    ],
                },
                sort_keys=True,
            )
        return (
            f"Session started: {session.id}\n"
            f"Checkpoint: {session.initial_checkpoint_id}"
        )
    if command.path == ("config", "init"):
        config_path = create_global_config(force=options.force)
        return f"Created global code-scout config at {config_path}."

    if command.path == ("config", "path"):
        return str(global_config_path())

    if command.path == ("config", "show"):
        return render_global_config(load_global_config(require_secrets=False))

    if command.path == ("config", "validate"):
        config = validate_global_config()
        return f"Global code-scout config at {config.path} is valid."

    if command.path == ("init",):
        config_path = create_workspace_config(
            options.workspace,
            force=options.force,
        )
        return f"Created code-scout config at {config_path}."

    if command.path == ("scan",):
        return scan_workspace(
            options.workspace,
            extractor=options.extractor,
            profile=options.profile,
        ).summary()

    if command.path == ("status",):
        return workspace_status(options.workspace).render(
            options.output_format or "text"
        )

    if command.path == ("doctor",):
        report = doctor(options.workspace)
        return CommandOutput(
            report.render(options.output_format or "text"),
            exit_code=report.exit_code,
        )

    if command.path == ("impact",):
        resolved_format = options.output_format or "text"
        if resolved_format not in {"text", "json"}:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                f"Unsupported impact format {resolved_format!r}.",
                "Use `--format text` or `--format json`.",
            )
        report = generate_impact_report(
            options.workspace,
            staged=options.staged,
            base_ref=options.base_ref,
            max_hops=options.max_hops,
            max_results=options.max_results,
        )
        return report.render(resolved_format)

    if command.path == ("changes",):
        if (options.from_snapshot_id is None) != (options.to_snapshot_id is None):
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                "changes requires both --from SNAPSHOT_ID and --to SNAPSHOT_ID.",
                "Pass both selectors to compare snapshots, or neither to list events.",
            )
        if options.from_snapshot_id is not None:
            return render_snapshot_diff(
                inspect_snapshot_diff(
                    options.workspace,
                    options.from_snapshot_id,
                    options.to_snapshot_id or "",
                ),
                options.output_format or "text",
                limit=options.limit
                if options.limit is not None
                else DEFAULT_DETAIL_LIMIT,
            )
        return changes_summary(options.workspace)

    if command.path == ("snapshot", "create"):
        resolved_format = options.output_format or "text"
        if resolved_format not in {"text", "json"}:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                f"Unsupported snapshot format {resolved_format!r}.",
                "Use `--format text` or `--format json`.",
            )
        return snapshot_workspace(options.workspace).render(resolved_format)

    if command.path == ("snapshot", "list"):
        return render_inspection(
            inspect_snapshots(
                options.workspace,
                limit=options.limit
                if options.limit is not None
                else DEFAULT_LIST_LIMIT,
            ),
            options.output_format or "text",
            kind="snapshot list",
        )

    if command.path == ("snapshot", "show"):
        if not options.snapshot_id:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                "snapshot show requires SNAPSHOT_ID.",
                "Run `code-scout snapshot list` to find retained snapshot IDs.",
            )
        return render_inspection(
            inspect_snapshot(
                options.workspace,
                options.snapshot_id,
                limit=options.limit
                if options.limit is not None
                else DEFAULT_DETAIL_LIMIT,
            ),
            options.output_format or "text",
            kind="snapshot show",
        )

    if command.path == ("evidence", "show"):
        if not options.revision_id:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                "evidence show requires REVISION_ID.",
                "Use a revision ID retained in snapshot or citation evidence.",
            )
        return render_inspection(
            inspect_evidence(
                options.workspace,
                options.revision_id,
                start_line=options.start_line,
                end_line=options.end_line,
            ),
            options.output_format or "text",
            kind="evidence show",
        )

    if command.path == ("graph", "inspect"):
        return graph_inspect_summary(
            options.workspace,
            options.output_format,
        )

    if command.path == ("watch",):
        return watch_workspace(
            options.workspace,
            dry_run=options.dry_run,
            repo=options.repo,
            profile=options.profile,
            debounce=options.debounce,
            changed_only=options.changed_only,
            log=options.log,
        ).summary()

    return f"code-scout {' '.join(command.path)} is not implemented yet."


def _verification_output(
    manifest: dict[str, Any], output_format: str | None
) -> CommandOutput:
    result = manifest["result"]
    if (output_format or "text") == "json":
        return CommandOutput(
            json.dumps(manifest, sort_keys=True, default=str),
            exit_code=0
            if manifest["outcome"] == "VERIFIED"
            else (2 if manifest["outcome"] == "ERROR" else 1),
        )
    return CommandOutput(
        f"{manifest['outcome']} — declared gates passed: {result['required_gate_pass_count']} / {result['required_gate_count']}\nRun: {manifest['run_id']}",
        exit_code=0
        if manifest["outcome"] == "VERIFIED"
        else (2 if manifest["outcome"] == "ERROR" else 1),
    )


def _inspection_output(value: dict[str, Any], output_format: str | None) -> str:
    if (output_format or "text") == "json":
        return json.dumps(value, sort_keys=True, default=str)
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _validate_doctor_invocation(args: Sequence[str]) -> None:
    for option in ("--workspace", "--format"):
        if option in args and args.index(option) == len(args) - 1:
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                f"Option {option} requires a value.",
                "Run `code-scout doctor --help` for usage.",
            )


def _validate_impact_invocation(args: Sequence[str]) -> None:
    for option in ("--workspace", "--format", "--base", "--max-hops", "--max-results"):
        if option not in args:
            continue
        value_index = args.index(option) + 1
        if value_index == len(args) or args[value_index].startswith("--"):
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                f"Option {option} requires a value.",
                "Run `code-scout impact --help` for usage.",
            )


def _validate_changes_invocation(args: Sequence[str]) -> None:
    for option in ("--workspace", "--format", "--from", "--to", "--limit"):
        if option not in args:
            continue
        value_index = args.index(option) + 1
        if value_index == len(args) or args[value_index].startswith("--"):
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError(
                f"Option {option} requires a value.",
                "Run `code-scout changes --help` for usage.",
            )


def _validate_session_begin_invocation(args: Sequence[str]) -> None:
    for option in (
        "--repo",
        "--reference-repo",
        "--policy",
        "--profile",
        "--workspace",
    ):
        if option in args:
            index = args.index(option) + 1
            if index == len(args) or args[index].startswith("--"):
                from code_scout.core.errors import CommandUsageError

                raise CommandUsageError(
                    f"Option {option} requires a value.",
                    "Run `code-scout change session begin --help` for usage.",
                )


def _validate_attempt_begin_invocation(args: Sequence[str]) -> None:
    if "--session" not in args and not any(
        arg.startswith("--session=") for arg in args
    ):
        from code_scout.core.errors import CommandUsageError

        raise CommandUsageError("change begin requires --session.")
    if "--session" in args:
        index = args.index("--session") + 1
        if index == len(args) or args[index].startswith("--"):
            from code_scout.core.errors import CommandUsageError

            raise CommandUsageError("Option --session requires a value.")


def _member_option(value: str, role: MemberRole) -> tuple[str, str, MemberRole]:
    member_id, separator, path = value.partition("=")
    if not separator or not member_id or not path:
        from code_scout.core.errors import CommandUsageError

        raise CommandUsageError(
            f"Invalid repository selection {value!r}.",
            "Use --repo ID=PATH or --reference-repo ID=PATH.",
        )
    return member_id, path, role


def present_error(error: CodeScoutError, output_format: str = "text") -> str:
    if output_format == "json":
        from code_scout.core.errors import CommandUsageError

        return json.dumps(
            {
                "schema_version": "1",
                "error": {
                    "kind": (
                        "usage"
                        if isinstance(error, CommandUsageError)
                        else "unavailable"
                    ),
                    "message": error.message,
                    "hint": error.hint,
                },
            },
            indent=2,
            sort_keys=True,
        )
    lines = [f"Error: {error.message}"]

    if error.hint:
        lines.append(f"Hint: {error.hint}")

    return "\n".join(lines)


def changes_summary(workspace: str) -> str:
    if not database_path(workspace).exists():
        return "No change events recorded."

    with open_storage(workspace) as connection:
        events = tuple(reversed(list_change_events(connection)))

    if not events:
        return "No change events recorded."

    lines = ["Recent change events:"]
    for event in events:
        lines.extend(_change_event_lines(event))
    return "\n".join(lines)


def _change_event_lines(event: ChangeEvent) -> list[str]:
    extractors = ", ".join(event.extractors_run) or "none"
    return [
        f"- {event.timestamp}",
        f"  Type: {event.type}",
        f"  File: {event.file}",
        f"  Extractors run: {extractors}",
        f"  Facts: +{event.facts_added}/-{event.facts_removed}",
        f"  Nodes updated: {event.nodes_updated}",
    ]


def graph_inspect_summary(workspace: str, output_format: str | None) -> str:
    resolved_format = output_format or "text"
    if resolved_format not in {"text", "json"}:
        raise CodeScoutError(
            f"Unsupported graph inspect format {resolved_format!r}.",
            "Use `--format text` or `--format json`.",
        )

    graph_data = load_docs_graph(Path(workspace))
    summary = _graph_inspect_payload(graph_data)

    if resolved_format == "json":
        return json.dumps(summary, indent=2, sort_keys=True)

    return _graph_inspect_text(summary)


def _graph_inspect_payload(graph_data: OutputGraphData) -> dict[str, Any]:
    findings = analyze_graph_quality(graph_data)
    quality_counts = dict.fromkeys(SUPPORTED_QUALITY_SEVERITIES, 0)
    for finding in findings:
        quality_counts[finding.severity] += 1

    return {
        "entity_counts": {
            "repositories": len(graph_data.repositories),
            "services": len(graph_data.services),
            "contracts": len(graph_data.contracts),
            "concepts": len(graph_data.concepts),
            "decisions": len(graph_data.decisions),
            "files": len(graph_data.files),
        },
        "edge_counts": {
            "edges": len(graph_data.edges),
        },
        "quality_finding_counts": quality_counts,
        "findings": [finding.to_dict() for finding in findings],
    }


def _graph_inspect_text(summary: dict[str, Any]) -> str:
    entity_counts = summary["entity_counts"]
    edge_counts = summary["edge_counts"]
    quality_counts = summary["quality_finding_counts"]
    findings = summary["findings"]
    lines = [
        "Graph inspect summary:",
        "Entities:",
        f"  Repositories: {entity_counts['repositories']}",
        f"  Services: {entity_counts['services']}",
        f"  Contracts: {entity_counts['contracts']}",
        f"  Concepts: {entity_counts['concepts']}",
        f"  Decisions: {entity_counts['decisions']}",
        f"  Files: {entity_counts['files']}",
        "Edges:",
        f"  Edges: {edge_counts['edges']}",
        "Quality findings:",
        f"  Errors: {quality_counts['error']}",
        f"  Warnings: {quality_counts['warning']}",
        f"  Info: {quality_counts['info']}",
    ]

    if not findings:
        lines.append("  No quality findings.")
        return "\n".join(lines)

    lines.append("  Quality findings need review.")
    lines.append("Findings:")
    lines.extend(_graph_inspect_finding_line(finding) for finding in findings)
    return "\n".join(lines)


def _graph_inspect_finding_line(finding: dict[str, Any]) -> str:
    return (
        f"- {finding['severity']} {finding['code']} "
        f"{finding['entity_type']} {finding['entity_id']}: {finding['message']}"
    )
