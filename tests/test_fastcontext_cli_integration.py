from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_scout.commands.cli import RootFallbackApp
from code_scout.engines import (
    CodeScoutRequest,
    EngineRegistry,
    FastContextEngine,
    OpenAIModelTarget,
    RuntimeOptions,
    create_engine_registry,
)


class QueryAgent:
    def __init__(self, responses: dict[str, str], calls: list[str]) -> None:
        self.responses = responses
        self.calls = calls
        self.llm = SimpleNamespace()

    async def run(
        self,
        *,
        prompt: str,
        max_turns: int,
        citation: bool,
    ) -> str:
        assert max_turns == 4
        assert citation is True
        self.calls.append(prompt)
        return self.responses[prompt]


def _target(**overrides: object) -> OpenAIModelTarget:
    values: dict[str, object] = {
        "base_url": "https://models.example.test/v1",
        "model": "fake-fastcontext",
        "timeout_seconds": 10,
        "max_tokens": 1000,
    }
    values.update(overrides)
    return OpenAIModelTarget(**values)  # type: ignore[arg-type]


def _invoke(app: RootFallbackApp, args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = app(args)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _write_lines(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(f"{path.stem} line {line}" for line in range(1, count + 1)),
        encoding="utf-8",
    )


def test_fake_fastcontext_cli_lookups_preserve_explicit_boundaries(
    tmp_path: Path,
) -> None:
    responses = {
        "Find the router implementation": (
            "<final_answer>\nsrc/router.py:2-4 (router implementation)\n</final_answer>"
        ),
        "Find the authentication middleware": (
            "<final_answer>\nsrc/middleware.py:3-5 (authentication middleware)\n"
            "</final_answer>"
        ),
        "Find every caller of route_request": (
            "<final_answer>\nsrc/server.py:6-7 (production caller)\n"
            "tests/test_server.py:8-10 (test caller)\n</final_answer>"
        ),
    }
    _write_lines(tmp_path / "src" / "router.py", 6)
    _write_lines(tmp_path / "src" / "middleware.py", 6)
    _write_lines(tmp_path / "src" / "server.py", 10)
    _write_lines(tmp_path / "tests" / "test_server.py", 12)
    calls: list[str] = []
    engine = FastContextEngine(
        _target(),
        api_key="fake-key",
        factory=lambda **_: QueryAgent(responses, calls),
    )
    registry = EngineRegistry()
    registry.register(engine)
    app = RootFallbackApp(registry)

    assert registry.get("fastcontext") is engine
    for query in responses:
        explicit = _invoke(
            app,
            [
                "--query",
                query,
                "--engine",
                "fastcontext",
                "--workspace",
                str(tmp_path),
            ],
        )

        assert explicit[0] == 0
        assert explicit[2] == ""

    assert calls == list(responses)


_INTEGRATION_BASE_URL = os.environ.get("FASTCONTEXT_INTEGRATION_BASE_URL")
_INTEGRATION_MODEL = os.environ.get("FASTCONTEXT_INTEGRATION_MODEL")


@pytest.mark.skipif(
    not (_INTEGRATION_BASE_URL and _INTEGRATION_MODEL),
    reason=(
        "set FASTCONTEXT_INTEGRATION_BASE_URL and FASTCONTEXT_INTEGRATION_MODEL "
        "to run the real-endpoint smoke test"
    ),
)
def test_real_fastcontext_endpoint_returns_valid_workspace_evidence() -> None:
    workspace = Path(__file__).parents[1]
    engine = FastContextEngine(
        _target(
            base_url=_INTEGRATION_BASE_URL,
            model=_INTEGRATION_MODEL,
            timeout_seconds=120,
            max_tokens=4096,
        ),
        api_key=os.environ.get("FASTCONTEXT_INTEGRATION_API_KEY"),
    )
    registry = create_engine_registry(engines=(engine,))

    result = registry.execute(
        CodeScoutRequest(
            query=(
                "Locate the Python implementation of parse_fastcontext_answer and "
                "return its exact file and line range."
            ),
            workspace=workspace,
            engine="fastcontext",
            runtime=RuntimeOptions(timeout_seconds=120, max_turns=6, max_tokens=4096),
        )
    )

    assert result.verified
    assert result.engine.name == "fastcontext"
    assert all((workspace / citation.path).is_file() for citation in result.citations)
