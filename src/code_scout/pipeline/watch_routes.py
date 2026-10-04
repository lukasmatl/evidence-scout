from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from code_scout.discovery import DEFAULT_ADR_PATHS
from code_scout.extractors import (
    ExtractorRouteRole,
    route_target,
    route_target_extractors,
)
from code_scout.extractors.asyncapi import ASYNCAPI_CANDIDATE_NAMES
from code_scout.extractors.openapi import OPENAPI_CANDIDATE_NAMES
from code_scout.models.workspace import Workspace

WatchExtractorTarget = ExtractorRouteRole


@dataclass(frozen=True)
class WatchRoute:
    id: str
    globs: tuple[str, ...]
    targets: tuple[WatchExtractorTarget, ...]
    priority: int
    supersedes: tuple[str, ...] = ()

    @property
    def extractors(self) -> tuple[str, ...]:
        return route_target_extractors(self.targets)


CODE_TARGETS = (
    route_target("graphify", "primary_code_evidence"),
    route_target("tree_sitter", "fallback_or_additive_facts"),
    route_target("symbol_extraction", "symbol_extraction_boundary"),
    route_target("lsp", "validation_or_enrichment"),
)

WATCH_ROUTES = (
    WatchRoute(
        id="typescript",
        globs=("*.ts", "*.mts", "*.cts"),
        targets=CODE_TARGETS,
        priority=80,
    ),
    WatchRoute(
        id="tsx",
        globs=("*.tsx",),
        targets=CODE_TARGETS,
        priority=80,
    ),
    WatchRoute(
        id="adr_markdown",
        globs=(),
        targets=(route_target("adr", "adr_decision_facts"),),
        priority=90,
        supersedes=("markdown",),
    ),
    WatchRoute(
        id="markdown",
        globs=("*.md", "*.mdx"),
        targets=(route_target("docs", "documentation_concept_facts"),),
        priority=10,
    ),
    WatchRoute(
        id="openapi",
        globs=tuple(sorted(OPENAPI_CANDIDATE_NAMES)),
        targets=(route_target("openapi", "http_contract_facts"),),
        priority=70,
    ),
    WatchRoute(
        id="asyncapi",
        globs=tuple(sorted(ASYNCAPI_CANDIDATE_NAMES)),
        targets=(route_target("asyncapi", "event_contract_facts"),),
        priority=70,
    ),
    WatchRoute(
        id="docker_compose",
        globs=("docker-compose.yml", "docker-compose.yaml"),
        targets=(route_target("infra", "compose_runtime_topology"),),
        priority=60,
    ),
    WatchRoute(
        id="dockerfile",
        globs=("Dockerfile", "Dockerfile.*"),
        targets=(route_target("infra", "dockerfile_runtime_topology"),),
        priority=60,
    ),
    WatchRoute(
        id="package_json",
        globs=("package.json",),
        targets=(route_target("package_metadata", "package_metadata_facts"),),
        priority=50,
    ),
)


def match_watch_routes(
    path: str,
    *,
    workspace: Workspace | None = None,
) -> tuple[WatchRoute, ...]:
    normalized_path = _normalize_path(path)
    candidate_routes = _watch_routes_for_workspace(workspace)
    matched = tuple(
        route for route in candidate_routes if _route_matches(route, normalized_path)
    )
    superseded = {route_id for route in matched for route_id in route.supersedes}
    return tuple(
        route
        for route in sorted(matched, key=lambda route: (-route.priority, route.id))
        if route.id not in superseded
    )


def watch_extractors_for_paths(
    paths: tuple[str, ...],
    *,
    workspace: Workspace | None = None,
) -> tuple[str, ...]:
    extractors: list[str] = []
    seen: set[str] = set()
    for path in sorted(paths, key=_normalize_path):
        for route in match_watch_routes(path, workspace=workspace):
            for extractor in route.extractors:
                if extractor in seen:
                    continue
                seen.add(extractor)
                extractors.append(extractor)
    return tuple(extractors)


def _watch_routes_for_workspace(workspace: Workspace | None) -> tuple[WatchRoute, ...]:
    adr_paths = (
        DEFAULT_ADR_PATHS
        if workspace is None
        else (
            *DEFAULT_ADR_PATHS,
            *workspace.adr_paths,
        )
    )
    openapi_paths = () if workspace is None else workspace.openapi_paths
    asyncapi_paths = () if workspace is None else workspace.asyncapi_paths
    infra_paths = () if workspace is None else workspace.infra_paths
    package_paths = () if workspace is None else workspace.package_metadata_paths

    return tuple(
        _route_with_workspace_globs(
            route,
            adr_paths=adr_paths,
            openapi_paths=openapi_paths,
            asyncapi_paths=asyncapi_paths,
            infra_paths=infra_paths,
            package_paths=package_paths,
        )
        for route in WATCH_ROUTES
    )


def _route_with_workspace_globs(
    route: WatchRoute,
    *,
    adr_paths: tuple[str, ...],
    openapi_paths: tuple[str, ...],
    asyncapi_paths: tuple[str, ...],
    infra_paths: tuple[str, ...],
    package_paths: tuple[str, ...],
) -> WatchRoute:
    configured_globs: tuple[str, ...] = ()
    if route.id == "adr_markdown":
        configured_globs = tuple(
            glob for path in adr_paths for glob in _path_markdown_globs(path)
        )
    elif route.id == "openapi":
        configured_globs = tuple(
            glob for path in openapi_paths for glob in _configured_path_globs(path)
        )
    elif route.id == "asyncapi":
        configured_globs = tuple(
            glob for path in asyncapi_paths for glob in _configured_path_globs(path)
        )
    elif route.id in {"docker_compose", "dockerfile"}:
        configured_globs = tuple(
            glob for path in infra_paths for glob in _configured_path_globs(path)
        )
    elif route.id == "package_json":
        configured_globs = tuple(
            glob for path in package_paths for glob in _configured_path_globs(path)
        )
    if not configured_globs:
        return route
    return WatchRoute(
        id=route.id,
        globs=_dedupe((*route.globs, *configured_globs)),
        targets=route.targets,
        priority=route.priority,
        supersedes=route.supersedes,
    )


def _path_markdown_globs(path: str) -> tuple[str, ...]:
    normalized = _normalize_path(path)
    if not normalized:
        return ()
    return (
        f"{normalized}/*.md",
        f"{normalized}/*.mdx",
        f"{normalized}/**/*.md",
        f"{normalized}/**/*.mdx",
    )


def _configured_path_globs(path: str) -> tuple[str, ...]:
    normalized = _normalize_path(path)
    if not normalized:
        return ()
    return (normalized, f"{normalized}/**")


def _route_matches(route: WatchRoute, path: str) -> bool:
    basename = Path(path).name
    comparable_path = path.lower()
    comparable_basename = basename.lower()
    return any(
        _glob_matches(comparable_path, comparable_basename, glob)
        for glob in route.globs
    )


def _glob_matches(path: str, basename: str, glob: str) -> bool:
    normalized_glob = _normalize_path(glob).lower()
    if "/" not in normalized_glob:
        return fnmatch.fnmatchcase(basename, normalized_glob)
    return fnmatch.fnmatchcase(path, normalized_glob)


def _normalize_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./").strip("/")


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
