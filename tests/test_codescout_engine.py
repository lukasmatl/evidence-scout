from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from code_scout.engines import (
    CODESCOUT_SYSTEM_PROMPT,
    Citation,
    CodeScoutEngine,
    CodeScoutEngineError,
    CodeScoutRequest,
    EngineRegistry,
    EvidenceRequiredError,
    OpenAIChatChoice,
    OpenAIChatMessage,
    OpenAIChatRequest,
    OpenAIChatResponse,
    OpenAICompatibilityError,
    OpenAIModelTarget,
    OpenAIToolCall,
    RuntimeOptions,
)
from code_scout.storage import open_storage, store_source_revision

TRAJECTORY_FIXTURES = Path(__file__).parent / "fixtures" / "engine_trajectories"


def test_system_prompt_prevents_captured_terminal_policy_failure() -> None:
    events = [
        json.loads(line)
        for line in (TRAJECTORY_FIXTURES / "codescout-20260717T165112.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert sum("error" in event.get("observation", {}) for event in events) == 5
    assert "already starts in the repository workspace" in CODESCOUT_SYSTEM_PROMPT
    assert "MUST be repository-relative" in CODESCOUT_SYSTEM_PROMPT
    for forbidden in ("absolute paths", "`pwd`", "`cd`", "redirection", "`&&`"):
        assert forbidden in CODESCOUT_SYSTEM_PROMPT


def test_request_prompt_does_not_cue_absolute_workspace_paths(workspace: Path) -> None:
    client = ScriptedClient((_response(_finish({"file": "src/router.py"})),))
    engine = CodeScoutEngine(_target(api_key_env=None), model_client=client)

    engine.execute(CodeScoutRequest("Find router", workspace, engine="codescout"))

    prompt = client.requests[0].messages[1].content
    assert str(workspace) not in prompt
    assert "terminal already starts" in prompt
    assert "repository-relative paths" in prompt


def _target(**values: object) -> OpenAIModelTarget:
    defaults: dict[str, object] = {
        "base_url": "https://models.example.test/v1",
        "model": "OpenHands/CodeScout-4B",
        "api_key_env": "MODEL_KEY",
        "timeout_seconds": 30.0,
        "max_tokens": 2048,
        "parameters": {"temperature": 0.1, "top_p": 0.9},
    }
    defaults.update(values)
    return OpenAIModelTarget(**defaults)  # type: ignore[arg-type]


def _response(*calls: OpenAIToolCall, content: str | None = None) -> OpenAIChatResponse:
    return OpenAIChatResponse(
        id="scripted",
        model="OpenHands/CodeScout-4B",
        choices=(
            OpenAIChatChoice(
                0,
                OpenAIChatMessage("assistant", content, tuple(calls)),
                "tool_calls" if calls else "stop",
            ),
        ),
    )


def _terminal(call_id: str, command: str) -> OpenAIToolCall:
    return OpenAIToolCall(
        call_id,
        "terminal",
        json.dumps({"command": command, "security_risk": "LOW"}),
    )


def _finish(*locations: dict[str, object]) -> OpenAIToolCall:
    return OpenAIToolCall(
        "finish",
        "localization_finish",
        json.dumps({"locations": list(locations)}),
    )


def _add_symbols(
    workspace: Path,
    *symbols: tuple[str, str, int, int],
) -> None:
    with open_storage(workspace) as connection:
        connection.execute(
            "INSERT INTO workspaces (id, name, root_path) VALUES ('ws', 'ws', ?)",
            (str(workspace),),
        )
        connection.execute(
            """
            INSERT INTO repositories (id, workspace_id, name, path)
            VALUES ('repo', 'ws', 'repo', '.')
            """
        )
        paths = sorted({path for path, _, _, _ in symbols})
        for index, path in enumerate(paths):
            connection.execute(
                """
                INSERT INTO files (id, repository_id, path)
                VALUES (?, 'repo', ?)
                """,
                (f"file-{index}", path),
            )
        file_ids = {
            row[1]: row[0]
            for row in connection.execute("SELECT id, path FROM files").fetchall()
        }
        for index, (path, name, start_line, end_line) in enumerate(symbols):
            connection.execute(
                """
                INSERT INTO symbols (
                    id, file_id, name, kind, qualified_name, start_line, end_line
                ) VALUES (?, ?, ?, 'function', ?, ?, ?)
                """,
                (
                    f"symbol-{index}",
                    file_ids[path],
                    name,
                    name,
                    start_line,
                    end_line,
                ),
            )


class ScriptedClient:
    def __init__(self, responses: Iterable[OpenAIChatResponse | Exception]) -> None:
        self.responses = iter(responses)
        self.requests: list[OpenAIChatRequest] = []
        self.targets: list[OpenAIModelTarget] = []
        self.api_keys: list[str | None] = []

    def __call__(
        self,
        target: OpenAIModelTarget,
        request: OpenAIChatRequest,
        *,
        api_key: str | None,
    ) -> OpenAIChatResponse:
        self.targets.append(target)
        self.requests.append(request)
        self.api_keys.append(api_key)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    source = tmp_path / "src" / "router.py"
    source.parent.mkdir()
    source.write_text("class Router:\n    def dispatch(self):\n        pass\n")
    return tmp_path


def test_parallel_terminal_searches_then_structured_finish(
    workspace: Path,
) -> None:
    client = ScriptedClient(
        (
            _response(
                _terminal("search", "rg -n Router src"),
                _terminal("files", "find src -name '*.py' -print"),
            ),
            _response(
                _finish(
                    {
                        "file": "src/router.py",
                        "class_name": "Router",
                        "function_name": "dispatch",
                    }
                )
            ),
        )
    )
    engine = CodeScoutEngine(_target(), api_key="super-secret", model_client=client)

    result = engine.execute(
        CodeScoutRequest(
            "Update router dispatch",
            workspace,
            engine="codescout",
            runtime=RuntimeOptions(timeout_seconds=2, max_turns=4, max_tokens=512),
        )
    )

    assert result.engine.model == "OpenHands/CodeScout-4B"
    assert result.engine.provider == "OpenHands CodeScout"
    assert result.engine.requires_evidence is True
    assert result.citations == (Citation("src/router.py", 2),)
    assert "Functions/modules:" in result.answer
    assert result.metadata["localization_finish"] == {
        "locations": [
            {
                "file": "src/router.py",
                "class_name": "Router",
                "function_name": "dispatch",
            }
        ]
    }
    assert len(client.requests) == 2
    assert client.targets[0].timeout_seconds <= 2
    assert client.targets[0].max_tokens == 512
    assert client.api_keys == ["super-secret", "super-secret"]
    assert {tool.name for tool in client.requests[0].tools} == {
        "terminal",
        "localization_finish",
    }
    tool_messages = [
        message for message in client.requests[1].messages if message.role == "tool"
    ]
    assert [message.tool_call_id for message in tool_messages] == ["search", "files"]
    assert all("exit_code" in (message.content or "") for message in tool_messages)

    trajectory_path = Path(str(result.metadata["trajectory_path"]))
    assert trajectory_path.parent == (
        workspace / ".code-scout" / "trajectories" / "codescout"
    )
    trajectory_text = trajectory_path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in trajectory_text.splitlines()]
    assert "super-secret" not in trajectory_text
    assert [event["event"] for event in events].count("tool_observation") == 2
    assert events[-1]["event"] == "localization_finish"
    assert events[-1]["locations"][0]["function_name"] == "dispatch"


def test_terminal_cat_observation_binds_its_matching_indexed_revision(
    workspace: Path,
) -> None:
    source = workspace / "src" / "router.py"
    with open_storage(workspace) as connection:
        revision = store_source_revision(
            connection,
            repository_id="repo:workspace",
            path="src/router.py",
            source_bytes=source.read_bytes(),
        )
    client = ScriptedClient(
        (
            _response(_terminal("read", "cat src/router.py")),
            _response(_finish({"file": "src/router.py"})),
        )
    )

    result = CodeScoutEngine(_target(api_key_env=None), model_client=client).execute(
        CodeScoutRequest("Find router", workspace, engine="codescout")
    )

    assert result.citations[0].source_revision_id == revision.id


def test_malformed_tool_call_is_observed_and_model_can_recover(
    workspace: Path,
) -> None:
    client = ScriptedClient(
        (
            _response(OpenAIToolCall("bad", "terminal", "not-json")),
            _response(_finish({"file": "src/router.py"})),
        )
    )
    result = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=client,
    ).execute(CodeScoutRequest("Find router", workspace, engine="codescout"))

    observation = next(
        message for message in client.requests[1].messages if message.role == "tool"
    )
    assert "error" in (observation.content or "")
    assert result.metadata["localization_finish"] == {
        "locations": [
            {
                "file": "src/router.py",
                "class_name": None,
                "function_name": None,
            }
        ]
    }


