"""Optional Microsoft FastContext engine adapter.

Tested against microsoft/fastcontext revision
788342a5c1f5ffbd9280fdbc813cc6bdb0024db4.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Callable, Coroutine, Mapping, MutableMapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from code_scout.core.errors import CodeScoutError
from code_scout.engines.availability import (
    ProviderAvailability,
    check_provider_availability,
)
from code_scout.engines.citations import bind_citations_to_index
from code_scout.engines.contracts import (
    Citation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    OpenAIModelTarget,
    TimingMetadata,
    resolve_timeout_seconds,
)
from code_scout.workspace.paths import (
    FASTCONTEXT_TRAJECTORY_DIRECTORY,
    workspace_state_path,
)

FASTCONTEXT_TESTED_REVISION = "788342a5c1f5ffbd9280fdbc813cc6bdb0024db4"
_FACTORY_ENVIRONMENT = ("MODEL", "API_KEY", "BASE_URL")
_GENERATION_PARAMETERS = ("temperature", "top_p")
_UNAUTHENTICATED_API_KEY = "not-required"
_factory_environment_lock = Lock()
_PATH_TOOLS = frozenset({"read", "glob", "grep"})
_SEARCH_TOOLS = frozenset({"glob", "grep"})
_FINAL_ANSWER_WRAPPER = re.compile(
    r"<\s*/?\s*final_answer\b[^>\n]*>?",
    flags=re.IGNORECASE,
)
_CITATION_LINE = re.compile(
    r"^[^\r\n]*?(?P<path>(?:[A-Za-z]:)?[^\s<>()\[\]{},;]+?)"
    r"(?:"
    r":[ \t]*(?P<colon_start>\d+)"
    r"(?:[ \t]*-[ \t]*(?P<colon_end>\d+))?"
    r"|[ \t]*\([ \t]*lines?[ \t]+(?P<label_start>\d+)"
    r"(?:[ \t]*-[ \t]*(?P<label_end>\d+))?[ \t]*\)"
    r")",
    flags=re.IGNORECASE | re.MULTILINE,
)


class FastContextError(CodeScoutError):
    """FastContext could not complete a repository exploration request."""


def parse_fastcontext_answer(answer: str) -> tuple[str, tuple[Citation, ...]]:
    """Separate FastContext's final-answer text from its untrusted citations."""

    answer_text = _FINAL_ANSWER_WRAPPER.sub("", answer).strip()
    citations = tuple(
        Citation(
            match.group("path").strip("`*"),
            int(match.group("colon_start") or match.group("label_start")),
            int(
                match.group("colon_end")
                or match.group("label_end")
                or match.group("colon_start")
                or match.group("label_start")
            ),
        )
        for match in _CITATION_LINE.finditer(answer_text)
    )
    return answer_text, citations


class FastContextAgent(Protocol):
    async def run(
        self,
        *,
        prompt: str,
        max_turns: int,
        citation: bool,
    ) -> str: ...


FastContextFactory = Callable[..., FastContextAgent]
AsyncRunner = Callable[[Coroutine[Any, Any, str], float], str]


