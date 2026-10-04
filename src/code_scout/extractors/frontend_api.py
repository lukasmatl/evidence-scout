from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from code_scout.discovery import DiscoveredFile
from code_scout.models.graph import Contract, Edge, Evidence
from code_scout.utils.identifiers import canonical_http_contract_id

EXTRACTOR_NAME = "frontend_api"
FRONTEND_API_CONFIDENCE = 0.82
FRONTEND_EXTENSIONS = frozenset({".cts", ".mts", ".ts", ".tsx"})
HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
METHOD_NAMES = "|".join(method.lower() for method in sorted(HTTP_METHODS))
URL_LITERAL = r"""(?P<url_quote>["'`])(?P<url_value>(?:\\.|(?!\1).)*?)(?P=url_quote)"""
METHOD_LITERAL = (
    r"""(?P<method_quote>["'`])"""
    r"""(?P<method_value>(?:\\.|(?!\1).)*?)"""
    r"""(?P=method_quote)"""
)

FETCH_CALL_PATTERN = re.compile(
    rf"\bfetch\s*(?:<[^()]*>)?\s*\(\s*{URL_LITERAL}",
    re.DOTALL,
)
METHOD_CALL_PATTERN = re.compile(
    rf"""
    \b(?P<object>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)
    \s*\.\s*(?P<method>{METHOD_NAMES})
    \s*(?:<[^()]*>)?\s*\(\s*{URL_LITERAL}
    """,
    re.DOTALL | re.IGNORECASE | re.VERBOSE,
)
OBJECT_CALL_PATTERN = re.compile(
    r"""
    \b(?P<callee>axios|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.request)
    \s*(?:<[^()]*>)?\s*\(\s*\{
    (?P<body>[\s\S]{0,1200}?)
    \}\s*\)
    """,
    re.VERBOSE,
)
METHOD_PROPERTY_PATTERN = re.compile(
    rf"""(?:\bmethod\b|["']method["'])\s*:\s*{METHOD_LITERAL}""",
    re.DOTALL,
)
URL_PROPERTY_PATTERN = re.compile(
    rf"""(?:\burl\b|["']url["'])\s*:\s*{URL_LITERAL}""",
    re.DOTALL,
)
CLIENT_NAME_TOKENS = frozenset(
    {
        "api",
        "axios",
        "client",
        "http",
        "ky",
        "request",
        "service",
    },
)


@dataclass(frozen=True)
class FrontendAPIUsage:
    repository_id: str
    file_path: str
    method: str
    path: str
    url: str
    line_number: int
    call_kind: str


