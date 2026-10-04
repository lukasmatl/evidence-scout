from __future__ import annotations

import json
import select
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen

from code_scout.evidence.navigation import (
    CodeScoutSymbolNavigator,
    NavigationCandidate,
    NavigationOperation,
    NavigationResult,
    SymbolNavigator,
)
from code_scout.evidence.pack import EvidencePack, PackedSourceProvenance

_READ_ONLY_TOOLS = frozenset(
    {
        "find_symbol",
        "find_declarations",
        "find_implementations",
        "find_referencing_symbols",
        "get_symbols_overview",
    }
)


class SerenaTransport(Protocol):
    """Small injectable MCP boundary used by the optional Serena navigator."""

    def call_tool(
        self, name: str, arguments: Mapping[str, object], timeout: float
    ) -> object: ...


class SerenaMCPError(RuntimeError):
    def __init__(self, message: str, *, unsupported: bool = False) -> None:
        self.unsupported = unsupported
        super().__init__(message)


@dataclass(frozen=True)
class SerenaConfig:
    """Connection details for one explicitly configured Serena MCP server."""

    command: tuple[str, ...] = ()
    url: str | None = None
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if bool(self.command) == bool(self.url):
            raise ValueError("configure exactly one Serena command or URL")
        if self.url is not None and not self.url.startswith(("http://", "https://")):
            raise ValueError("Serena URL must be an absolute http(s) URL")
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Serena timeout_seconds must be between 0 and 120")
        if any(not part.strip() for part in self.command):
            raise ValueError("Serena command arguments must not be empty")


class SerenaMCPTransport:
    """Minimal stdlib MCP client supporting configured stdio or HTTP servers."""

    def __init__(self, config: SerenaConfig) -> None:
        self._config = config

    def call_tool(
        self, name: str, arguments: Mapping[str, object], timeout: float
    ) -> object:
        if name not in _READ_ONLY_TOOLS:
            raise SerenaMCPError(f"Serena tool {name!r} is not permitted")
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        }
        if self._config.url is not None:
            return self._http_call(request, timeout)
        return self._stdio_call(request, timeout)

    def _http_call(self, payload: dict[str, object], timeout: float) -> object:
        assert self._config.url is not None
        initialized, headers = self._http_request(
            _initialize_request(1), timeout, accept_empty=False
        )
        _json_rpc_result(initialized)
        session_id = headers.get("Mcp-Session-Id")
        session_headers = {"Mcp-Session-Id": session_id} if session_id else {}
        self._http_request(
            _initialized_notification(),
            timeout,
            extra_headers=session_headers,
            accept_empty=True,
        )
        result, _headers = self._http_request(
            payload,
            timeout,
            extra_headers=session_headers,
            accept_empty=False,
        )
        return _json_rpc_result(result)

    def _http_request(
        self,
        payload: dict[str, object],
        timeout: float,
        *,
        extra_headers: Mapping[str, str] | None = None,
        accept_empty: bool,
    ) -> tuple[object, Mapping[str, str]]:
        assert self._config.url is not None
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **dict(extra_headers or {}),
        }
        request = Request(
            self._config.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            if not body and accept_empty:
                decoded: object = {}
            else:
                decoded = _decode_http_payload(body)
            return decoded, response.headers

    def _stdio_call(self, payload: dict[str, object], timeout: float) -> object:
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                self._config.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            assert process.stdin is not None
            deadline = time.monotonic() + timeout
            _write_stdio(process, _initialize_request(1))
            _json_rpc_result(_read_stdio_response(process, 1, deadline))
            _write_stdio(process, _initialized_notification())
            request = dict(payload)
            request["id"] = 2
            _write_stdio(process, request)
            return _json_rpc_result(_read_stdio_response(process, 2, deadline))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Serena MCP process timed out") from error
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)


