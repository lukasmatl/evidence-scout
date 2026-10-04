"""OpenHands CodeScout terminal-localization model loop."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Protocol
from uuid import uuid4

from code_scout.core.errors import CodeScoutError
from code_scout.engines.availability import (
    ProviderAvailability,
    check_provider_availability,
)
from code_scout.engines.citations import ObservedSource, bind_citations_to_index
from code_scout.engines.contracts import (
    Citation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    EngineWarning,
    OpenAIModelTarget,
    SupportingEvidence,
    TimingMetadata,
    resolve_timeout_seconds,
)
from code_scout.engines.openai_compat import (
    OpenAIChatMessage,
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAICompatibilityError,
    OpenAIToolCall,
    OpenAIToolDefinition,
    complete_chat,
)
from code_scout.engines.terminal import (
    ConfinedTerminal,
    TerminalLimits,
    TerminalObservation,
)
from code_scout.evidence.assessment import assess_raw_evidence
from code_scout.storage.schema import database_path
from code_scout.workspace.paths import (
    CODESCOUT_TRAJECTORY_DIRECTORY,
    workspace_state_path,
)

CODESCOUT_MODEL_IDENTIFIERS = (
    "OpenHands/CodeScout-1.7B",
    "OpenHands/CodeScout-4B",
    "OpenHands/CodeScout-14B",
)
MAX_PARALLEL_TERMINAL_CALLS = 5
_SECURITY_RISKS = frozenset({"NONE", "UNKNOWN", "LOW", "MEDIUM", "HIGH"})
_SENSITIVE_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "password", "secret", "token"}
)

CODESCOUT_SYSTEM_PROMPT = """You are a specialized code localization agent. Your sole
objective is to identify the files, classes, and functions that need modification for
the user's issue. You have access to the repository through a Linux terminal.

Use the terminal tool to search and inspect the repository with commands such as rg,
grep, find, ls, cat, and line-range sed. You may issue up to 5 independent terminal
calls in one turn. Read large files in targeted chunks and verify every result.

The terminal already starts in the repository workspace. Every path in a terminal
command MUST be repository-relative. Do not use absolute paths, `pwd`, `cd`, shell
redirection, command chaining such as `&&` or `;`, or commands outside rg, grep, find,
ls, cat, line-range sed, and read-only git inspection. Use a safe pipeline only when
needed. If a terminal observation rejects a command, correct the command on the next
turn instead of repeating it.