def frontend_api_contracts(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[Contract, ...]:
    contracts: list[Contract] = []
    seen_ids: set[str] = set()
    for usage in frontend_api_usages(discovered_files):
        contract = _usage_contract(usage)
        if contract.id in seen_ids:
            continue
        seen_ids.add(contract.id)
        contracts.append(contract)
    return tuple(contracts)


def frontend_api_edges(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[Edge, ...]:
    return _dedupe_usage_edges(
        tuple(_usage_edge(usage) for usage in frontend_api_usages(discovered_files))
    )


def frontend_api_usages(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[FrontendAPIUsage, ...]:
    usages: list[FrontendAPIUsage] = []
    seen: set[tuple[str, str, str, int, str]] = set()
    for discovered_file in discovered_files:
        if discovered_file.extension not in FRONTEND_EXTENSIONS:
            continue
        source = Path(discovered_file.absolute_path).read_text(encoding="utf-8")
        for usage in _file_usages(discovered_file, source):
            key = (
                usage.file_path,
                usage.method,
                usage.path,
                usage.line_number,
                usage.call_kind,
            )
            if key in seen:
                continue
            seen.add(key)
            usages.append(usage)
    return tuple(usages)


def _file_usages(
    discovered_file: DiscoveredFile,
    source: str,
) -> tuple[FrontendAPIUsage, ...]:
    usages: list[FrontendAPIUsage] = []
    for match in FETCH_CALL_PATTERN.finditer(source):
        url = _literal_value(match, "url")
        path = _normalized_http_path(url)
        if path is None:
            continue
        method = _method_after_fetch_url(source, match.end()) or "GET"
        usages.append(
            _usage(
                discovered_file,
                method=method,
                path=path,
                url=url or "",
                source=source,
                match=match,
            )
        )

    for match in METHOD_CALL_PATTERN.finditer(source):
        object_name = match.group("object")
        if object_name == "axios" or _looks_like_typed_client(object_name):
            url = _literal_value(match, "url")
            path = _normalized_http_path(url)
            if path is None:
                continue
            usages.append(
                _usage(
                    discovered_file,
                    method=match.group("method").upper(),
                    path=path,
                    url=url or "",
                    source=source,
                    match=match,
                    call_kind=f"{object_name}.{match.group('method').lower()}",
                )
            )

    for match in OBJECT_CALL_PATTERN.finditer(source):
        callee = match.group("callee")
        if callee != "axios" and not _looks_like_typed_client(callee):
            continue
        method_match = METHOD_PROPERTY_PATTERN.search(match.group("body"))
        url_match = URL_PROPERTY_PATTERN.search(match.group("body"))
        if method_match is None or url_match is None:
            continue
        method = _http_method(_literal_value(method_match, "method"))
        url = _literal_value(url_match, "url")
        path = _normalized_http_path(url)
        if method is None or path is None:
            continue
        usages.append(
            _usage(
                discovered_file,
                method=method,
                path=path,
                url=url or "",
                source=source,
                match=match,
                call_kind=callee,
            )
        )
    return tuple(usages)


def _usage(
    discovered_file: DiscoveredFile,
    *,
    method: str,
    path: str,
    url: str,
    source: str,
    match: re.Match[str],
    call_kind: str = "fetch",
) -> FrontendAPIUsage:
    return FrontendAPIUsage(
        repository_id=discovered_file.repository_id,
        file_path=discovered_file.path,
        method=method,
        path=path,
        url=url,
        line_number=_line_number(source, match.start()),
        call_kind=call_kind,
    )


def _method_after_fetch_url(source: str, offset: int) -> str | None:
    snippet = source[offset : offset + 700]
    terminator_index = snippet.find(");")
    if terminator_index >= 0:
        snippet = snippet[:terminator_index]
    method_match = METHOD_PROPERTY_PATTERN.search(snippet)
    if method_match is None:
        return None
    return _http_method(_literal_value(method_match, "method"))


def _http_method(value: str | None) -> str | None:
    if value is None:
        return None
    method = value.upper()
    return method if method in HTTP_METHODS else None


def _literal_value(match: re.Match[str], prefix: str) -> str | None:
    quote = match.group(f"{prefix}_quote")
    value = match.group(f"{prefix}_value")
    if quote == "`" and "${" in value:
        return None
    if quote == "`":
        return value
    try:
        return ast.literal_eval(f"{quote}{value}{quote}")
    except (SyntaxError, ValueError):
        return value


def _normalized_http_path(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if candidate.startswith(("http://", "https://")):
        parsed = urlsplit(candidate)
        candidate = parsed.path or "/"
    elif candidate.startswith("/") and not candidate.startswith("//"):
        candidate = candidate.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    else:
        return None
    normalized = f"/{candidate.strip('/')}" if candidate.strip("/") else "/"
    return normalized


def _looks_like_typed_client(object_name: str) -> bool:
    normalized_name = object_name.lower()
    tokens = {
        token
        for part in re.split(r"[._$]", object_name.lower())
        for token in re.findall(r"[a-z]+", part)
    }
    return bool(tokens & CLIENT_NAME_TOKENS) or any(
        token in normalized_name for token in CLIENT_NAME_TOKENS
    )


def _usage_contract(usage: FrontendAPIUsage) -> Contract:
    return Contract(
        id=_contract_id(usage.repository_id, usage.path, usage.method),
        name=f"{usage.method} {usage.path}",
        kind="http",
        path=usage.path,
        method=usage.method,
        evidence=(_usage_evidence(usage),),
    )


def _usage_edge(usage: FrontendAPIUsage) -> Edge:
    return Edge(
        source_type="file",
        source_id=_file_id(usage.repository_id, usage.file_path),
        target_type="contract",
        target_id=_contract_id(usage.repository_id, usage.path, usage.method),
        relation_type="consumes",
        confidence=FRONTEND_API_CONFIDENCE,
        evidence=(_usage_evidence(usage),),
    )


def _usage_evidence(usage: FrontendAPIUsage) -> Evidence:
    return Evidence(
        file_path=usage.file_path,
        extractor_name=EXTRACTOR_NAME,
        confidence=FRONTEND_API_CONFIDENCE,
        start_line=usage.line_number,
        end_line=usage.line_number,
        metadata={
            "repository_id": usage.repository_id,
            "call_kind": usage.call_kind,
            "method": usage.method,
            "route": usage.path,
            "url": usage.url,
        },
    )


def _dedupe_usage_edges(edges: tuple[Edge, ...]) -> tuple[Edge, ...]:
    merged: dict[tuple[str, str, str, str, str], Edge] = {}
    for edge in edges:
        key = (
            edge.source_type,
            edge.source_id,
            edge.target_type,
            edge.target_id,
            edge.relation_type,
        )
        if key in merged:
            existing = merged[key]
            merged[key] = Edge(
                source_type=existing.source_type,
                source_id=existing.source_id,
                target_type=existing.target_type,
                target_id=existing.target_id,
                relation_type=existing.relation_type,
                confidence=max(existing.confidence, edge.confidence),
                evidence=(*existing.evidence, *edge.evidence),
            )
        else:
            merged[key] = edge
    return tuple(merged[key] for key in sorted(merged))


def _contract_id(repository_id: str, path: str, method: str) -> str:
    return canonical_http_contract_id(repository_id, method, path)


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _line_number(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1
