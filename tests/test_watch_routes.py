from __future__ import annotations

from code_scout.models.workspace import default_single_repository_workspace
from code_scout.pipeline.watch_routes import (
    match_watch_routes,
    watch_extractors_for_paths,
)
from code_scout.workspace.config import OptionalPathConfig, WorkspaceConfig


def test_typescript_route_targets_code_evidence_modes() -> None:
    routes = match_watch_routes("src/users/service.ts")

    assert _route_ids(routes) == ("typescript",)
    assert routes[0].extractors == (
        "graphify",
        "tree_sitter",
        "symbol_extraction",
        "lsp",
    )
    assert {target.name: target.role for target in routes[0].targets} == {
        "graphify": "primary_code_evidence",
        "tree_sitter": "fallback_or_additive_facts",
        "symbol_extraction": "symbol_extraction_boundary",
        "lsp": "validation_or_enrichment",
    }


def test_tsx_route_targets_code_evidence_modes() -> None:
    routes = match_watch_routes("apps/web/app/users/page.tsx")

    assert _route_ids(routes) == ("tsx",)
    assert routes[0].extractors == (
        "graphify",
        "tree_sitter",
        "symbol_extraction",
        "lsp",
    )


def test_general_markdown_routes_to_docs() -> None:
    routes = match_watch_routes("docs/overview.md")

    assert _route_ids(routes) == ("markdown",)
    assert routes[0].extractors == ("docs",)


def test_adr_markdown_supersedes_general_markdown_route() -> None:
    routes = match_watch_routes("docs/adr/0001-use-sqlite.md")

    assert _route_ids(routes) == ("adr_markdown",)
    assert routes[0].extractors == ("adr",)


def test_configured_adr_path_routes_to_adr() -> None:
    workspace = default_single_repository_workspace(
        ".",
        config=WorkspaceConfig(
            paths=OptionalPathConfig(adr=("./records/decisions",)),
        ),
    )

    routes = match_watch_routes(
        "records/decisions/0002-use-uv.md",
        workspace=workspace,
    )

    assert _route_ids(routes) == ("adr_markdown",)


def test_openapi_route_targets_openapi_extractor() -> None:
    routes = match_watch_routes("contracts/openapi.yaml")

    assert _route_ids(routes) == ("openapi",)
    assert routes[0].extractors == ("openapi",)


def test_configured_openapi_path_routes_to_openapi_extractor() -> None:
    workspace = default_single_repository_workspace(
        ".",
        config=WorkspaceConfig(
            paths=OptionalPathConfig(openapi=("./contracts/public-api.yaml",)),
        ),
    )

    routes = match_watch_routes("contracts/public-api.yaml", workspace=workspace)

    assert _route_ids(routes) == ("openapi",)


def test_asyncapi_route_targets_asyncapi_extractor() -> None:
    routes = match_watch_routes("contracts/asyncapi.json")

    assert _route_ids(routes) == ("asyncapi",)
    assert routes[0].extractors == ("asyncapi",)


def test_configured_asyncapi_path_routes_to_asyncapi_extractor() -> None:
    workspace = default_single_repository_workspace(
        ".",
        config=WorkspaceConfig(
            paths=OptionalPathConfig(asyncapi=("./contracts/events.yaml",)),
        ),
    )

    routes = match_watch_routes("contracts/events.yaml", workspace=workspace)

    assert _route_ids(routes) == ("asyncapi",)


def test_docker_compose_route_targets_infra_extractor() -> None:
    routes = match_watch_routes("deploy/docker-compose.yml")

    assert _route_ids(routes) == ("docker_compose",)
    assert routes[0].extractors == ("infra",)


def test_dockerfile_route_targets_infra_extractor() -> None:
    routes = match_watch_routes("apps/api/Dockerfile")

    assert _route_ids(routes) == ("dockerfile",)
    assert routes[0].extractors == ("infra",)


def test_package_json_route_targets_package_metadata_extractor() -> None:
    routes = match_watch_routes("apps/web/package.json")

    assert _route_ids(routes) == ("package_json",)
    assert routes[0].extractors == ("package_metadata",)


def test_watch_extractors_for_paths_are_deterministic_and_deduped() -> None:
    extractors = watch_extractors_for_paths(
        (
            "docs/adr/0001-use-sqlite.md",
            "src/app.ts",
            "apps/web/page.tsx",
            "package.json",
        ),
    )

    assert extractors == (
        "graphify",
        "tree_sitter",
        "symbol_extraction",
        "lsp",
        "adr",
        "package_metadata",
    )


def _route_ids(routes) -> tuple[str, ...]:
    return tuple(route.id for route in routes)