When localization is complete, you MUST call localization_finish. Include file,
class_name, and function_name when a method needs modification; file and function_name
for a global function; file and class_name for class-level work; or only file for
imports, globals, new top-level entities, or changes without a unique existing entity.
Return repository-relative paths without a leading './'. Prioritize precision and
recall. Do not solve or edit the issue."""


class CodeScoutEngineError(CodeScoutError):
    """The CodeScout model loop could not produce a structured localization."""


@dataclass(frozen=True)
class LocalizationLocation:
    """One released LocalizationFinish location entry."""

    file: str
    class_name: str | None = None
    function_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.file, str) or not self.file.strip():
            raise ValueError("location file must be non-empty text.")
        if self.file != self.file.strip() or "\\" in self.file:
            raise ValueError("location file must be a normalized relative path.")
        path = PurePosixPath(self.file)
        if path.is_absolute() or self.file.startswith("./") or ".." in path.parts:
            raise ValueError("location file must stay relative to the workspace.")
        for name, value in (
            ("class_name", self.class_name),
            ("function_name", self.function_name),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"location {name} must be non-empty text or null.")

    def to_payload(self) -> dict[str, str | None]:
        return {
            "file": self.file,
            "class_name": self.class_name,
            "function_name": self.function_name,
        }


@dataclass(frozen=True)
class LocalizationFinish:
    """Validated payload accepted only from the localization_finish tool."""

    locations: tuple[LocalizationLocation, ...]

    @classmethod
    def from_arguments(cls, arguments: str) -> LocalizationFinish:
        try:
            payload = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                "localization_finish arguments are not valid JSON."
            ) from error
        if not isinstance(payload, dict) or set(payload) != {"locations"}:
            raise ValueError(
                "localization_finish requires exactly one locations field."
            )
        raw_locations = payload["locations"]
        if not isinstance(raw_locations, list):
            raise ValueError("localization_finish locations must be a list.")
        locations: list[LocalizationLocation] = []
        for raw in raw_locations:
            if not isinstance(raw, dict):
                raise ValueError("each localization location must be an object.")
            unknown = set(raw) - {"file", "class_name", "function_name"}
            if unknown or "file" not in raw:
                raise ValueError(
                    "each location requires file and only supports class_name and "
                    "function_name."
                )
            locations.append(
                LocalizationLocation(
                    file=raw["file"],
                    class_name=raw.get("class_name"),
                    function_name=raw.get("function_name"),
                )
            )
        return cls(tuple(locations))

    def to_payload(self) -> dict[str, list[dict[str, str | None]]]:
        return {"locations": [location.to_payload() for location in self.locations]}


class CodeScoutModelClient(Protocol):
    def __call__(
        self,
        target: OpenAIModelTarget,
        request: OpenAIChatRequest,
        *,
        api_key: str | None,
    ) -> OpenAIChatResponse: ...


TerminalFactory = Callable[[Path, TerminalLimits], ConfinedTerminal]


class CodeScoutEngine:
    """Drive CodeScout checkpoints through OpenAI-compatible function calling."""

    metadata = EngineMetadata(
        name="fast",
        provider="OpenHands CodeScout",
        requires_evidence=True,
    )

    def __init__(
        self,
        target: OpenAIModelTarget,
        *,
        api_key: str | None = None,
        model_client: CodeScoutModelClient = complete_chat,
        terminal_factory: TerminalFactory | None = None,
        environment: Mapping[str, str] | None = None,
        max_turns: int | None = None,
    ) -> None:
        if max_turns is not None and not 1 <= max_turns <= 100:
            raise ValueError("max_turns must be between 1 and 100.")
        self.target = target
        self._api_key = api_key
        self._model_client = model_client
        self._terminal_factory = terminal_factory or _terminal_factory
        self._environment = environment
        self.max_turns = max_turns

    def availability(self, *, probe_endpoint: bool = False) -> ProviderAvailability:
        return check_provider_availability(
            target=self.target,
            probe_endpoint=probe_endpoint,
            environment=self._environment,
        )

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        started_at = datetime.now(UTC)
        started = monotonic()
        timeout = resolve_timeout_seconds(
            request.runtime.timeout_seconds, self.target.timeout_seconds
        )
        deadline = started + timeout
        trajectory = _trajectory_path(request.workspace, started_at)
        api_key = self._resolve_api_key()
        terminal = self._terminal_factory(
            request.workspace,
            TerminalLimits(
                command_timeout_seconds=min(10.0, timeout),
                turn_timeout_seconds=timeout,
            ),
        )
        messages = [
            OpenAIChatMessage("system", _system_prompt(self._max_turns(request))),
            OpenAIChatMessage("user", _user_prompt(request)),
        ]
        tools = _tool_definitions()
        tool_calls = 0
        observed_sources: list[ObservedSource] = []
        max_turns = self._max_turns(request)

        for turn in range(1, max_turns + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                self._fail(
                    trajectory,
                    "timeout",
                    f"CodeScout exceeded its {timeout:g}-second timeout.",
                )
            turn_target = replace(
                self.target,
                timeout_seconds=min(self.target.timeout_seconds, remaining),
                max_tokens=min(self.target.max_tokens, request.runtime.max_tokens),
            )
            chat_request = OpenAIChatRequest.from_target(
                turn_target,
                messages,
                tools=tools,
            )
            _append_jsonl(
                trajectory,
                {
                    "event": "model_request",
                    "turn": turn,
                    "target": {
                        "base_url": turn_target.base_url,
                        "model": turn_target.model,
                        "timeout_seconds": turn_target.timeout_seconds,
                        "authentication": (
                            "configured (redacted)" if api_key else "not configured"
                        ),
                    },
                    "request": _redact(chat_request.to_payload()),
                },
            )
            try:
                response = self._model_client(
                    turn_target,
                    chat_request,
                    api_key=api_key,
                )
                message = _first_message(response)
            except OpenAICompatibilityError as error:
                _append_jsonl(
                    trajectory,
                    {
                        "event": "endpoint_error",
                        "turn": turn,
                        "message": "OpenAI-compatible model request failed (redacted).",
                    },
                )
                raise CodeScoutEngineError(
                    "CodeScout model endpoint failed.",
                    "Check the configured endpoint, model, credentials, and trajectory "
                    f"at {trajectory}.",
                ) from error
            messages.append(message)

            if not message.tool_calls:
                self._fail(
                    trajectory,
                    "missing_finish",
                    "CodeScout returned without calling localization_finish.",
                )
            tool_calls += len(message.tool_calls)

            finish = _find_finish(message.tool_calls)
            if finish is not None:
                try:
                    payload = LocalizationFinish.from_arguments(finish.arguments)
                except ValueError as error:
                    self._tool_error(trajectory, messages, turn, finish, str(error))
                    continue
                _append_jsonl(
                    trajectory,
                    {
                        "event": "localization_finish",
                        "turn": turn,
                        **payload.to_payload(),
                    },
                )
                answer, citations, warnings, resolution_metadata = (
                    _translate_localization(payload, request.workspace)
                )
                return CodeScoutResult(
                    answer=answer,
                    citations=bind_citations_to_index(
                        request.workspace,
                        citations,
                        observed_sources=observed_sources,
                    ),
                    warnings=warnings,
                    engine=replace(self.metadata, model=self.target.model),
                    timing=TimingMetadata(
                        duration_seconds=monotonic() - started,
                        started_at=started_at,
                    ),
                    metadata={
                        "trajectory_path": str(trajectory),
                        "localization_finish": payload.to_payload(),
                        "localization_resolution": resolution_metadata,
                        "model_calls": turn,
                        "tool_calls": tool_calls,
                    },
                )

            calls = message.tool_calls
            if len(calls) > MAX_PARALLEL_TERMINAL_CALLS:
                error = (
                    f"At most {MAX_PARALLEL_TERMINAL_CALLS} parallel terminal calls "
                    "are allowed."
                )
                for call in calls:
                    self._tool_error(trajectory, messages, turn, call, error)
                continue
            for call in calls:
                observation = _execute_terminal_call(terminal, call)
                observed_source = _observed_source(call, observation)
                if observed_source is not None:
                    observed_sources.append(observed_source)
                _append_jsonl(
                    trajectory,
                    {
                        "event": "tool_observation",
                        "turn": turn,
                        "tool_call_id": call.id,
                        "tool": call.name,
                        "observation": observation,
                    },
                )
                messages.append(
                    OpenAIChatMessage(
                        "tool",
                        json.dumps(observation, sort_keys=True),
                        tool_call_id=call.id,
                    )
                )

        self._fail(
            trajectory,
            "turn_budget_exhausted",
            "CodeScout exhausted its turn budget without localization_finish.",
        )

    def _max_turns(self, request: CodeScoutRequest) -> int:
        if request.runtime.max_turns_override is not None:
            return request.runtime.max_turns_override
        return request.runtime.max_turns if self.max_turns is None else self.max_turns

    def _resolve_api_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key
        from code_scout.runtime.config import resolve_model_target

        return resolve_model_target(
            self.target,
            name="fast.model",
            environment=self._environment,
        ).api_key

    @staticmethod
    def _tool_error(
        trajectory: Path,
        messages: list[OpenAIChatMessage],
        turn: int,
        call: OpenAIToolCall,
        error: str,
    ) -> None:
        observation = {"error": error}
        _append_jsonl(
            trajectory,
            {
                "event": "tool_observation",
                "turn": turn,
                "tool_call_id": call.id,
                "tool": call.name,
                "observation": observation,
            },
        )
        messages.append(
            OpenAIChatMessage(
                "tool",
                json.dumps(observation, sort_keys=True),
                tool_call_id=call.id,
            )
        )

    @staticmethod
    def _fail(trajectory: Path, code: str, message: str) -> None:
        _append_jsonl(
            trajectory,
            {"event": "loop_error", "code": code, "message": message},
        )
        raise CodeScoutEngineError(
            message,
            f"Inspect the redacted trajectory at {trajectory}.",
        )


def _system_prompt(max_turns: int) -> str:
    return f"{CODESCOUT_SYSTEM_PROMPT}\nYou have up to {max_turns} turns."


def _user_prompt(request: CodeScoutRequest) -> str:
    return f"""The terminal already starts in the selected repository workspace.