def test_checkpoint_terminal_metadata_does_not_block_safe_command(
    workspace: Path,
) -> None:
    traced_call = OpenAIToolCall(
        "search",
        "terminal",
        json.dumps(
            {
                "command": "ls src",
                "is_input": True,
                "reset": False,
                "security_risk": "none",
                "timeout": 10000,
            }
        ),
    )
    client = ScriptedClient(
        (
            _response(traced_call),
            _response(_finish({"file": "src/router.py"})),
        )
    )

    result = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=client,
    ).execute(CodeScoutRequest("Find router", workspace, engine="codescout"))

    observation = next(
        message for message in client.requests[1].messages if message.role == "tool"
    )
    payload = json.loads(observation.content or "{}")
    assert payload["command"] == "ls src"
    assert payload["exit_code"] == 0
    assert result.citations == (Citation("src/router.py", 1, 3),)


def test_turn_budget_exhaustion_requires_finish(workspace: Path) -> None:
    client = ScriptedClient(
        (
            _response(_terminal("one", "ls src")),
            _response(_terminal("two", "ls src")),
        )
    )
    engine = CodeScoutEngine(_target(api_key_env=None), model_client=client)

    with pytest.raises(CodeScoutEngineError, match="turn budget"):
        engine.execute(
            CodeScoutRequest(
                "Find router",
                workspace,
                engine="codescout",
                runtime=RuntimeOptions(max_turns=2),
            )
        )


