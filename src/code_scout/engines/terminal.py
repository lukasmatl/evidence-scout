"""Confined, provider-independent terminal access for repository exploration."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic

from code_scout.core.errors import CodeScoutError

_ALLOWED_COMMANDS = frozenset({"rg", "grep", "find", "ls", "sed", "cat", "git"})
_GIT_INSPECTION_COMMANDS = frozenset(
    {
        "blame",
        "describe",
        "diff",
        "grep",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "shortlog",
        "status",
    }
)
_SHELL_CONTROL = frozenset({"&", "&&", ";", ";;", "<", "<<", ">", ">>", "||"})
_FIND_MUTATIONS = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-fls",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-ok",
        "-okdir",
    }
)
_GIT_DANGEROUS_OPTIONS = (
    "--config-env",
    "--contents",
    "--exec-path",
    "--ext-diff",
    "--git-dir",
    "--open-files-in-pager",
    "--output",
    "--paginate",
    "--textconv",
    "--work-tree",
)
_RG_PROCESS_OPTIONS = ("--hostname-bin", "--pre")


class TerminalPolicyError(CodeScoutError):
    """A terminal request is outside the read-only exploration policy."""


@dataclass(frozen=True)
class TerminalLimits:
    """Resource limits shared by all commands in one model tool loop."""

    max_output_bytes: int = 64 * 1024
    command_timeout_seconds: float = 10.0
    turn_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_output_bytes <= 10 * 1024 * 1024:
            raise ValueError("max_output_bytes must be between 1 and 10485760.")
        if not 0 < self.command_timeout_seconds <= 300:
            raise ValueError("command_timeout_seconds must be between 0 and 300.")
        if not 0 < self.turn_timeout_seconds <= 3600:
            raise ValueError("turn_timeout_seconds must be between 0 and 3600.")


@dataclass(frozen=True)
class ValidatedTerminalCommand:
    """A shell-free pipeline accepted by the terminal policy."""

    command: str
    pipeline: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class TerminalObservation:
    """Structured output suitable for returning to a model tool loop."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_seconds: float
    timed_out: bool = False