Use only repository-relative paths in terminal commands.

<issue_description>
{request.query}
</issue_description>

Act as a code search agent and localize only the specific files, classes, or functions
that need modification. Do not solve the issue. Your final output must be a
localization_finish tool call. Returned paths must be repository-relative without a
leading './'."""


def _tool_definitions() -> tuple[OpenAIToolDefinition, ...]:
    terminal = OpenAIToolDefinition(
        name="terminal",
        description="Execute one read-only repository exploration command.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "security_risk": {
                    "type": "string",
                    "enum": sorted(_SECURITY_RISKS),
                    "description": (
                        "Advisory risk classification; repository command safety is "
                        "enforced independently."
                    ),
                },
                "is_input": {"type": "boolean", "default": False},
                "timeout": {"type": "number"},
                "reset": {"type": "boolean", "default": False},
            },
            "required": ["command", "security_risk"],
            "additionalProperties": False,
        },
    )
    finish = OpenAIToolDefinition(
        name="localization_finish",
        description="Submit the final structured code localization.",
        parameters={
            "type": "object",
            "properties": {
                "locations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "class_name": {"type": ["string", "null"]},
                            "function_name": {"type": ["string", "null"]},
                        },
                        "required": ["file"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["locations"],
            "additionalProperties": False,
        },
    )
    return terminal, finish


def _first_message(response: OpenAIChatResponse) -> OpenAIChatMessage:
    if not response.choices:
        raise OpenAICompatibilityError(
            "The model endpoint returned no completion choices.",
            "Check the OpenAI-compatible server logs and model configuration.",
        )
    return response.choices[0].message


def _find_finish(calls: tuple[OpenAIToolCall, ...]) -> OpenAIToolCall | None:
    return next(
        (call for call in calls if call.name.casefold() == "localization_finish"),
        None,
    )


def _execute_terminal_call(
    terminal: ConfinedTerminal,
    call: OpenAIToolCall,
) -> dict[str, object]:
    if call.name.casefold() != "terminal":
        return {"error": f"Unknown CodeScout tool: {call.name}."}
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("terminal arguments must be an object.")
        unknown = set(arguments) - {
            "command",
            "security_risk",
            "is_input",
            "timeout",
            "reset",
        }
        command = arguments.get("command")
        security_risk = arguments.get("security_risk")
        if unknown or not isinstance(command, str) or not command.strip():
            raise ValueError("terminal requires a non-empty command and known fields.")
        if (
            not isinstance(security_risk, str)
            or security_risk.upper() not in _SECURITY_RISKS
        ):
            raise ValueError("terminal security_risk is missing or invalid.")
        # Released checkpoints may emit OpenHands terminal-session metadata even for
        # ordinary one-shot searches. This adapter has no persistent shell, so the
        # hints are intentionally ignored; ConfinedTerminal still validates every
        # command against the read-only repository policy.
        observation = terminal.execute(command)
    except (CodeScoutError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {"error": str(error)}
    return _terminal_payload(observation)


def _terminal_payload(observation: TerminalObservation) -> dict[str, object]:
    return asdict(observation)


def _observed_source(
    call: OpenAIToolCall, observation: dict[str, object]
) -> ObservedSource | None:
    """Return a complete file identity only for a successful plain ``cat`` call."""
    if call.name.casefold() != "terminal" or observation.get("exit_code") != 0:
        return None
    try:
        arguments = json.loads(call.arguments)
        command = arguments["command"]
        tokens = shlex.split(command)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        len(tokens) != 2
        or tokens[0] != "cat"
        or not isinstance(observation.get("stdout"), str)
        or observation.get("truncated") is not False
    ):
        return None
    path = PurePosixPath(tokens[1])
    if path.is_absolute() or ".." in path.parts or tokens[1].startswith("./"):
        return None
    return ObservedSource(path.as_posix(), str(observation["stdout"]))


def _terminal_factory(workspace: Path, limits: TerminalLimits) -> ConfinedTerminal:
    return ConfinedTerminal(workspace, limits=limits)


@dataclass(frozen=True)
class _ResolvedPrediction:
    location: LocalizationLocation
    citation: Citation | None
    source: str | None
    warning: EngineWarning | None = None


def _translate_localization(
    finish: LocalizationFinish,
    workspace: Path,
) -> tuple[str, tuple[Citation, ...], tuple[EngineWarning, ...], dict[str, object]]:
    predictions: list[_ResolvedPrediction] = []
    seen: set[LocalizationLocation] = set()
    for location in finish.locations:
        if location in seen:
            continue
        seen.add(location)
        predictions.append(_resolve_prediction(location, workspace))

    citations = tuple(
        prediction.citation
        for prediction in predictions
        if prediction.citation is not None
    )
    warnings = tuple(
        prediction.warning
        for prediction in predictions
        if prediction.warning is not None
    )
    answer = _render_localization(predictions)
    metadata = {
        "predictions": [
            {
                **prediction.location.to_payload(),
                "status": (
                    "resolved" if prediction.citation is not None else "unresolved"
                ),
                "source": prediction.source,
                "citation": (
                    prediction.citation.to_dict()
                    if prediction.citation is not None
                    else None
                ),
            }
            for prediction in predictions
        ]
    }
    return answer, citations, warnings, metadata


def _resolve_prediction(
    location: LocalizationLocation,
    workspace: Path,
) -> _ResolvedPrediction:
    path = workspace / Path(*PurePosixPath(location.file).parts)
    try:
        path.resolve().relative_to(workspace.resolve())
    except (OSError, RuntimeError, ValueError):
        return _unresolved(
            location,
            "localization-invalid-path",
            f"Could not resolve CodeScout prediction '{_prediction_label(location)}': "
            "the provider path resolves outside the workspace.",
        )
    if not path.is_file():
        return _unresolved(
            location,
            "localization-invalid-path",
            f"Could not resolve CodeScout prediction '{_prediction_label(location)}': "
            "the provider path is not an existing file.",
        )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return _unresolved(
            location,
            "localization-unreadable-file",
            f"Could not resolve CodeScout prediction '{_prediction_label(location)}': "
            "the provider path is not readable UTF-8 text.",
        )

    if location.class_name is None and location.function_name is None:
        if not lines:
            return _unresolved(
                location,
                "localization-empty-file",
                f"Could not cite empty predicted file '{location.file}'.",
            )
        return _ResolvedPrediction(
            location,
            Citation(location.file, 1, len(lines)),
            "file",
        )

    symbol_ranges = _symbol_ranges(workspace, location, lines)
    if len(symbol_ranges) == 1:
        start_line, end_line = symbol_ranges[0]
        return _ResolvedPrediction(
            location,
            Citation(
                location.file,
                start_line,
                end_line,
                _indexed_support(workspace, location.file, start_line, end_line),
            ),
            "symbol",
        )
    if len(symbol_ranges) > 1:
        return _unresolved(
            location,
            "localization-ambiguous-symbol",
            f"CodeScout prediction '{_prediction_label(location)}' matched "
            f"{len(symbol_ranges)} symbol ranges; no location was guessed.",
        )

    textual_ranges = _textual_ranges(lines, location)
    if len(textual_ranges) == 1:
        start_line, end_line = textual_ranges[0]
        return _ResolvedPrediction(
            location,
            Citation(location.file, start_line, end_line),
            "text",
        )
    if len(textual_ranges) > 1:
        return _unresolved(
            location,
            "localization-ambiguous-text",
            f"CodeScout prediction '{_prediction_label(location)}' matched "
            f"{len(textual_ranges)} textual locations; no location was guessed.",
        )
    return _unresolved(
        location,
        "localization-symbol-not-found",
        f"CodeScout prediction '{_prediction_label(location)}' was not found in "
        "code-scout symbols or bounded textual lookup.",
    )


def _indexed_support(
    workspace: Path, path: str, start: int, end: int
) -> tuple[SupportingEvidence, ...]:
    """Bind only the exact indexed symbol range used by local resolution."""
    storage_path = database_path(workspace)
    try:
        storage_path.resolve().relative_to(workspace.resolve())
        connection = sqlite3.connect(f"file:{storage_path}?mode=ro", uri=True)
        rows = connection.execute(
            """
            SELECT raw.id, raw.metadata_json, raw.provenance FROM symbols
            JOIN files ON files.id = symbols.file_id
            JOIN raw_evidence_units AS raw
              ON raw.owner_entity_type = 'symbol' AND raw.owner_entity_id = symbols.id
             AND raw.source_file = files.path
             AND raw.start_line = symbols.start_line AND raw.end_line = symbols.end_line
            WHERE files.path = ? AND symbols.start_line = ? AND symbols.end_line = ?
            ORDER BY raw.id
            """,
            (path, start, end),
        ).fetchall()
        return tuple(
            SupportingEvidence(
                evidence_id, assess_raw_evidence(json.loads(metadata), provenance)
            )
            for evidence_id, metadata, provenance in rows
        ) or (SupportingEvidence(),)
    except (sqlite3.Error, OSError, RuntimeError, ValueError):
        return (SupportingEvidence(),)
    finally:
        if "connection" in locals():
            connection.close()


def _symbol_ranges(
    workspace: Path,
    location: LocalizationLocation,
    lines: list[str],
) -> tuple[tuple[int, int], ...]:
    storage_path = database_path(workspace)
    if not storage_path.is_file():
        return ()
    try:
        storage_path.resolve().relative_to(workspace.resolve())
    except (OSError, RuntimeError, ValueError):
        return ()
    try:
        connection = sqlite3.connect(f"file:{storage_path}?mode=ro", uri=True)
        rows = connection.execute(
            """
            SELECT symbols.name, symbols.qualified_name,
                   symbols.start_line, symbols.end_line
            FROM symbols
            JOIN files ON files.id = symbols.file_id
            WHERE files.path = ?
            ORDER BY symbols.start_line, symbols.end_line, symbols.id
            """,
            (location.file,),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        if "connection" in locals():
            connection.close()

    target = location.function_name or location.class_name
    assert target is not None
    ranges: list[tuple[int, int]] = []
    for name, qualified_name, start_line, end_line in rows:
        names = {str(name)}
        if qualified_name is not None:
            names.add(str(qualified_name))
        if not any(_symbol_name_matches(value, target) for value in names):
            continue
        if location.function_name and location.class_name:
            qualified = str(qualified_name or "")
            if _has_qualification(qualified):
                if not re.search(
                    rf"(?:^|[.:/$#]){re.escape(location.class_name)}"
                    rf"(?:[.:/$#].*)?{re.escape(location.function_name)}$",
                    qualified,
                ):
                    continue
            elif not _line_in_unique_named_block(
                lines,
                int(start_line) if isinstance(start_line, int) else 0,
                location.class_name,
            ):
                continue
        if (
            isinstance(start_line, int)
            and isinstance(end_line, int)
            and start_line >= 1
            and end_line >= start_line
        ):
            value = (start_line, end_line)
            if value not in ranges:
                ranges.append(value)
    return tuple(ranges)


def _symbol_name_matches(value: str, target: str) -> bool:
    return value == target or bool(
        re.search(rf"(?:^|[.:/$#]){re.escape(target)}$", value)
    )


def _has_qualification(value: str) -> bool:
    return any(separator in value for separator in (".", ":", "/", "$", "#"))


def _textual_ranges(
    lines: list[str],
    location: LocalizationLocation,
) -> tuple[tuple[int, int], ...]:
    target = location.function_name or location.class_name
    assert target is not None
    escaped = re.escape(target)
    if location.function_name is not None:
        patterns = (
            re.compile(rf"^\s*(?:async\s+)?def\s+{escaped}\b"),
            re.compile(rf"^\s*(?:[\w<>\[\],?*&]+\s+)+{escaped}\s*\("),
            re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\b"),
            re.compile(rf"^\s*(?:pub\s+)?fn\s+{escaped}\b"),
        )
    else:
        patterns = (
            re.compile(
                rf"^\s*(?:(?:export|public|private|protected|internal)\s+)*"
                rf"(?:class|module|interface|trait|struct|enum)\s+{escaped}\b"
            ),
        )

    matches = [
        index
        for index, line in enumerate(lines, start=1)
        if any(pattern.search(line) for pattern in patterns)
    ]
    if location.function_name and location.class_name:
        matches = [
            line
            for line in matches
            if _line_in_unique_named_block(lines, line, location.class_name)
        ]
    return tuple((line, line) for line in matches)


def _line_in_unique_named_block(
    lines: list[str],
    line_number: int,
    name: str,
) -> bool:
    class_pattern = re.compile(
        rf"^\s*(?:(?:export|public|private|protected|internal)\s+)*"
        rf"(?:class|module|interface|trait|struct)\s+{re.escape(name)}\b"
    )
    class_lines = [
        index for index, line in enumerate(lines, start=1) if class_pattern.search(line)
    ]
    if len(class_lines) != 1:
        return False
    start = class_lines[0]
    return start <= line_number <= _textual_block_end(lines, start)


def _textual_block_end(lines: list[str], start_line: int) -> int:
    declaration = lines[start_line - 1]
    indentation = len(declaration) - len(declaration.lstrip())
    brace_depth = declaration.count("{") - declaration.count("}")
    if brace_depth > 0:
        for line_number in range(start_line + 1, len(lines) + 1):
            brace_depth += lines[line_number - 1].count("{")
            brace_depth -= lines[line_number - 1].count("}")
            if brace_depth <= 0:
                return line_number
        return len(lines)
    for line_number in range(start_line + 1, len(lines) + 1):
        line = lines[line_number - 1]
        if line.strip() and len(line) - len(line.lstrip()) <= indentation:
            return line_number - 1
    return len(lines)


def _unresolved(
    location: LocalizationLocation,
    code: str,
    message: str,
) -> _ResolvedPrediction:
    return _ResolvedPrediction(location, None, None, EngineWarning(code, message))


def _prediction_label(location: LocalizationLocation) -> str:
    symbol = ".".join(
        value
        for value in (location.class_name, location.function_name)
        if value is not None
    )
    return f"{location.file}:{symbol}" if symbol else location.file


def _render_localization(predictions: list[_ResolvedPrediction]) -> str:
    files = [
        prediction
        for prediction in predictions
        if prediction.citation is not None
        and prediction.location.class_name is None
        and prediction.location.function_name is None
    ]
    symbols = [
        prediction
        for prediction in predictions
        if prediction.citation is not None
        and (
            prediction.location.class_name is not None
            or prediction.location.function_name is not None
        )
    ]
    unresolved = [
        prediction for prediction in predictions if prediction.citation is None
    ]
    sections = ["OpenHands CodeScout localization:"]
    if files:
        sections.append("Files:")
        sections.extend(f"- {item.location.file}" for item in files)
    if symbols:
        sections.append("Functions/modules:")
        sections.extend(
            f"- {_prediction_label(item.location)} "
            f"(lines {item.citation.start_line}-{item.citation.end_line})"
            for item in symbols
            if item.citation is not None
        )
    if unresolved:
        sections.append("Unresolved predictions:")
        sections.extend(f"- {_prediction_label(item.location)}" for item in unresolved)
    return "\n".join(sections)


def _trajectory_path(workspace: Path, started_at: datetime) -> Path:
    directory = workspace_state_path(workspace, CODESCOUT_TRAJECTORY_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%S.%fZ")
    return directory / f"{stamp}-{uuid4().hex}.jsonl"


def _append_jsonl(path: Path, event: Mapping[str, object]) -> None:
    with path.open("a", encoding="utf-8") as trajectory:
        trajectory.write(json.dumps(event, sort_keys=True, separators=(",", ":")))
        trajectory.write("\n")


def _redact(value: object, *, key: str = "") -> object:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_authorization", "_password", "_secret", "_token")
    ):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            item_key: _redact(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


__all__ = [
    "CODESCOUT_MODEL_IDENTIFIERS",
    "CODESCOUT_SYSTEM_PROMPT",
    "CodeScoutEngine",
    "CodeScoutEngineError",
    "CodeScoutModelClient",
    "LocalizationFinish",
    "LocalizationLocation",
]
