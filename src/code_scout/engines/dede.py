from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from code_scout.engines.citations import validate_citations
from code_scout.engines.contracts import Citation
from code_scout.workspace.config import DedeEnrichmentConfig


@dataclass(frozen=True)
class DedeSource:
    source_file: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class DedeNode:
    id: str
    type: str
    display_name: str
    repository_name: str | None
    project_name: str | None
    certainty: str
    source: DedeSource | None
    metadata: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class DedeEdge:
    id: str
    source_id: str
    target_id: str
    type: str
    certainty: str
    unresolved: bool
    source: DedeSource | None
    metadata: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class DedeImpactPath:
    id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    certainty: str
    unresolved: bool


@dataclass(frozen=True)
class DedeImportResult:
    nodes: tuple[DedeNode, ...] = ()
    edges: tuple[DedeEdge, ...] = ()
    impact_paths: tuple[DedeImpactPath, ...] = ()
    unresolved: tuple[dict[str, Any], ...] = ()
    warnings: tuple[dict[str, str], ...] = ()


def import_configured_dede_graph(
    workspace: Path,
    config: DedeEnrichmentConfig,
    *,
    max_nodes: int,
    max_edges: int,
    max_paths: int,
    now: datetime | None = None,
) -> DedeImportResult:
    """Read one configured dede artifact without invoking the dede executable."""

    warnings: list[dict[str, str]] = []
    if _find_executable(config.executable) is None:
        warnings.append(
            _warning(
                "dede-missing-executable",
                f"Configured dede executable {config.executable!r} was not found; "
                "an existing graph can still be imported.",
            )
        )

    graph_path, reason = _workspace_graph_path(workspace, config.graph_path)
    if graph_path is None:
        return DedeImportResult(
            warnings=(
                *warnings,
                _warning(
                    "dede-invalid-graph-path", f"Dede graph was not read: {reason}."
                ),
            )
        )
    if not graph_path.is_file():
        return DedeImportResult(
            warnings=(
                *warnings,
                _warning(
                    "dede-missing-graph",
                    f"Configured dede graph {config.graph_path!r} does not exist.",
                ),
            )
        )

    try:
        document = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DedeImportResult(
            warnings=(
                *warnings,
                _warning(
                    "dede-incompatible-schema",
                    "Configured dede graph is not readable JSON; "
                    "base evidence was kept.",
                ),
            )
        )
    schema_error = _schema_error(document)
    if schema_error is not None:
        return DedeImportResult(
            warnings=(
                *warnings,
                _warning(
                    "dede-incompatible-schema",
                    f"Configured dede graph is incompatible: {schema_error}.",
                ),
            )
        )
    assert isinstance(document, dict)

    for item in document.get("warnings", []):
        if isinstance(item, str) and item.strip():
            warnings.append(_warning("dede-provider-warning", item))
        elif isinstance(item, dict):
            message = _string_field(item, "message", "detail", "warning")
            if message is not None:
                warnings.append(
                    _warning(
                        _string_field(item, "code") or "dede-provider-warning",
                        message,
                    )
                )

    stale_warning = _stale_warning(
        document["scanMetadata"],
        max_age_seconds=config.max_age_seconds,
        now=now or datetime.now(UTC),
    )
    if stale_warning is not None:
        warnings.append(stale_warning)

    unresolved_raw = tuple(
        item for item in document.get("unresolved", []) if isinstance(item, dict)
    )
    unresolved_ids = {
        value
        for item in unresolved_raw
        if (value := _string_field(item, "edgeId", "id")) is not None
    }
    nodes: list[DedeNode] = []
    for index, raw in enumerate(document["nodes"]):
        node = _node(workspace, raw, index, warnings)
        if node is not None:
            nodes.append(node)
    edges: list[DedeEdge] = []
    seen_edge_ids: set[str] = set()
    for index, raw in enumerate(document["edges"]):
        edge = _edge(workspace, raw, index, warnings, unresolved_ids)
        if edge is not None and edge.id not in seen_edge_ids:
            seen_edge_ids.add(edge.id)
            edges.append(edge)
    for index, raw in enumerate(unresolved_raw):
        if not _string_field(raw, "sourceId", "sourceNodeId", "source", "from"):
            continue
        edge = _edge(
            workspace,
            raw,
            index,
            warnings,
            unresolved_ids,
            force_unresolved=True,
        )
        if edge is not None and edge.id not in seen_edge_ids:
            seen_edge_ids.add(edge.id)
            edges.append(edge)

    paths = [
        path
        for index, raw in enumerate(document.get("impactPaths", []))
        if (path := _impact_path(raw, index)) is not None
    ]
    if len(nodes) > max_nodes or len(edges) > max_edges or len(paths) > max_paths:
        warnings.append(
            _warning(
                "dede-evidence-truncated",
                "Dede evidence exceeded the frozen-pack quota and was truncated.",
            )
        )
    return DedeImportResult(
        nodes=tuple(nodes[:max_nodes]),
        edges=tuple(edges[:max_edges]),
        impact_paths=tuple(paths[:max_paths]),
        unresolved=unresolved_raw,
        warnings=tuple(warnings),
    )