class TerminalCommandPolicy:
    """Validate a small shell-free language of repository inspection commands."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory.")

    def validate(self, command: str) -> ValidatedTerminalCommand:
        if not command.strip():
            raise self._reject("Command must not be empty.")
        if "\n" in command or "\r" in command:
            raise self._reject("Multiline commands are not allowed.")
        if "$(" in command or "`" in command:
            raise self._reject("Command substitution is not allowed.")

        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError as error:
            raise self._reject("Command could not be parsed safely.") from error

        if any(
            token in _SHELL_CONTROL
            or (token != "|" and token and set(token) <= set("|&;<>"))
            for token in tokens
        ):
            raise self._reject(
                "Shell redirection and compound commands are not allowed."
            )
        stages = self._split_pipeline(tokens)
        for stage in stages:
            self._validate_stage(stage)
        return ValidatedTerminalCommand(command=command, pipeline=stages)

    def _split_pipeline(self, tokens: list[str]) -> tuple[tuple[str, ...], ...]:
        stages: list[tuple[str, ...]] = []
        current: list[str] = []
        for token in tokens:
            if token == "|":
                if not current:
                    raise self._reject("Pipelines must not contain empty commands.")
                stages.append(tuple(current))
                current = []
            else:
                current.append(token)
        if not current:
            raise self._reject("Pipelines must not contain empty commands.")
        stages.append(tuple(current))
        return tuple(stages)

    def _validate_stage(self, stage: tuple[str, ...]) -> None:
        executable = stage[0]
        if executable not in _ALLOWED_COMMANDS:
            raise self._reject(f"Command {executable!r} is not an approved reader.")
        args = stage[1:]
        self._validate_paths(args)
        if executable == "find":
            self._validate_find(args)
        elif executable == "sed":
            self._validate_sed(args)
        elif executable == "rg":
            self._reject_options(args, _RG_PROCESS_OPTIONS, executable)
        elif executable == "git":
            self._validate_git(args)

    def _validate_paths(self, args: tuple[str, ...]) -> None:
        for arg in args:
            values = (arg, arg.split("=", 1)[1]) if "=" in arg else (arg,)
            for value in values:
                if not value or value == "-":
                    continue
                path = PurePosixPath(value.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise self._reject("Absolute paths and traversal are not allowed.")
                if value.startswith("-"):
                    continue
                candidate = (self.workspace / value).resolve()
                if not candidate.is_relative_to(self.workspace):
                    raise self._reject(
                        "Paths through escaping symlinks are not allowed."
                    )

    def _validate_find(self, args: tuple[str, ...]) -> None:
        for arg in args:
            if arg.casefold() in _FIND_MUTATIONS:
                raise self._reject(f"find option {arg!r} can mutate or execute code.")

    def _validate_sed(self, args: tuple[str, ...]) -> None:
        scripts: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in {"-n", "--quiet", "--silent"}:
                index += 1
                continue
            if arg in {"-e", "--expression"}:
                if index + 1 >= len(args):
                    raise self._reject("sed expression option requires a value.")
                scripts.append(args[index + 1])
                index += 2
                continue
            if arg.startswith("--expression="):
                scripts.append(arg.split("=", 1)[1])
                index += 1
                continue
            if arg.startswith("-"):
                raise self._reject(f"sed option {arg!r} is not approved.")
            if not scripts:
                scripts.append(arg)
            index += 1
        if not scripts or any(
            not _is_read_only_sed_script(script) for script in scripts
        ):
            raise self._reject("Only sed line-range printing is allowed.")

    def _validate_git(self, args: tuple[str, ...]) -> None:
        if not args or args[0] not in _GIT_INSPECTION_COMMANDS:
            raise self._reject(
                "git must use an approved read-only inspection subcommand."
            )
        for arg in args[1:]:
            if arg in {"-c", "-C", "-O"} or any(
                arg == option or arg.startswith(f"{option}=")
                for option in _GIT_DANGEROUS_OPTIONS
            ):
                raise self._reject(
                    f"git option {arg!r} can escape the inspection boundary."
                )

    def _reject_options(
        self,
        args: tuple[str, ...],
        options: tuple[str, ...],
        executable: str,
    ) -> None:
        for arg in args:
            if any(arg == option or arg.startswith(f"{option}=") for option in options):
                raise self._reject(
                    f"{executable} option {arg!r} can execute another process."
                )

    @staticmethod
    def _reject(message: str) -> TerminalPolicyError:
        return TerminalPolicyError(
            message, "Use a read-only repository inspection command."
        )


class ConfinedTerminal:
    """Execute accepted terminal commands within per-command and turn budgets."""

    def __init__(
        self,
        workspace: Path,
        *,
        limits: TerminalLimits | None = None,
    ) -> None:
        self.policy = TerminalCommandPolicy(workspace)
        self.workspace = self.policy.workspace
        self.limits = limits or TerminalLimits()
        self._deadline = monotonic() + self.limits.turn_timeout_seconds
        self._environment = _terminal_environment(self.workspace)

    def execute(self, command: str) -> TerminalObservation:
        validated = self.policy.validate(command)
        started = monotonic()
        remaining = self._deadline - started
        if remaining <= 0:
            return TerminalObservation(
                command=command,
                exit_code=124,
                stdout="",
                stderr="Terminal turn budget exhausted.",
                truncated=False,
                duration_seconds=0.0,
                timed_out=True,
            )
        timeout = min(self.limits.command_timeout_seconds, remaining)
        return self._run(validated, started=started, timeout=timeout)

    def _run(
        self,
        validated: ValidatedTerminalCommand,
        *,
        started: float,
        timeout: float,
    ) -> TerminalObservation:
        processes: list[subprocess.Popen[bytes]] = []
        timed_out = False
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            previous_stdout = None
            try:
                for index, stage in enumerate(validated.pipeline):
                    executable = _resolve_executable(
                        stage[0], self._environment["PATH"], self.workspace
                    )
                    execution_stage = _execution_stage(stage)
                    process = subprocess.Popen(
                        (executable, *execution_stage[1:]),
                        cwd=self.workspace,
                        env=self._environment,
                        stdin=previous_stdout,
                        stdout=output
                        if index == len(validated.pipeline) - 1
                        else subprocess.PIPE,
                        stderr=errors,
                        start_new_session=True,
                    )
                    if previous_stdout is not None:
                        previous_stdout.close()
                    previous_stdout = process.stdout
                    processes.append(process)
                for process in processes:
                    remaining = timeout - (monotonic() - started)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(validated.command, timeout)
                    process.wait(timeout=remaining)
            except (OSError, subprocess.TimeoutExpired) as error:
                if isinstance(error, subprocess.TimeoutExpired):
                    timed_out = True
                _terminate(processes)
                if isinstance(error, OSError):
                    errors.write(
                        f"Could not execute command: {error.strerror}.".encode()
                    )

            stdout, stdout_truncated = _read_bounded(
                output, self.limits.max_output_bytes
            )
            remaining_bytes = max(
                0, self.limits.max_output_bytes - len(stdout.encode())
            )
            stderr, stderr_truncated = _read_bounded(errors, remaining_bytes)

        exit_code = (
            124 if timed_out else (processes[-1].returncode if processes else 127)
        )
        if timed_out:
            timeout_message = f"Command timed out after {timeout:g} seconds."
            stderr = f"{stderr}\n{timeout_message}".strip()
        return TerminalObservation(
            command=validated.command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
            duration_seconds=max(0.0, monotonic() - started),
            timed_out=timed_out,
        )


def _is_read_only_sed_script(script: str) -> bool:
    compact = "".join(script.split())
    if not compact:
        return False
    if compact.endswith(("p", "P")):
        compact = compact[:-1]
    parts = compact.split(",")
    return 1 <= len(parts) <= 2 and all(
        part == "$" or (part.isdigit() and int(part) >= 1) for part in parts
    )


def _terminal_environment(workspace: Path) -> dict[str, str]:
    path_entries = []
    for entry in os.get_exec_path():
        candidate = Path(entry)
        if not candidate.is_absolute():
            continue
        try:
            if candidate.resolve().is_relative_to(workspace):
                continue
        except OSError:
            continue
        path_entries.append(entry)
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PAGER": "cat",
        "PATH": os.pathsep.join(path_entries),
        "RIPGREP_CONFIG_PATH": os.devnull,
    }


def _resolve_executable(command: str, path: str, workspace: Path) -> str:
    for directory in path.split(os.pathsep):
        executable = Path(directory) / command
        if not executable.is_file() or not os.access(executable, os.X_OK):
            continue
        resolved = executable.resolve()
        if resolved.is_relative_to(workspace):
            raise PermissionError("Refusing to execute a workspace-owned binary")
        return str(resolved)
    raise FileNotFoundError(f"Approved command {command!r} is not installed")


def _execution_stage(stage: tuple[str, ...]) -> tuple[str, ...]:
    if stage[0] != "git":
        return stage
    return (
        "git",
        "-c",
        "core.fsmonitor=false",
        "--no-pager",
        *stage[1:],
    )


def _terminate(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
    for process in processes:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()


def _read_bounded(file: object, limit: int) -> tuple[str, bool]:
    file.seek(0)  # type: ignore[attr-defined]
    content = file.read(limit + 1)  # type: ignore[attr-defined]
    truncated = len(content) > limit
    return content[:limit].decode("utf-8", errors="replace"), truncated
