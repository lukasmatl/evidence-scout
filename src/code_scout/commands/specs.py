from __future__ import annotations

# ruff: noqa: E501
from collections.abc import Sequence
from dataclasses import dataclass

POSITIONING_TEXT = (
    "code-scout keeps local-first architecture memory close to your workspace."
)
WORKSPACE_HELP = (
    "Workspace to read from. Defaults to CODE_SCOUT_WORKSPACE or the current directory."
)
WORKSPACE_OPTION_HELP = (
    "  --workspace PATH  Workspace to read from. Defaults to CODE_SCOUT_WORKSPACE "
    "or the current directory. [default: .]"
)


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    help: str
    workspace: bool = True
    force: bool = False
    extractor: bool = False
    profile: bool = False
    dry_run: bool = False
    repo: bool = False
    debounce: bool = False
    changed_only: bool = False
    log: bool = False
    format: bool = False
    change_selector: bool = False
    traversal_bounds: bool = False
    argument: str | None = None
    limit: bool = False
    line_range: bool = False
    snapshot_comparison: bool = False
    verification_session_begin: bool = False
    verification_attempt: bool = False
    verification_run: bool = False


COMMANDS = (
    CommandSpec(
        ("change", "session", "finish"),
        "Record a final aggregate completion receipt for one verified session run.",
        format=True,
        argument="SESSION_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("change", "finish"),
        "Record a historical completion receipt for one verified task run.",
        format=True,
        argument="CHANGE_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("change", "verify"),
        "Run frozen gates for one active task attempt.",
        format=True,
        argument="CHANGE_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("change", "show"),
        "Show historical task evidence and current applicability.",
        format=True,
        argument="CHANGE_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("change", "evidence"),
        "Validate and show immutable verification run evidence.",
        format=True,
        argument="RUN_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("change", "begin"),
        "Capture the actual current workspace as a task attempt baseline.",
        format=True,
        argument="TASK_LABEL",
        verification_attempt=True,
    ),
    CommandSpec(
        ("change", "abandon"),
        "Close an active task attempt without rolling back workspace files.",
        format=True,
        argument="CHANGE_ID",
        verification_attempt=True,
    ),
    CommandSpec(
        ("change", "session", "begin"),
        "Freeze explicitly selected repositories into a verification session.",
        format=True,
        verification_session_begin=True,
    ),
    CommandSpec(
        ("change", "session", "verify"),
        "Run frozen gates for the final workspace.",
        format=True,
        argument="SESSION_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("change", "session", "show"),
        "Show session lifecycle and historical run applicability.",
        format=True,
        argument="SESSION_ID",
        verification_run=True,
    ),
    CommandSpec(
        ("config", "init"),
        "Create the global code-scout model configuration.",
        workspace=False,
        force=True,
    ),
    CommandSpec(
        ("config", "path"),
        "Print the global code-scout configuration path.",
        workspace=False,
    ),
    CommandSpec(
        ("config", "show"),
        "Show global model routing with secrets redacted.",
        workspace=False,
    ),
    CommandSpec(
        ("config", "validate"),
        "Validate global model routing and referenced secrets.",
        workspace=False,
    ),
    CommandSpec(
        ("init",),
        "Initialize code-scout memory scaffolding for this workspace.",
        force=True,
    ),
    CommandSpec(
        ("scan",),
        "Scan workspace signals into local architecture memory.",
        extractor=True,
        profile=True,
    ),
    CommandSpec(
        ("status",),
        "Report whether indexed evidence matches the workspace.",
        format=True,
    ),
    CommandSpec(
        ("doctor",),
        "Diagnose local configuration and dependency readiness.",
        format=True,
    ),
    CommandSpec(
        ("impact",),
        "Report cited impact for staged or branch changes.",
        format=True,
        change_selector=True,
        traversal_bounds=True,
    ),
    CommandSpec(
        ("changes",),
        "List recent change events or compare retained snapshots.",
        format=True,
        limit=True,
        snapshot_comparison=True,
    ),
    CommandSpec(
        ("snapshot", "create"),
        "Checkpoint the current committed index state.",
        format=True,
    ),
    CommandSpec(
        ("snapshot", "list"),
        "List retained historical snapshots.",
        format=True,
        limit=True,
    ),
    CommandSpec(
        ("snapshot", "show"),
        "Show retained metadata and bounded snapshot detail.",
        format=True,
        argument="SNAPSHOT_ID",
        limit=True,
    ),
    CommandSpec(
        ("evidence", "show"),
        "Print retained source text for a source revision.",
        format=True,
        argument="REVISION_ID",
        line_range=True,
    ),
    CommandSpec(
        ("graph", "inspect"),
        "Print graph counts and quality finding totals.",
        format=True,
    ),
    CommandSpec(
        ("watch",),
        "Watch workspace changes and plan incremental extraction.",
        profile=True,
        dry_run=True,
        repo=True,
        debounce=True,
        changed_only=True,
        log=True,
    ),
)


