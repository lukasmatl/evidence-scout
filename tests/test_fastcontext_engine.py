from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from code_scout.engines import (
    FASTCONTEXT_TESTED_REVISION,
    Citation,
    CodeScoutRequest,
    EngineRegistry,
    EvidenceRequiredError,
    FastContextEngine,
    FastContextError,
    OpenAIModelTarget,
    RuntimeOptions,
    parse_fastcontext_answer,
)
from code_scout.workspace.paths import FASTCONTEXT_TRAJECTORY_DIRECTORY

TRAJECTORY_FIXTURES = Path(__file__).parent / "fixtures" / "engine_trajectories"


class FakeAgent:
    def __init__(self, answer: str = "<final_answer>src/router.py:1-2</final_answer>"):
        self.answer = answer
        self.llm = SimpleNamespace()
        self.calls: list[dict[str, object]] = []

    async def run(
        self,
        *,
        prompt: str,
        max_turns: int,
        citation: bool,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "max_turns": max_turns, "citation": citation}
        )
        return self.answer


def _target(**overrides: Any) -> OpenAIModelTarget:
    values: dict[str, Any] = {
        "base_url": "https://models.example.test/v1",
        "model": "fastcontext-model",
        "api_key_env": "FASTCONTEXT_KEY",
        "timeout_seconds": 18,
        "max_tokens": 2000,
        "parameters": {"temperature": 0.2, "top_p": 0.85, "seed": 7},
    }
    values.update(overrides)
    return OpenAIModelTarget(**values)