class SerenaSymbolNavigator:
    """Prefer live Serena symbols, falling back to one frozen evidence pack."""

    def __init__(
        self,
        workspace: str | Path,
        pack: EvidencePack,
        config: SerenaConfig,
        *,
        transport: SerenaTransport | None = None,
        fallback: SymbolNavigator | None = None,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._pack = pack
        self._config = config
        self._transport = transport or SerenaMCPTransport(config)
        self._fallback = fallback or CodeScoutSymbolNavigator(pack)

    def definitions(self, symbol: str) -> NavigationResult:
        return self._navigate("definitions", symbol, "find_symbol")

    def implementations(self, symbol: str) -> NavigationResult:
        return self._navigate("implementations", symbol, "find_implementations")

    def references(self, symbol: str) -> NavigationResult:
        return self._navigate("references", symbol, "find_referencing_symbols")

    def callers(self, symbol: str) -> NavigationResult:
        return self._navigate("callers", symbol, "find_referencing_symbols")

    def callees(self, symbol: str) -> NavigationResult:
        return self._fallback_with_warning(
            "callees",
            symbol,
            "serena-unsupported-operation",
            "Serena does not expose a read-only callee lookup; "
            "code-scout evidence was used.",
        )

    def overview(self, path: str) -> NavigationResult:
        return self._navigate(
            "overview", path, "get_symbols_overview", arguments={"relative_path": path}
        )

    def _navigate(
        self,
        operation: NavigationOperation,
        requested: str,
        tool: str,
        *,
        arguments: Mapping[str, object] | None = None,
    ) -> NavigationResult:
        if not requested.strip():
            raise ValueError("symbol or path must not be empty")
        call_arguments = dict(arguments or {"name_path_pattern": requested})
        call_arguments.setdefault("include_body", False)
        try:
            response = self._transport.call_tool(
                tool, call_arguments, self._config.timeout_seconds
            )
            locations = _response_locations(response)
            candidates, warnings = self._validated_candidates(
                operation, requested, locations
            )
        except TimeoutError:
            return self._fallback_with_warning(
                operation,
                requested,
                "serena-timeout",
                "Serena symbol lookup timed out; code-scout evidence was used.",
            )
        except SerenaMCPError as error:
            code = (
                "serena-unsupported-operation"
                if error.unsupported
                else "serena-unavailable"
            )
            return self._fallback_with_warning(
                operation,
                requested,
                code,
                "Serena could not serve this symbol lookup; "
                "code-scout evidence was used.",
            )
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return self._fallback_with_warning(
                operation,
                requested,
                "serena-malformed-response",
                "Serena returned a malformed symbol response; "
                "code-scout evidence was used.",
            )

        if not candidates:
            fallback = self._fallback_for(operation, requested)
            warning = {
                "code": "serena-no-valid-locations",
                "message": (
                    "Serena returned no valid workspace locations; "
                    "code-scout evidence was used."
                ),
            }
            return replace(fallback, warnings=(*warnings, warning, *fallback.warnings))
        status = "ambiguous" if len(candidates) > 1 else "confirmed"
        return NavigationResult(
            operation=operation,
            requested_symbol=requested,
            status=status,
            candidates=tuple(candidates),
            coverage={"serena": "available", "lsp": "live"},
            warnings=warnings,
            provider="serena",
        )

    def _validated_candidates(
        self,
        operation: NavigationOperation,
        requested: str,
        locations: Sequence[Mapping[str, object]],
    ) -> tuple[list[NavigationCandidate], tuple[dict[str, str], ...]]:
        # Import at use time: importing ``code_scout.engines`` initializes its
        # public registry, which includes the verify engine that owns these tools.
        from code_scout.engines.citations import validate_citations
        from code_scout.engines.contracts import Citation

        parsed = [_location(item) for item in locations]
        validation = validate_citations(
            self._workspace,
            (Citation(path, start, end) for path, start, end, _name, _kind in parsed),
        )
        accepted = {
            (item.path, item.start_line, item.end_line) for item in validation.citations
        }
        candidates: list[NavigationCandidate] = []
        seen: set[tuple[str, int, int, str]] = set()
        for path, start, end, name, kind in parsed:
            key = (path.replace("\\", "/"), start, end, name)
            normalized = next(
                (
                    item
                    for item in validation.citations
                    if item.start_line == start
                    and item.end_line == end
                    and (
                        item.path == key[0]
                        or (self._workspace / item.path).resolve()
                        == Path(path).resolve()
                    )
                ),
                None,
            )
            if normalized is None or (normalized.path, start, end) not in accepted:
                continue
            candidate_key = (normalized.path, start, end, name)
            if candidate_key in seen:
                continue
            seen.add(candidate_key)
            source = PackedSourceProvenance(normalized.path, start, end, "serena", 1.0)
            symbol_id = name or requested
            candidates.append(
                NavigationCandidate(
                    id=f"serena:{operation}:{normalized.path}:{start}:{symbol_id}",
                    symbol_id=symbol_id,
                    name=name or requested,
                    kind=kind or "symbol",
                    path=normalized.path,
                    start_line=start,
                    end_line=end,
                    extractor="serena",
                    confidence=1.0,
                    confidence_class="confirmed",
                    provenance=(source,),
                    neighbor_id=symbol_id if operation != "definitions" else None,
                )
            )
        warnings = tuple(
            {"code": warning.code, "message": warning.message}
            for warning in validation.warnings
        )
        return candidates, warnings

    def _fallback_with_warning(
        self,
        operation: NavigationOperation,
        requested: str,
        code: str,
        message: str,
    ) -> NavigationResult:
        fallback = self._fallback_for(operation, requested)
        return replace(
            fallback,
            warnings=({"code": code, "message": message}, *fallback.warnings),
        )

    def _fallback_for(
        self, operation: NavigationOperation, requested: str
    ) -> NavigationResult:
        return getattr(self._fallback, operation)(requested)


def configured_symbol_navigator(
    workspace: str | Path,
    pack: EvidencePack,
    *,
    transport: SerenaTransport | None = None,
) -> SymbolNavigator:
    """Select Serena only when the workspace explicitly enables it."""

    from code_scout.core.errors import ConfigError
    from code_scout.workspace.config import load_workspace_config

    fallback = CodeScoutSymbolNavigator(pack)
    try:
        workspace_config = load_workspace_config(workspace)
    except ConfigError:
        return fallback
    serena = workspace_config.navigation.serena
    if not serena.enabled:
        return fallback
    config = SerenaConfig(
        command=serena.command,
        url=serena.url,
        timeout_seconds=serena.timeout_seconds,
    )
    return SerenaSymbolNavigator(
        workspace,
        pack,
        config,
        transport=transport,
        fallback=fallback,
    )


def _json_rpc_result(response: object) -> object:
    if not isinstance(response, dict):
        raise SerenaMCPError("Serena returned a non-object JSON-RPC response")
    if "error" in response:
        error = response["error"]
        text = json.dumps(error).casefold()
        raise SerenaMCPError(
            "Serena rejected the MCP tool call",
            unsupported=any(
                word in text for word in ("not found", "unsupported", "unknown tool")
            ),
        )
    if "result" not in response:
        raise SerenaMCPError("Serena JSON-RPC response omitted result")
    return response["result"]


def _initialize_request(request_id: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "code-scout", "version": "1"},
        },
    }