class FastContextPathResolver:
    """Resolve untrusted FastContext tool paths beneath one workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._events: list[dict[str, str]] = []
        self._lock = Lock()

    def resolve(self, requested_path: object, *, tool: str) -> Path | None:
        if not isinstance(requested_path, str) or not requested_path:
            self._reject(tool, str(requested_path), "invalid_path")
            return None

        requested = Path(requested_path)
        if requested.is_absolute():
            if requested.exists():
                return self._confine_existing(
                    requested,
                    tool=tool,
                    requested_path=requested_path,
                    corrected=False,
                    correction_reason="",
                )
            if _lexically_within(requested, self.workspace):
                self._reject(tool, requested_path, "not_found")
                return None
            relative = Path(*requested.parts[1:])
        else:
            relative = requested

        candidates: list[tuple[Path, str]] = [
            (self.workspace / relative, "workspace_relative")
        ]
        if relative.parts and relative.parts[0] == self.workspace.name:
            candidates.append(
                (
                    self.workspace / Path(*relative.parts[1:]),
                    "removed_repository_prefix",
                )
            )

        unique: dict[str, tuple[Path, str]] = {}
        for candidate, reason in candidates:
            unique.setdefault(os.path.abspath(candidate), (candidate, reason))
        existing = [candidate for candidate in unique.values() if candidate[0].exists()]
        if not existing:
            self._reject(tool, requested_path, "not_found")
            return None
        if len(existing) != 1:
            self._reject(tool, requested_path, "ambiguous")
            return None

        candidate, reason = existing[0]
        corrected = requested.is_absolute() or reason == "removed_repository_prefix"
        return self._confine_existing(
            candidate,
            tool=tool,
            requested_path=requested_path,
            corrected=corrected,
            correction_reason=reason,
        )

    def metadata(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            return tuple(dict(event) for event in self._events)

    def _confine_existing(
        self,
        candidate: Path,
        *,
        tool: str,
        requested_path: str,
        corrected: bool,
        correction_reason: str,
    ) -> Path | None:
        if not _lexically_within(candidate, self.workspace):
            self._reject(tool, requested_path, "outside_workspace")
            return None
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.workspace):
            self._reject(tool, requested_path, "symlink_escape")
            return None
        if corrected:
            self._record(
                status="corrected",
                tool=tool,
                requested_path=requested_path,
                resolved_path=str(resolved),
                reason=correction_reason,
            )
        return resolved

    def _reject(self, tool: str, requested_path: str, reason: str) -> None:
        self._record(
            status="rejected",
            tool=tool,
            requested_path=requested_path,
            reason=reason,
        )

    def _record(self, **event: str) -> None:
        with self._lock:
            self._events.append(event)


class _GuardedFastContextTool:
    def __init__(self, tool: object, resolver: FastContextPathResolver) -> None:
        self._tool = tool
        self._resolver = resolver
        self.name = str(tool.name)  # type: ignore[attr-defined]
        self.description = getattr(tool, "description", "")
        self.parameters = getattr(tool, "parameters", {})

    async def call(self, parameters: str, **kwargs: object) -> str:
        try:
            parsed = json.loads(parameters)
        except (TypeError, ValueError):
            return await self._tool.call(parameters, **kwargs)  # type: ignore[attr-defined]
        if not isinstance(parsed, dict):
            return await self._tool.call(parameters, **kwargs)  # type: ignore[attr-defined]

        tool_name = self.name.casefold()
        if "path" not in parsed and tool_name in _SEARCH_TOOLS:
            parsed["path"] = str(self._resolver.workspace)
        if "path" in parsed:
            resolved = self._resolver.resolve(parsed["path"], tool=self.name)
            if resolved is None:
                return (
                    f"Code Scout rejected FastContext {self.name} path: "
                    f"{parsed['path']!r}."
                )
            parsed["path"] = str(resolved)
        return await self._tool.call(json.dumps(parsed), **kwargs)  # type: ignore[attr-defined]

    def schema(self) -> object:
        return self._tool.schema()  # type: ignore[attr-defined]


def guard_fastcontext_tools(
    agent: object,
    resolver: FastContextPathResolver,
    *,
    required: bool,
) -> None:
    """Install path guards at FastContext's ToolSet dispatch boundary."""

    candidates: list[MutableMapping[object, object]] = []
    for owner in (agent, *vars(agent).values()):
        for value in vars(owner).values() if hasattr(owner, "__dict__") else ():
            if not isinstance(value, MutableMapping):
                continue
            names = {
                str(getattr(tool, "name", key)).casefold()
                for key, tool in value.items()
            }
            if names.intersection(_PATH_TOOLS):
                candidates.append(value)

    distinct = {id(candidate): candidate for candidate in candidates}
    if len(distinct) != 1:
        if required:
            raise FastContextError(
                "FastContext tool confinement could not be installed.",
                "Install the exact tested FastContext revision before selecting this "
                "engine.",
            )
        return

    tools = next(iter(distinct.values()))
    guarded_names: set[str] = set()
    for key, tool in tuple(tools.items()):
        name = str(getattr(tool, "name", key)).casefold()
        if name in _PATH_TOOLS:
            if name == "grep" and hasattr(tool, "_rg_path"):
                tool._rg_path = shutil.which("rg") or "rg"  # type: ignore[attr-defined]
            tools[key] = _GuardedFastContextTool(tool, resolver)
            guarded_names.add(name)
    if required and guarded_names != _PATH_TOOLS:
        raise FastContextError(
            "FastContext tool confinement could not be installed.",
            "The provider's read-only tool set does not match the tested revision.",
        )


def _lexically_within(path: Path, workspace: Path) -> bool:
    normalized = Path(os.path.abspath(path))
    return normalized.is_relative_to(workspace)