def dede_confidence(certainty: str) -> float:
    normalized = certainty.casefold().replace("_", "").replace("-", "")
    if normalized in {"certain", "confirmed", "exact", "high"}:
        return 1.0
    if normalized in {"probable", "likely"}:
        return 0.8
    if normalized in {"inferred", "medium"}:
        return 0.65
    if normalized in {"ambiguous", "low"}:
        return 0.3
    return 0.0


def _schema_error(document: object) -> str | None:
    if not isinstance(document, dict):
        return "top-level value must be an object"
    version = _field(document, "schemaVersion", "version")
    if version is not None and str(version).split(".", 1)[0] not in {"1", "v1"}:
        return f"unsupported schema version {version!r}"
    for field, expected in (
        ("nodes", list),
        ("edges", list),
        ("scanMetadata", dict),
    ):
        if not isinstance(document.get(field), expected):
            return f"{field!r} must be a {expected.__name__}"
    for optional in ("unresolved", "warnings", "impactPaths"):
        if optional in document and not isinstance(document[optional], list):
            return f"{optional!r} must be a list"
    return None


def _node(
    workspace: Path,
    raw: object,
    index: int,
    warnings: list[dict[str, str]],
) -> DedeNode | None:
    if not isinstance(raw, dict):
        warnings.append(_malformed_warning("node", index))
        return None
    node_id = _string_field(raw, "id")
    node_type = _string_field(raw, "type")
    if node_id is None or node_type is None:
        warnings.append(_malformed_warning("node", index))
        return None
    certainty = _string_field(raw, "certainty") or "Unknown"
    source = _source(
        workspace, _field(raw, "sourceLocation"), f"node {node_id}", warnings
    )
    return DedeNode(
        id=node_id,
        type=node_type,
        display_name=_string_field(raw, "displayName", "name") or node_id,
        repository_name=_string_field(raw, "repositoryName", "repository"),
        project_name=_string_field(raw, "projectName", "project"),
        certainty=certainty,
        source=source,
        metadata=_metadata(_field(raw, "metadata")),
    )


def _edge(
    workspace: Path,
    raw: object,
    index: int,
    warnings: list[dict[str, str]],
    unresolved_ids: set[str],
    *,
    force_unresolved: bool = False,
) -> DedeEdge | None:
    if not isinstance(raw, dict):
        warnings.append(_malformed_warning("edge", index))
        return None
    source_id = _string_field(raw, "sourceId", "sourceNodeId", "source", "from")
    target_id = _string_field(raw, "targetId", "targetNodeId", "target", "to")
    edge_type = _string_field(raw, "type", "relationType", "kind")
    if source_id is None or target_id is None or edge_type is None:
        warnings.append(_malformed_warning("edge", index))
        return None
    edge_id = _string_field(raw, "id") or f"{source_id}:{edge_type}:{target_id}"
    certainty = _string_field(raw, "certainty") or "Unknown"
    unresolved_value = _field(raw, "unresolved", "isUnresolved")
    unresolved = (
        force_unresolved
        or edge_id in unresolved_ids
        or unresolved_value is True
        or certainty.casefold() in {"unresolved", "unknown"}
    )
    return DedeEdge(
        id=edge_id,
        source_id=source_id,
        target_id=target_id,
        type=edge_type,
        certainty=certainty,
        unresolved=unresolved,
        source=_source(
            workspace, _field(raw, "sourceLocation"), f"edge {edge_id}", warnings
        ),
        metadata=_metadata(_field(raw, "metadata")),
    )