def test_configured_turn_budget_overrides_request_default(workspace: Path) -> None:
    client = ScriptedClient(
        (
            _response(_terminal("one", "ls src")),
            _response(_terminal("two", "cat src/router.py")),
            _response(_finish({"file": "src/router.py"})),
        )
    )
    engine = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=client,
        max_turns=3,
    )

    result = engine.execute(
        CodeScoutRequest(
            "Find router",
            workspace,
            engine="codescout",
            runtime=RuntimeOptions(max_turns=1),
        )
    )

    assert result.metadata["model_calls"] == 3


def test_request_turn_budget_override_beats_configured_budget(workspace: Path) -> None:
    client = ScriptedClient((_response(_finish({"file": "src/router.py"})),))
    engine = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=client,
        max_turns=3,
    )

    engine.execute(
        CodeScoutRequest(
            "Find router",
            workspace,
            engine="codescout",
            runtime=RuntimeOptions(max_turns_override=7),
        )
    )

    assert client.requests[0].messages[0].content.endswith("You have up to 7 turns.")


def test_endpoint_failure_is_actionable_and_redacted(workspace: Path) -> None:
    client = ScriptedClient(
        (
            OpenAICompatibilityError(
                "Endpoint exposed super-secret in its internal body.",
                "Check endpoint.",
            ),
        )
    )
    engine = CodeScoutEngine(
        _target(),
        api_key="super-secret",
        model_client=client,
    )

    with pytest.raises(CodeScoutEngineError, match="endpoint failed") as caught:
        engine.execute(CodeScoutRequest("Find router", workspace, engine="codescout"))

    assert "super-secret" not in str(caught.value)
    trajectory = next(
        (workspace / ".code-scout" / "trajectories" / "codescout").glob("*.jsonl")
    )
    assert "super-secret" not in trajectory.read_text(encoding="utf-8")


