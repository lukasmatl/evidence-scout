from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

_SLUG_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")
_ROUTE_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HTTP_METHODS = frozenset(
    {
        "CONNECT",
        "DELETE",
        "GET",
        "HEAD",
        "OPTIONS",
        "PATCH",
        "POST",
        "PUT",
        "TRACE",
    },
)


def slugify_identifier(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_SEPARATOR_PATTERN.sub("-", ascii_value.lower()).strip("-")
    return slug or fallback


def canonical_http_contract_id(
    repository_id: str,
    method: str | None,
    path: str | None,
) -> str:
    identity = canonical_http_contract_identity(method, path)
    path_slug = slugify_identifier(identity.path, fallback="root")
    return f"contract:{repository_id}:http:{identity.method.lower()}:{path_slug}"


def canonical_http_contract_identity(
    method: str | None,
    path: str | None,
) -> CanonicalHTTPContractIdentity:
    return CanonicalHTTPContractIdentity(
        method=normalize_http_method(method),
        path=normalize_http_path(path),
    )


def normalize_http_method(method: str | None) -> str:
    if method is None:
        return "UNKNOWN"
    normalized = method.strip().upper()
    return normalized if normalized in _HTTP_METHODS else "UNKNOWN"


def normalize_http_path(path: str | None) -> str:
    if path is None:
        return "/"

    candidate = path.strip()
    if not candidate:
        return "/"
    candidate = candidate.split("?", maxsplit=1)[0].split("#", maxsplit=1)[0]
    candidate = candidate.replace("\\", "/")

    segments = tuple(
        _normalize_route_segment(segment)
        for segment in candidate.split("/")
        if segment.strip()
    )
    return "/" + "/".join(segments) if segments else "/"


def _normalize_route_segment(segment: str) -> str:
    segment = segment.strip()

    parameter_name = _route_parameter_name(segment)
    if parameter_name is not None:
        return "{" + parameter_name + "}"

    return segment.lower()


def _route_parameter_name(segment: str) -> str | None:
    if segment.startswith(":"):
        return _valid_route_parameter_name(segment[1:])

    if segment.startswith("{") and segment.endswith("}"):
        return _valid_route_parameter_name(segment[1:-1].lstrip("*"))

    if segment.startswith("[[...") and segment.endswith("]]"):
        return _valid_route_parameter_name(segment[5:-2])

    if segment.startswith("[...") and segment.endswith("]"):
        return _valid_route_parameter_name(segment[4:-1])

    if segment.startswith("[") and segment.endswith("]"):
        return _valid_route_parameter_name(segment[1:-1])

    if segment.startswith("<") and segment.endswith(">"):
        name = segment[1:-1]
        if ":" in name:
            name = name.rsplit(":", maxsplit=1)[1]
        return _valid_route_parameter_name(name)

    return None


def _valid_route_parameter_name(name: str) -> str | None:
    name = name.strip()
    return name if _ROUTE_PARAMETER_NAME_PATTERN.fullmatch(name) else None


class CanonicalHTTPContractIdentity(NamedTuple):
    method: str
    path: str