def root_help_text() -> str:
    return "\n".join(
        [
            POSITIONING_TEXT,
            "",
            "Usage: code-scout -q QUERY [--engine NAME] [--workspace PATH] "
            "[--timeout-seconds SECONDS] [--max-turns COUNT] [--format text|json]",
            "       code-scout [OPTIONS] COMMAND [ARGS]...",
            "",
            "Options:",
            "  -q, --query TEXT   Workspace question. [required]",
            "  --engine NAME      Registered exploration engine. [default: fast]",
            "  --workspace PATH   Workspace to query. [default: .]",
            "  --timeout-seconds SECONDS",
            "                     Maximum request duration. Overrides the",
            "                     configured model target timeout. "
            "[default: 300, configurable]",
            "  --max-turns COUNT  Override configured turn budget for this request.",
            "  --format text|json",
            "  -h, --help         Show this message and exit.",
            "  --version          Show the code-scout version and exit.",
            "",
            "Commands:",
            *[f"  {' '.join(command.path):<23} {command.help}" for command in COMMANDS],
        ],
    )


def command_help_text(command: CommandSpec) -> str:
    lines = [
        command.help,
        "",
        f"Usage: code-scout {' '.join(command.path)} [OPTIONS]",
        "",
        "Options:",
    ]

    if command.workspace:
        lines.append(WORKSPACE_OPTION_HELP)
    if command.force:
        lines.append("  --force          Overwrite an existing config file.")
    if command.extractor:
        lines.append("  --extractor PATH Run a single extractor path.")
    if command.profile:
        lines.append("  --profile NAME   Run a named extractor profile.")
    if command.dry_run:
        lines.append(
            "  --dry-run        Print planned work without writing graph state."
        )
    if command.repo:
        lines.append(
            "  --repo ID        Limit watch planning to one configured repository."
        )
    if command.debounce:
        lines.append("  --debounce SEC   Debounce file events before planning.")
    if command.changed_only:
        lines.append(
            "  --changed-only   Plan incremental extraction for changed files only."
        )
    if command.log:
        lines.append("  --log PATH       Write watch output to a log file.")
    if command.format:
        lines.append("  --format text|json")
    if command.limit:
        default = 20 if command.path == ("snapshot", "list") else 100
        lines.append(
            f"  --limit N        Maximum records to include. [default: {default}]"
        )
    if command.snapshot_comparison:
        lines.append("  --from SNAPSHOT_ID  Earlier retained snapshot to compare.")
        lines.append("  --to SNAPSHOT_ID    Later retained snapshot to compare.")
    if command.line_range:
        lines.append("  --start-line N   First retained line to print.")
        lines.append("  --end-line N     Last retained line to print.")
    if command.change_selector:
        lines.append("  --staged         Compare the Git index with HEAD.")
        lines.append("  --base REF       Compare HEAD with the merge base of REF.")
    if command.traversal_bounds:
        lines.append(
            "  --max-hops N     Maximum dependency traversal depth. [default: 2]"
        )
        lines.append("  --max-results N  Maximum cited impact paths. [default: 100]")
    if command.verification_session_begin:
        lines.append("  --repo ID=PATH   Editable repository (repeatable).")
        lines.append("  --reference-repo ID=PATH  Reference repository (repeatable).")
        lines.append("  --policy PATH    Verification policy TOML file. [required]")
        lines.append("  --profile ID     Frozen policy profile. [required]")
    if command.verification_attempt:
        if command.path == ("change", "begin"):
            lines.append("  --session ID     Existing verification session. [required]")
    if command.verification_run:
        lines.append("  --format text|json")

    lines.append("  -h, --help        Show this message and exit.")
    return "\n".join(lines)


def find_command(args: Sequence[str]) -> CommandSpec | None:
    invocation = tuple(args)
    for command_spec in COMMANDS:
        if invocation == command_spec.path:
            return command_spec
        if (
            command_spec.argument is not None
            and invocation[: len(command_spec.path)] == command_spec.path
            and len(invocation) == len(command_spec.path) + 1
        ):
            return command_spec
    return None


def command(path: tuple[str, ...]) -> CommandSpec:
    command_spec = find_command(path)
    if command_spec is None:
        raise LookupError(f"Unknown command spec: {' '.join(path)}")
    return command_spec