def _impact_path(raw: object, index: int) -> DedeImpactPath | None:
    if not isinstance(raw, dict):
        return None
    node_values = _field(raw, "nodeIds", "nodes", "path")
    edge_values = _field(raw, "edgeIds", "edges")
    if not isinstance(node_values, list) or not all(
        isinstance(item, str) and item for item in node_values
    ):
        return None
    if edge_values is None:
        edge_values = []
    if not isinstance(edge_values, list) or not all(
        isinstance(item, str) and item for item in edge_values
    ):
        return None
    certainty = _string_field(raw, "certainty") or "Unknown"
    return DedeImpactPath(
        id=_string_field(raw, "id") or f"dede-impact-path:{index}",
        node_ids=tuple(node_values),
        edge_ids=tuple(edge_values),
        certainty=certainty,
        unresolved=bool(_field(raw, "unresolved", "isUnresolved"))
        or certainty.casefold() in {"unresolved", "unknown"},
    )


def _source(
    workspace: Path,
    raw: object,
    owner: str,
    warnings: list[dict[str, str]],
) -> DedeSource | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append(
            _warning(
                "dede-invalid-source-location", f"Ignored invalid source for {owner}."
            )
        )
        return None
    path = _string_field(raw, "filePath", "path", "file")
    start_line = _integer_field(raw, "startLine", "line")
    end_line = _integer_field(raw, "endLine") or start_line
    if path is None or start_line is None or end_line is None:
        warnings.append(
            _warning(
                "dede-invalid-source-location", f"Ignored invalid source for {owner}."
            )
        )
        return None
    validation = validate_citations(workspace, (Citation(path, start_line, end_line),))
    if not validation.citations:
        warnings.append(
            _warning(
                "dede-invalid-source-location",
                f"Ignored invalid or out-of-workspace source for {owner}.",
            )
        )
        return None
    citation = validation.citations[0]
    assert citation.end_line is not None
    return DedeSource(citation.path, citation.start_line, citation.end_line)


def _workspace_graph_path(workspace: Path, value: str) -> tuple[Path | None, str]:
    raw = value.strip().replace("\\", "/")
    parsed = PurePosixPath(raw)
    if not raw or ".." in parsed.parts:
        return None, "path is empty or contains traversal"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace.joinpath(*parsed.parts)
    try:
        candidate.resolve(strict=False).relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return None, "path resolves outside the workspace"
    return candidate, ""


def _find_executable(executable: str) -> Path | None:
    candidate = Path(executable)
    if candidate.parent != Path("."):
        return (
            candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
        )
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry or ".") / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _stale_warning(
    metadata: object, *, max_age_seconds: float, now: datetime
) -> dict[str, str] | None:
    if not isinstance(metadata, dict):
        return None
    raw = _string_field(
        metadata,
        "completedAtUtc",
        "scanCompletedAtUtc",
        "generatedAtUtc",
        "completedAt",
    )
    if raw is None:
        return None
    try:
        completed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=UTC)
        age = (now.astimezone(UTC) - completed.astimezone(UTC)).total_seconds()
    except ValueError:
        return _warning(
            "dede-stale-metadata",
            "Dede scan completion metadata is invalid; freshness is unknown.",
        )
    if age <= max_age_seconds:
        return None
    return _warning(
        "dede-stale-graph",
        f"Dede graph is stale ({int(age)} seconds old); "
        "evidence was retained with a warning.",
    )


def _metadata(value: object) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, dict):
        return ()
    return tuple(
        (
            str(key),
            item
            if item is None or isinstance(item, (bool, int, float, str))
            else json.dumps(item, sort_keys=True, ensure_ascii=False),
        )
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    )


def _field(mapping: dict[str, Any], *names: str) -> Any:
    indexed = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        if name.casefold() in indexed:
            return indexed[name.casefold()]
    return None


def _string_field(mapping: dict[str, Any], *names: str) -> str | None:
    value = _field(mapping, *names)
    return value if isinstance(value, str) and value.strip() else None


def _integer_field(mapping: dict[str, Any], *names: str) -> int | None:
    value = _field(mapping, *names)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _malformed_warning(kind: str, index: int) -> dict[str, str]:
    return _warning(
        "dede-incompatible-item",
        f"Ignored malformed dede {kind} at index {index}.",
    )


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "provider": "dede"}
