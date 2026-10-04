from __future__ import annotations

from dataclasses import replace

import pytest

from code_scout.core.errors import ConfigError
from code_scout.extractors import EXTRACTOR_PROFILES as ROOT_EXTRACTOR_PROFILES
from code_scout.extractors.catalog import (
    EXTRACTOR_CATALOG,
    EXTRACTOR_PROFILES,
    replacement_extractors_for_selection,
    route_target,
    route_target_extractors,
    selected_profile_extractors,
    selected_scan_extractors,
    selected_watch_route_extractors,
    watch_profile_extractors,
)
from code_scout.models.workspace import default_single_repository_workspace
from code_scout.workspace.config import WorkspaceConfig


def test_catalog_preserves_extractor_profiles_import_compatibility() -> None:
    assert ROOT_EXTRACTOR_PROFILES is EXTRACTOR_PROFILES
    assert EXTRACTOR_PROFILES["docs"].extractors == ("adr", "docs", "openapi")


def test_catalog_selects_profile_scan_extractors_with_existing_symbol_alias() -> None:
    enabled = {
        "graphify",
        "tree_sitter",
        "lsp",
        "adr",
        "openapi",
        "symbol_extraction",
    }

    assert selected_profile_extractors("code-only", enabled) == (
        "graphify",
        "tree_sitter",
        "lsp",
        "symbol_extraction",
    )
    assert selected_profile_extractors("agent", enabled) == (
        "graphify",
        "openapi",
        "adr",
    )


def test_catalog_rejects_unknown_profiles_with_available_names() -> None:
    with pytest.raises(ConfigError) as error:
        selected_profile_extractors("unknown", set())

    assert "Unknown extractor profile `unknown`." in str(error.value)
    assert "agent" in (error.value.hint or "")


def test_catalog_selects_default_scan_extractors_like_scan_did() -> None:
    config = WorkspaceConfig()
    workspace = default_single_repository_workspace(".", config=config)

    selected = selected_scan_extractors(
        workspace,
        config,
        extractor=None,
        profile=None,
        graphify_source_configured=lambda *_args: False,
    )

    assert selected == (
        "tree_sitter",
        "adr",
        "docs",
        "infra",
        "openapi",
        "asyncapi",
        "package_metadata",
        "symbol_extraction",
    )


def test_catalog_selects_profile_for_watch_from_enabled_extractors() -> None:
    config = WorkspaceConfig()
    workspace = default_single_repository_workspace(".", config=config)
    workspace = replace(
        workspace,
        extractors=replace(
            workspace.extractors,
            enabled=("tree_sitter", "symbol_extraction", "docs"),
        ),
    )

    assert watch_profile_extractors(workspace, "code-only") == (
        "tree_sitter",
        "symbol_extraction",
    )


def test_catalog_filters_watch_route_extractors_by_profile_and_graphify_source() -> (
    None
):
    config = WorkspaceConfig()
    workspace = default_single_repository_workspace(".", config=config)
    route_extractors = (
        "graphify",
        "tree_sitter",
        "symbol_extraction",
        "lsp",
        "docs",
    )

    assert selected_watch_route_extractors(
        route_extractors,
        workspace=workspace,
        profile="code-only",
        graphify_source_configured=False,
    ) == (
        "tree_sitter",
        "symbol_extraction",
        "lsp",
    )
    assert selected_watch_route_extractors(
        route_extractors,
        workspace=workspace,
        profile="docs",
        graphify_source_configured=True,
    ) == ("docs",)


def test_catalog_extracts_route_target_names_in_order() -> None:
    assert route_target_extractors(
        (
            route_target("graphify", "primary_code_evidence"),
            route_target("tree_sitter", "fallback_or_additive_facts"),
        )
    ) == ("graphify", "tree_sitter")


def test_catalog_replacement_aliases_match_existing_behavior() -> None:
    assert replacement_extractors_for_selection(
        ("symbol_extraction", "adr", "package_metadata", "openapi"),
    ) == (
        "file_discovery",
        "symbol_extraction",
        "tree_sitter",
        "heuristic",
        "dotnet",
        "byond",
        "frontend_api",
        "adr",
        "adr_markdown",
        "package_metadata",
        "package_json",
        "openapi",
    )


def test_catalog_symbol_extraction_and_adapter_tier_metadata() -> None:
    assert EXTRACTOR_CATALOG["symbol_extraction"].replacement_aliases == (
        "symbol_extraction",
        "tree_sitter",
        "heuristic",
        "dotnet",
        "byond",
        "frontend_api",
    )
    assert EXTRACTOR_CATALOG["tree_sitter"].adapter_tier == "tree_sitter"
    assert EXTRACTOR_CATALOG["heuristic"].adapter_tier == "heuristic"
    assert EXTRACTOR_CATALOG["dotnet"].adapter_tier == "dotnet"
    assert EXTRACTOR_CATALOG["byond"].adapter_tier == "byond"