def test_adapter_translates_model_workspace_query_bounds_and_trajectory(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = FakeAgent()
    factory_calls: list[dict[str, object]] = []
    runner_timeouts: list[float] = []

    def factory(**kwargs: object) -> FakeAgent:
        factory_calls.append(
            {
                **kwargs,
                "model": os.environ.get("MODEL"),
                "api_key": os.environ.get("API_KEY"),
                "base_url": os.environ.get("BASE_URL"),
            }
        )
        return agent

    def run(coroutine: object, timeout: float) -> str:
        runner_timeouts.append(timeout)
        return asyncio.run(coroutine)  # type: ignore[arg-type]

    engine = FastContextEngine(
        _target(),
        factory=factory,
        async_runner=run,
        environment={"FASTCONTEXT_KEY": "provider-secret"},
    )
    request = CodeScoutRequest(
        query="  Find the router exactly as written  ",
        workspace=workspace,
        engine="fastcontext",
        runtime=RuntimeOptions(timeout_seconds=9, max_turns=3, max_tokens=1500),
    )

    result = engine.execute(request)

    assert len(factory_calls) == 1
    call = factory_calls[0]
    assert call["work_dir"] == str(workspace.resolve())
    trajectory = Path(str(call["trajectory_file"]))
    expected_directory = workspace.resolve() / FASTCONTEXT_TRAJECTORY_DIRECTORY
    assert trajectory.parent == expected_directory
    assert trajectory.suffix == ".jsonl"
    assert result.metadata["trajectory_path"] == str(trajectory)
    assert agent.calls == [
        {
            "prompt": "  Find the router exactly as written  ",
            "max_turns": 3,
            "citation": True,
        }
    ]
    assert runner_timeouts == [9]
    assert agent.llm.max_tokens == 1500
    assert agent.llm.temperature == 0.2
    assert agent.llm.top_p == 0.85
    assert not hasattr(agent.llm, "seed")
    assert call["model"] == "fastcontext-model"
    assert call["api_key"] == "provider-secret"
    assert call["base_url"] == "https://models.example.test/v1"
    assert os.environ.get("API_KEY") != "provider-secret"
    assert result.answer == "src/router.py:1-2"
    assert result.citations == (Citation("src/router.py", 1, 2),)
    assert result.engine.provider_version == FASTCONTEXT_TESTED_REVISION


def test_adapter_uses_stricter_global_model_limits(tmp_path: Path) -> None:
    agent = FakeAgent()
    timeouts: list[float] = []

    def run(coroutine: object, timeout: float) -> str:
        timeouts.append(timeout)
        return asyncio.run(coroutine)  # type: ignore[arg-type]

    engine = FastContextEngine(
        _target(timeout_seconds=4, max_tokens=600),
        api_key="secret",
        factory=lambda **_: agent,
        async_runner=run,
    )

    engine.execute(
        CodeScoutRequest(
            query="Find it",
            workspace=tmp_path,
            engine="fastcontext",
            runtime=RuntimeOptions(
                timeout_seconds=30,
                max_turns=4,
                max_tokens=1000,
            ),
        )
    )

    assert timeouts == [4]
    assert agent.llm.max_tokens == 600


def test_adapter_uses_configured_provider_turn_budget(tmp_path: Path) -> None:
    agent = FakeAgent()
    engine = FastContextEngine(
        _target(),
        api_key="secret",
        factory=lambda **_: agent,
        max_turns=8,
    )

    engine.execute(
        CodeScoutRequest(
            query="Find it",
            workspace=tmp_path,
            engine="fastcontext",
            runtime=RuntimeOptions(max_turns=4),
        )
    )

    assert agent.calls[0]["max_turns"] == 8


def test_request_turn_budget_override_beats_configured_budget(tmp_path: Path) -> None:
    agent = FakeAgent()
    engine = FastContextEngine(
        _target(),
        api_key="secret",
        factory=lambda **_: agent,
        max_turns=8,
    )

    engine.execute(
        CodeScoutRequest(
            query="Find it",
            workspace=tmp_path,
            engine="fastcontext",
            runtime=RuntimeOptions(max_turns_override=12),
        )
    )

    assert agent.calls[0]["max_turns"] == 12


def test_adapter_supports_unauthenticated_local_endpoint(tmp_path: Path) -> None:
    captured_api_keys: list[str | None] = []

    def factory(**_: object) -> FakeAgent:
        captured_api_keys.append(os.environ.get("API_KEY"))
        return FakeAgent()

    engine = FastContextEngine(
        _target(
            base_url="http://localhost:8000/v1",
            api_key_env=None,
        ),
        factory=factory,
    )

    engine.execute(
        CodeScoutRequest(
            query="Find it",
            workspace=tmp_path,
            engine="fastcontext",
        )
    )

    assert captured_api_keys == ["not-required"]


def test_adapter_translates_timeout_and_provider_errors(tmp_path: Path) -> None:
    def timeout(coroutine: object, _seconds: float) -> str:
        coroutine.close()  # type: ignore[attr-defined]
        raise TimeoutError

    timed_out = FastContextEngine(
        _target(),
        api_key="secret",
        factory=lambda **_: FakeAgent(),
        async_runner=timeout,
    )
    request = CodeScoutRequest(
        query="Find it",
        workspace=tmp_path,
        engine="fastcontext",
        runtime=RuntimeOptions(timeout_seconds=7),
    )

    with pytest.raises(FastContextError, match="7-second timeout") as caught:
        timed_out.execute(request)
    assert "increase" in (caught.value.hint or "")

    def broken_factory(**_: object) -> FakeAgent:
        raise RuntimeError("secret provider detail")

    broken = FastContextEngine(
        _target(),
        api_key="secret",
        factory=broken_factory,
    )
    with pytest.raises(FastContextError, match="exploration failed") as caught:
        broken.execute(request)
    assert "secret provider detail" not in caught.value.message
    assert FASTCONTEXT_TRAJECTORY_DIRECTORY in (caught.value.hint or "")

    failed_model = FastContextEngine(
        _target(),
        api_key="secret",
        factory=lambda **_: FakeAgent(
            "LLM API call failed. So stopping the agent.\nError details:\nprivate"
        ),
    )
    with pytest.raises(FastContextError, match="model execution failed") as caught:
        failed_model.execute(request)
    assert "private" not in caught.value.message


def test_real_async_runner_enforces_timeout(tmp_path: Path) -> None:
    class SlowAgent(FakeAgent):
        async def run(
            self,
            *,
            prompt: str,
            max_turns: int,
            citation: bool,
        ) -> str:
            await asyncio.sleep(0.05)
            return await super().run(
                prompt=prompt,
                max_turns=max_turns,
                citation=citation,
            )

    engine = FastContextEngine(
        _target(timeout_seconds=0.01),
        api_key="secret",
        factory=lambda **_: SlowAgent(),
    )

    with pytest.raises(FastContextError, match="0.01-second timeout"):
        engine.execute(
            CodeScoutRequest(
                query="Find it",
                workspace=tmp_path,
                engine="fastcontext",
            )
        )


@pytest.mark.parametrize(
    ("response", "answer", "citations"),
    [
        (
            "Before the block.\n<final_answer>\n"
            "src/router.py:4-8 (router implementation)\n"
            "tests/test_router.py:12 (coverage)\n</final_answer>",
            "Before the block.\n\nsrc/router.py:4-8 (router implementation)\n"
            "tests/test_router.py:12 (coverage)",
            (Citation("src/router.py", 4, 8), Citation("tests/test_router.py", 12)),
        ),
        (
            "<FINAL_ANSWER\nsrc/middleware.py:7-9 — authentication middleware",
            "src/middleware.py:7-9 — authentication middleware",
            (Citation("src/middleware.py", 7, 9),),
        ),
        (
            "Useful prose despite a closing-only wrapper.\n"
            "src/callers.py:21 </final_answer>",
            "Useful prose despite a closing-only wrapper.\nsrc/callers.py:21",
            (Citation("src/callers.py", 21),),
        ),
    ],
)
def test_parse_fastcontext_answer_retains_prose_and_tolerates_wrappers(
    response: str,
    answer: str,
    citations: tuple[Citation, ...],
) -> None:
    assert parse_fastcontext_answer(response) == (answer, citations)


def test_parse_captured_fastcontext_lines_without_false_directory_citation() -> None:
    event = json.loads(
        (TRAJECTORY_FIXTURES / "fastcontext-20260717T172329.jsonl").read_text(
            encoding="utf-8"
        )
    )

    _, citations = parse_fastcontext_answer(event["content"])

    assert citations == (
        Citation("src/code_scout/cli.py", 1, 455),
        Citation("src/code_scout/commands/specs.py", 1, 187),
        Citation("src/code_scout/commands/parser.py", 1, 167),
        Citation("src/code_scout/commands/dispatch.py", 1, 271),
    )
    assert Citation("src/code_scout/", 1) not in citations


def test_registry_validates_every_fastcontext_citation(tmp_path: Path) -> None:
    source = tmp_path / "src" / "router.py"
    source.parent.mkdir()
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")
    engine = FastContextEngine(
        _target(),
        api_key="secret",
        factory=lambda **_: FakeAgent(
            "<final_answer>\n"
            "src/router.py:1-2\n"
            "src/missing.py:1\n"
            "src/router.py:99\n"
            "</final_answer>"
        ),
    )
    registry = EngineRegistry()
    registry.register(engine)

    result = registry.execute(
        CodeScoutRequest("Find it", tmp_path, engine="fastcontext")
    )

    assert result.citations == (Citation("src/router.py", 1, 2),)
    assert [warning.code for warning in result.warnings] == [
        "citation-missing-file",
        "citation-range-out-of-bounds",
    ]


def test_registry_fails_fastcontext_without_valid_evidence(tmp_path: Path) -> None:
    engine = FastContextEngine(
        _target(),
        api_key="secret",
        factory=lambda **_: FakeAgent(
            "<final_answer>Invented result: src/missing.py:1</final_answer>"
        ),
    )
    registry = EngineRegistry()
    registry.register(engine)

    with pytest.raises(EvidenceRequiredError) as caught:
        registry.execute(CodeScoutRequest("Find it", tmp_path, engine="fastcontext"))

    assert caught.value.result.citations == ()
    assert [warning.code for warning in caught.value.result.warnings] == [
        "citation-missing-file"
    ]
