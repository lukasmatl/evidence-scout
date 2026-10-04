from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from code_scout.core.errors import CodeScoutError
from code_scout.engines.contracts import OpenAIModelTarget


class OpenAICompatibilityError(CodeScoutError):
    """An OpenAI-compatible endpoint returned an invalid response."""


@dataclass(frozen=True)
class OpenAIToolCall:
    """One OpenAI function tool call returned by a chat model."""

    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("tool call id must not be empty.")
        if not self.name.strip():
            raise ValueError("tool call name must not be empty.")
        if not isinstance(self.arguments, str):
            raise ValueError("tool call arguments must be text.")


@dataclass(frozen=True)
class OpenAIChatMessage:
    """One message in an OpenAI Chat Completions request or response."""

    role: str
    content: str | None
    tool_calls: tuple[OpenAIToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("message role must not be empty.")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("message content must be text or null.")
        if self.content is None and not self.tool_calls:
            raise ValueError("null message content requires tool calls.")
        if self.tool_call_id is not None and not self.tool_call_id.strip():
            raise ValueError("tool_call_id must not be empty when provided.")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload


@dataclass(frozen=True)
class OpenAIToolDefinition:
    """JSON-schema function tool exposed through Chat Completions."""

    name: str
    description: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be empty.")
        if not self.description.strip():
            raise ValueError("tool description must not be empty.")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_payload(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


@dataclass(frozen=True)
class OpenAIChatRequest:
    """Provider-independent Chat Completions-shaped request."""

    model: str
    messages: tuple[OpenAIChatMessage, ...]
    max_tokens: int
    parameters: Mapping[str, object] = field(default_factory=dict)
    tools: tuple[OpenAIToolDefinition, ...] = ()

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("request model must not be empty.")
        if not self.messages:
            raise ValueError("request messages must not be empty.")
        if not 1 <= self.max_tokens <= 1_000_000:
            raise ValueError("request max_tokens must be between 1 and 1000000.")
        reserved = {"model", "messages", "max_tokens"}.intersection(self.parameters)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"parameters override managed request fields: {names}.")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    @classmethod
    def from_target(
        cls,
        target: OpenAIModelTarget,
        messages: Sequence[OpenAIChatMessage],
        *,
        tools: Sequence[OpenAIToolDefinition] = (),
    ) -> OpenAIChatRequest:
        return cls(
            model=target.model,
            messages=tuple(messages),
            max_tokens=target.max_tokens,
            parameters=target.parameters,
            tools=tuple(tools),
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [message.to_payload() for message in self.messages],
            "max_tokens": self.max_tokens,
            **self.parameters,
        }
        if self.tools:
            payload["tools"] = [tool.to_payload() for tool in self.tools]
        return payload


@dataclass(frozen=True)
class OpenAIChatChoice:
    index: int
    message: OpenAIChatMessage
    finish_reason: str | None = None


@dataclass(frozen=True)
class OpenAIChatResponse:
    """Validated subset shared by OpenAI-compatible model-backed engines."""

    id: str | None
    model: str | None
    choices: tuple[OpenAIChatChoice, ...]

    @property
    def content(self) -> str:
        if not self.choices:
            raise OpenAICompatibilityError(
                "The model endpoint returned no completion choices.",
                "Check the OpenAI-compatible server logs and model configuration.",
            )
        content = self.choices[0].message.content
        if content is None:
            raise OpenAICompatibilityError(
                "The model endpoint returned tool calls without answer text.",
                "Inspect the structured tool calls instead of response.content.",
            )
        return content

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> OpenAIChatResponse:
        try:
            raw_choices = payload["choices"]
            if not isinstance(raw_choices, list):
                raise TypeError("choices is not a list")
            choices = tuple(_parse_choice(choice) for choice in raw_choices)
            response_id = payload.get("id")
            model = payload.get("model")
            if response_id is not None and not isinstance(response_id, str):
                raise TypeError("id is not text")
            if model is not None and not isinstance(model, str):
                raise TypeError("model is not text")
        except (KeyError, TypeError, ValueError) as error:
            raise OpenAICompatibilityError(
                "The model endpoint returned an invalid Chat Completions response.",
                f"Expected choices[].message.role/content fields ({error}).",
            ) from error
        return cls(id=response_id, model=model, choices=choices)


OpenAITransport = Callable[[Request, float], bytes]


def complete_chat(
    target: OpenAIModelTarget,
    request: OpenAIChatRequest,
    *,
    api_key: str | None,
    transport: OpenAITransport | None = None,
) -> OpenAIChatResponse:
    """Send the one HTTP payload shape used by all model-backed engines."""

    endpoint = _chat_completions_url(target.base_url)
    headers = {"Content-Type": "application/json"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    http_request = Request(
        endpoint,
        data=json.dumps(request.to_payload()).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    send = _default_transport if transport is None else transport
    try:
        response_bytes = send(http_request, target.timeout_seconds)
        payload = json.loads(response_bytes)
        if not isinstance(payload, dict):
            raise TypeError("top-level response is not an object")
        return OpenAIChatResponse.from_payload(payload)
    except OpenAICompatibilityError:
        raise
    except Exception as error:
        raise OpenAICompatibilityError(
            f"OpenAI-compatible request to {endpoint} failed.",
            "Check the configured endpoint, model, credentials, and server logs.",
        ) from error


def _parse_choice(value: object) -> OpenAIChatChoice:
    if not isinstance(value, dict):
        raise TypeError("choice is not an object")
    index = value.get("index", 0)
    raw_message = value["message"]
    finish_reason = value.get("finish_reason")
    if not isinstance(index, int):
        raise TypeError("choice index is not an integer")
    if not isinstance(raw_message, dict):
        raise TypeError("choice message is not an object")
    role = raw_message["role"]
    content = raw_message.get("content")
    if not isinstance(role, str) or (
        content is not None and not isinstance(content, str)
    ):
        raise TypeError("choice message role/content has an invalid type")
    tool_calls = _parse_tool_calls(raw_message.get("tool_calls", []))
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise TypeError("finish_reason is not text")
    return OpenAIChatChoice(
        index=index,
        message=OpenAIChatMessage(
            role=role,
            content=content,
            tool_calls=tool_calls,
        ),
        finish_reason=finish_reason,
    )


def _parse_tool_calls(value: object) -> tuple[OpenAIToolCall, ...]:
    if not isinstance(value, list):
        raise TypeError("message tool_calls is not a list")
    calls: list[OpenAIToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("tool call is not an object")
        call_id = item["id"]
        function = item["function"]
        if item.get("type", "function") != "function":
            raise TypeError("tool call type is not function")
        if not isinstance(call_id, str) or not isinstance(function, dict):
            raise TypeError("tool call id/function has an invalid type")
        name = function["name"]
        arguments = function["arguments"]
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise TypeError("tool call name/arguments is not text")
        calls.append(OpenAIToolCall(call_id, name, arguments))
    return tuple(calls)


def _chat_completions_url(base_url: str) -> str:
    split = urlsplit(base_url)
    path = split.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/chat/completions"


def _default_transport(request: Request, timeout: float) -> bytes:
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()