class FastContextEngine:
    """Run the optional FastContext provider behind the shared engine protocol."""

    metadata = EngineMetadata(
        name="fastcontext",
        provider="microsoft/fastcontext",
        provider_version=FASTCONTEXT_TESTED_REVISION,
        requires_evidence=True,
    )

    def __init__(
        self,
        target: OpenAIModelTarget,
        *,
        api_key: str | None = None,
        factory: FastContextFactory | None = None,
        async_runner: AsyncRunner | None = None,
        environment: Mapping[str, str] | None = None,
        max_turns: int | None = None,
    ) -> None:
        if max_turns is not None and not 1 <= max_turns <= 100:
            raise ValueError("max_turns must be between 1 and 100.")
        self.target = target
        self._api_key = api_key
        self._factory = factory
        self._async_runner = _run_with_timeout if async_runner is None else async_runner
        self._environment = environment
        self.max_turns = max_turns

    def availability(self, *, probe_endpoint: bool = False) -> ProviderAvailability:
        return check_provider_availability(
            target=self.target,
            dependency="fastcontext",
            probe_endpoint=probe_endpoint,
            environment=self._environment,
        )

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        started_at = datetime.now(UTC)
        started = monotonic()
        trajectory = _trajectory_path(request.workspace, started_at)
        timeout = resolve_timeout_seconds(
            request.runtime.timeout_seconds, self.target.timeout_seconds
        )
        max_tokens = min(request.runtime.max_tokens, self.target.max_tokens)
        path_resolver = FastContextPathResolver(request.workspace)

        try:
            api_key = self._resolve_api_key()
            factory = self._load_factory() if self._factory is None else self._factory
            with _factory_environment(
                model=self.target.model,
                api_key=api_key or _UNAUTHENTICATED_API_KEY,
                base_url=self.target.base_url,
            ):
                agent = factory(
                    trajectory_file=str(trajectory),
                    work_dir=str(request.workspace),
                )
            guard_fastcontext_tools(
                agent,
                path_resolver,
                required=self._factory is None,
            )
            _configure_agent_model(
                agent,
                max_tokens=max_tokens,
                parameters=self.target.parameters,
            )
            answer = self._async_runner(
                agent.run(
                    prompt=request.query,
                    max_turns=self._max_turns(request),
                    citation=True,
                ),
                timeout,
            )
        except FastContextError:
            raise
        except TimeoutError as error:
            raise FastContextError(
                f"FastContext exceeded its {timeout:g}-second timeout.",
                "Reduce the query scope or increase the configured timeout.",
            ) from error
        except Exception as error:
            raise FastContextError(
                "FastContext repository exploration failed.",
                "Check the provider installation, model endpoint, and trajectory at "
                f"{trajectory}.",
            ) from error

        if not isinstance(answer, str) or not answer.strip():
            raise FastContextError(
                "FastContext returned no answer.",
                f"Inspect the provider trajectory at {trajectory}.",
            )
        if answer.lstrip().startswith("LLM API call failed."):
            raise FastContextError(
                "FastContext model execution failed.",
                "Check the configured endpoint, model, credentials, and provider "
                f"trajectory at {trajectory}.",
            )
        answer_text, citations = parse_fastcontext_answer(answer)
        if not answer_text:
            raise FastContextError(
                "FastContext returned no answer.",
                f"Inspect the provider trajectory at {trajectory}.",
            )
        return CodeScoutResult(
            answer=answer_text,
            citations=bind_citations_to_index(request.workspace, citations),
            warnings=(),
            engine=self.metadata,
            timing=TimingMetadata(
                duration_seconds=monotonic() - started,
                started_at=started_at,
            ),
            metadata={
                "trajectory_path": str(trajectory),
                "path_events": path_resolver.metadata(),
            },
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
            name="fastcontext.model",
            environment=self._environment,
        ).api_key

    @staticmethod
    def _load_factory() -> FastContextFactory:
        try:
            from fastcontext.agent.agent_factory import make_fastcontext_agent
        except (ImportError, ModuleNotFoundError) as error:
            raise FastContextError(
                "The optional FastContext provider is not installed.",
                "Install microsoft/fastcontext at revision "
                f"{FASTCONTEXT_TESTED_REVISION} before selecting this engine.",
            ) from error
        return make_fastcontext_agent


def _trajectory_path(workspace: Path, started_at: datetime) -> Path:
    directory = workspace_state_path(workspace, FASTCONTEXT_TRAJECTORY_DIRECTORY)
    filename = (
        f"trajectory-{started_at.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}.jsonl"
    )
    return directory / filename


def _configure_agent_model(
    agent: FastContextAgent,
    *,
    max_tokens: int,
    parameters: Mapping[str, object],
) -> None:
    llm = getattr(agent, "llm", None)
    if llm is None:
        return
    llm.max_tokens = max_tokens
    for name in _GENERATION_PARAMETERS:
        if name in parameters:
            setattr(llm, name, parameters[name])


@contextmanager
def _factory_environment(
    *,
    model: str,
    api_key: str | None,
    base_url: str,
):
    values = {"MODEL": model, "API_KEY": api_key, "BASE_URL": base_url}
    with _factory_environment_lock:
        previous = {name: os.environ.get(name) for name in _FACTORY_ENVIRONMENT}
        try:
            for name, value in values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _run_with_timeout(coroutine: Coroutine[Any, Any, str], timeout: float) -> str:
    async def bounded() -> str:
        return await asyncio.wait_for(coroutine, timeout=timeout)

    return asyncio.run(bounded())


__all__ = [
    "FASTCONTEXT_TESTED_REVISION",
    "FastContextEngine",
    "FastContextError",
    "FastContextPathResolver",
    "guard_fastcontext_tools",
    "parse_fastcontext_answer",
]