def _initialized_notification() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }


def _write_stdio(process: subprocess.Popen[str], payload: Mapping[str, object]) -> None:
    if process.stdin is None:
        raise SerenaMCPError("Serena MCP process has no input stream")
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()


def _read_stdio_response(
    process: subprocess.Popen[str], request_id: int, deadline: float
) -> object:
    if process.stdout is None:
        raise SerenaMCPError("Serena MCP process has no output stream")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Serena MCP process timed out")
        ready, _writable, _exceptional = select.select(
            [process.stdout], [], [], remaining
        )
        if not ready:
            raise TimeoutError("Serena MCP process timed out")
        line = process.stdout.readline()
        if not line:
            raise SerenaMCPError("Serena MCP process closed its output stream")
        response = json.loads(line)
        if isinstance(response, dict) and response.get("id") == request_id:
            return response


def _decode_http_payload(body: bytes) -> object:
    text = body.decode("utf-8")
    if text.lstrip().startswith("data:"):
        data_lines = [
            line.removeprefix("data:").strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            raise ValueError("Serena SSE response contained no data")
        text = data_lines[-1]
    return json.loads(text)


def _response_locations(response: object) -> list[Mapping[str, object]]:
    value = response
    if isinstance(value, dict) and value.get("isError") is True:
        text = json.dumps(value).casefold()
        raise SerenaMCPError(
            "Serena tool returned an error",
            unsupported=any(
                word in text for word in ("not found", "unsupported", "unknown tool")
            ),
        )
    if isinstance(value, dict) and "structuredContent" in value:
        value = value["structuredContent"]
    if isinstance(value, dict) and "content" in value:
        content = value["content"]
        if not isinstance(content, list):
            raise ValueError("MCP content must be a list")
        texts = [item.get("text") for item in content if isinstance(item, dict)]
        if not texts or not isinstance(texts[0], str):
            raise ValueError("MCP content has no text payload")
        value = json.loads(texts[0])
    if isinstance(value, dict):
        for key in ("locations", "symbols", "result"):
            if key in value:
                value = value[key]
                break
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Serena response must contain a list of locations")
    return value


def _location(item: Mapping[str, object]) -> tuple[str, int, int, str, str]:
    path = item.get("relative_path", item.get("path", item.get("uri")))
    body = item.get("body_location", item.get("range", {}))
    if not isinstance(path, str) or not isinstance(body, dict):
        raise ValueError("Serena location is missing path or range")
    start_value = body.get("start_line", body.get("start"))
    end_value = body.get("end_line", body.get("end", start_value))
    if isinstance(start_value, dict):
        start_value = start_value.get("line")
    if isinstance(end_value, dict):
        end_value = end_value.get("line")
    if isinstance(start_value, bool) or not isinstance(start_value, int):
        raise ValueError("Serena start line is invalid")
    if isinstance(end_value, bool) or not isinstance(end_value, int):
        raise ValueError("Serena end line is invalid")
    # Serena's body_location uses zero-based inclusive lines.
    start, end = start_value + 1, end_value + 1
    name = item.get("name_path", item.get("name", ""))
    kind = item.get("kind", "symbol")
    return path, start, end, str(name), str(kind)


__all__ = [
    "SerenaConfig",
    "SerenaMCPError",
    "SerenaMCPTransport",
    "SerenaSymbolNavigator",
    "SerenaTransport",
    "configured_symbol_navigator",
]
