from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

import pytest

from code_scout.engines import (
    AvailabilityState,
    OpenAIChatMessage,
    OpenAIChatRequest,
    OpenAICompatibilityError,
    OpenAIModelTarget,
    OpenAIToolDefinition,
    check_provider_availability,
    complete_chat,
)


def _target() -> OpenAIModelTarget:
    return OpenAIModelTarget(
        base_url="http://localhost:8000/v1",
        model="shared-model",
        timeout_seconds=12,
        max_tokens=800,
        parameters={"temperature": 0.2},
    )


def test_shared_chat_completions_request_and_response_shape() -> None:
    target = _target()
    request = OpenAIChatRequest.from_target(
        target,
        (
            OpenAIChatMessage("system", "Cite source files."),
            OpenAIChatMessage("user", "Find the router."),
        ),
    )
    captured: dict[str, object] = {}

    def transport(http_request: Request, timeout: float) -> bytes:
        captured["url"] = http_request.full_url
        captured["timeout"] = timeout
        captured["authorization"] = http_request.get_header("Authorization")
        captured["payload"] = json.loads(http_request.data or b"{}")
        return json.dumps(
            {
                "id": "completion-1",
                "model": "shared-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "In router.py"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()

    response = complete_chat(
        target,
        request,
        api_key="secret-value",
        transport=transport,
    )

    assert response.content == "In router.py"
    assert captured == {
        "url": "http://localhost:8000/v1/chat/completions",
        "timeout": 12,
        "authorization": "Bearer secret-value",
        "payload": {
            "model": "shared-model",
            "messages": [
                {"role": "system", "content": "Cite source files."},
                {"role": "user", "content": "Find the router."},
            ],
            "max_tokens": 800,
            "temperature": 0.2,
        },
    }


def test_invalid_chat_response_is_actionable_and_does_not_expose_body() -> None:
    request = OpenAIChatRequest.from_target(
        _target(), (OpenAIChatMessage("user", "Question"),)
    )

    with pytest.raises(OpenAICompatibilityError, match="invalid") as caught:
        complete_chat(
            _target(),
            request,
            api_key=None,
            transport=lambda _request, _timeout: b'{"secret":"not-shown"}',
        )

    assert "not-shown" not in str(caught.value)


def test_shared_chat_boundary_serializes_and_parses_parallel_function_calls() -> None:
    target = _target()
    request = OpenAIChatRequest.from_target(
        target,
        (OpenAIChatMessage("user", "Find the router."),),
        tools=(
            OpenAIToolDefinition(
                "terminal",
                "Search the repository.",
                {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            ),
        ),
    )
    captured: dict[str, object] = {}

    def transport(http_request: Request, _timeout: float) -> bytes:
        captured.update(json.loads(http_request.data or b"{}"))
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "one",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": '{"command":"rg Router"}',
                                    },
                                },
                                {
                                    "id": "two",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": '{"command":"find src"}',
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
        ).encode()

    response = complete_chat(target, request, api_key=None, transport=transport)

    assert captured["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Search the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    ]
    assert [call.id for call in response.choices[0].message.tool_calls] == [
        "one",
        "two",
    ]


def test_availability_distinguishes_every_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    missing_dependency = check_provider_availability(
        target=_target(), dependency="optional_provider"
    )
    assert missing_dependency.state is AvailabilityState.MISSING_DEPENDENCY

    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    missing_executable = check_provider_availability(
        target=_target(),
        executable="provider-cli",
        environment={"PATH": str(tmp_path)},
    )
    assert missing_executable.state is AvailabilityState.MISSING_EXECUTABLE

    missing_endpoint = check_provider_availability(target=None)
    assert missing_endpoint.state is AvailabilityState.MISSING_ENDPOINT_CONFIGURATION

    unreachable = check_provider_availability(
        target=_target(),
        probe_endpoint=True,
        endpoint_probe=lambda _url, _timeout: (_ for _ in ()).throw(OSError()),
    )
    assert unreachable.state is AvailabilityState.UNREACHABLE_ENDPOINT

    not_probed = check_provider_availability(
        target=_target(),
        endpoint_probe=lambda _url, _timeout: (_ for _ in ()).throw(OSError()),
    )
    assert not_probed.state is AvailabilityState.READY

    probed = check_provider_availability(
        target=_target(),
        probe_endpoint=True,
        endpoint_probe=lambda _url, _timeout: None,
    )
    assert probed.state is AvailabilityState.READY