def test_plain_answer_without_localization_finish_is_rejected(workspace: Path) -> None:
    client = ScriptedClient((_response(content="src/router.py"),))

    with pytest.raises(CodeScoutEngineError, match="without calling"):
        CodeScoutEngine(
            _target(api_key_env=None),
            model_client=client,
        ).execute(CodeScoutRequest("Find router", workspace, engine="codescout"))


def test_malformed_localization_finish_never_counts_as_success(
    workspace: Path,
) -> None:
    malformed = OpenAIToolCall(
        "finish",
        "localization_finish",
        '{"locations":[{"file":"../escape.py"}]}',
    )
    client = ScriptedClient((_response(malformed),))

    with pytest.raises(CodeScoutEngineError, match="turn budget"):
        CodeScoutEngine(
            _target(api_key_env=None),
            model_client=client,
        ).execute(
            CodeScoutRequest(
                "Find router",
                workspace,
                engine="codescout",
                runtime=RuntimeOptions(max_turns=1),
            )
        )


def test_exact_function_uses_code_scout_symbol_range(workspace: Path) -> None:
    _add_symbols(workspace, ("src/router.py", "dispatch", 2, 3))
    engine = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient(
            (
                _response(
                    _finish({"file": "src/router.py", "function_name": "dispatch"})
                ),
            )
        ),
    )

    result = engine.execute(
        CodeScoutRequest("Find dispatch", workspace, engine="codescout")
    )

    assert result.citations == (Citation("src/router.py", 2, 3),)
    assert result.metadata["localization_resolution"] == {
        "predictions": [
            {
                "file": "src/router.py",
                "class_name": None,
                "function_name": "dispatch",
                "status": "resolved",
                "source": "symbol",
                "citation": Citation("src/router.py", 2, 3).to_dict(),
            }
        ]
    }


def test_module_prediction_uses_bounded_textual_lookup(workspace: Path) -> None:
    result = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient(
            (_response(_finish({"file": "src/router.py", "class_name": "Router"})),)
        ),
    ).execute(CodeScoutRequest("Find Router", workspace, engine="codescout"))

    assert result.citations == (Citation("src/router.py", 1),)
    assert "src/router.py:Router (lines 1-1)" in result.answer


def test_duplicate_file_predictions_are_deterministically_deduplicated(
    workspace: Path,
) -> None:
    location = {"file": "src/router.py"}
    result = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient((_response(_finish(location, location)),)),
    ).execute(CodeScoutRequest("Find router", workspace, engine="codescout"))

    assert result.citations == (Citation("src/router.py", 1, 3),)
    assert result.answer.count("- src/router.py") == 1


def test_ambiguous_symbols_are_unresolved_with_warning(workspace: Path) -> None:
    _add_symbols(
        workspace,
        ("src/router.py", "dispatch", 1, 1),
        ("src/router.py", "dispatch", 2, 3),
    )
    result = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient(
            (
                _response(
                    _finish({"file": "src/router.py", "function_name": "dispatch"})
                ),
            )
        ),
    ).execute(CodeScoutRequest("Find dispatch", workspace, engine="codescout"))

    assert result.citations == ()
    assert tuple(warning.code for warning in result.warnings) == (
        "localization-ambiguous-symbol",
    )
    assert "Unresolved predictions:" in result.answer


def test_absent_symbol_is_unresolved_without_inventing_a_line(workspace: Path) -> None:
    result = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient(
            (_response(_finish({"file": "src/router.py", "function_name": "missing"})),)
        ),
    ).execute(CodeScoutRequest("Find missing", workspace, engine="codescout"))

    assert result.citations == ()
    assert result.warnings[0].code == "localization-symbol-not-found"
    assert "src/router.py:missing" in result.answer


def test_registry_validates_mixed_valid_and_invalid_predictions(
    workspace: Path,
) -> None:
    engine = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient(
            (
                _response(
                    _finish(
                        {"file": "missing.py"},
                        {"file": "src/router.py", "function_name": "dispatch"},
                        {"file": "src/router.py", "function_name": "absent"},
                    )
                ),
            )
        ),
    )
    registry = EngineRegistry()
    registry.register(engine)

    result = registry.execute(
        CodeScoutRequest("Find router changes", workspace, engine="fast")
    )

    assert result.citations == (Citation("src/router.py", 2),)
    assert tuple(warning.code for warning in result.warnings) == (
        "localization-invalid-path",
        "localization-symbol-not-found",
    )
    assert "Unresolved predictions:" in result.answer


def test_registry_requires_one_valid_codescout_citation(workspace: Path) -> None:
    engine = CodeScoutEngine(
        _target(api_key_env=None),
        model_client=ScriptedClient((_response(_finish({"file": "missing.py"})),)),
    )
    registry = EngineRegistry()
    registry.register(engine)

    with pytest.raises(EvidenceRequiredError, match="no valid citations"):
        registry.execute(CodeScoutRequest("Find missing", workspace, engine="fast"))


@pytest.mark.parametrize("mapped", [True, False])
def test_fast_assesses_only_identified_local_evidence(workspace, mapped):
    from code_scout.commands.cli import render_scout_result
    from code_scout.engines.citations import validate_result_citations
    from code_scout.storage import RawEvidenceUnit, upsert_raw_evidence_units

    _add_symbols(workspace, ("src/router.py", "dispatch", 2, 3))
    with open_storage(workspace) as connection:
        upsert_raw_evidence_units(
            connection,
            (
                RawEvidenceUnit(
                    "raw:dispatch",
                    "symbol",
                    "symbol-0",
                    "src/router.py",
                    "dispatch",
                    "heuristic",
                    1.0,
                    2 if mapped else 1,
                    3,
                    {
                        "evidence_metadata": {
                            "unresolved": True,
                            "resolver_conflicts": ["target"],
                        }
                    },
                ),
            ),
        )
        connection.commit()
    client = ScriptedClient(
        (
            # Forged labels are rejected by the provider boundary; the retry supplies
            # a valid prediction. Neither provider labels nor prose becomes evidence.
            _response(
                _finish(
                    {
                        "file": "src/router.py",
                        "assessment": {"validation": "corroborated"},
                    }
                )
            ),
            _response(
                _finish({"file": "src/router.py", "function_name": "dispatch"}),
                content="validated",
            ),
        )
    )
    result = CodeScoutEngine(_target(api_key_env=None), model_client=client).execute(
        CodeScoutRequest("Find dispatch", workspace, "fast")
    )
    result = validate_result_citations(result, workspace)
    support = result.citations[0].supporting_evidence[0]
    expected = (
        ("inferred", "unresolved", "conflicted", "unknown") if mapped else ("unknown",)
    )
    assert support.assessment.labels == expected
    assert support.evidence_id == ("raw:dispatch" if mapped else None)
    payload = json.loads(render_scout_result(result, "json"))
    assert payload["citations"][0]["supporting_evidence"][0]["assessment"][
        "labels"
    ] == list(expected)
    assert ", ".join(expected) in render_scout_result(result)
    assert len(client.requests) == 2


def test_fast_empty_predictions_have_no_assessed_evidence(workspace):
    from code_scout.commands.cli import render_scout_result

    result = CodeScoutEngine(
        _target(api_key_env=None), model_client=ScriptedClient((_response(_finish()),))
    ).execute(CodeScoutRequest("Find absent code", workspace, "fast"))
    assert result.citations == ()
    assert json.loads(render_scout_result(result, "json"))["citations"] == []
    assert "validated" not in render_scout_result(result)
