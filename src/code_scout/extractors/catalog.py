from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from code_scout.core.errors import ConfigError
from code_scout.extractors.profiles import (
    EXTRACTOR_PROFILES,
)
from code_scout.extractors.profiles import (
    ExtractorProfile as ExtractorProfile,
)


@dataclass(frozen=True)
class ExtractorRouteRole:
    name: str
    role: str


@dataclass(frozen=True)
class ExtractorCatalogEntry:
    name: str
    profiles: tuple[str, ...] = ()
    route_roles: tuple[str, ...] = ()
    adapter_tier: str | None = None
    replacement_aliases: tuple[str, ...] = ()


def _profiles_for_extractor(extractor_name: str) -> tuple[str, ...]:
    return tuple(
        profile.name
        for profile in EXTRACTOR_PROFILES.values()
        if extractor_name in profile.scan_extractors
    )


EXTRACTOR_CATALOG = {
    entry.name: entry
    for entry in (
        ExtractorCatalogEntry(
            name="graphify",
            profiles=_profiles_for_extractor("graphify"),
            route_roles=("primary_code_evidence",),
        ),
        ExtractorCatalogEntry(
            name="tree_sitter",
            profiles=_profiles_for_extractor("tree_sitter"),
            route_roles=("fallback_or_additive_facts",),
            adapter_tier="tree_sitter",
        ),
        ExtractorCatalogEntry(
            name="symbol_extraction",
            profiles=_profiles_for_extractor("symbol_extraction"),
            route_roles=("symbol_extraction_boundary",),
            replacement_aliases=(
                "symbol_extraction",
                "tree_sitter",
                "heuristic",
                "dotnet",
                "byond",
                "frontend_api",
            ),
        ),
        ExtractorCatalogEntry(
            name="lsp",
            profiles=_profiles_for_extractor("lsp"),
            route_roles=("validation_or_enrichment",),
            replacement_aliases=("lsp",),
        ),
        ExtractorCatalogEntry(
            name="adr",
            profiles=_profiles_for_extractor("adr"),
            route_roles=("adr_decision_facts",),
            replacement_aliases=("adr", "adr_markdown"),
        ),
        ExtractorCatalogEntry(
            name="docs",
            profiles=_profiles_for_extractor("docs"),
            route_roles=("documentation_concept_facts",),
        ),
        ExtractorCatalogEntry(
            name="openapi",
            profiles=_profiles_for_extractor("openapi"),
            route_roles=("http_contract_facts",),
        ),
        ExtractorCatalogEntry(
            name="asyncapi",
            profiles=_profiles_for_extractor("asyncapi"),
            route_roles=("event_contract_facts",),
        ),
        ExtractorCatalogEntry(
            name="infra",
            profiles=_profiles_for_extractor("infra"),
            route_roles=(
                "compose_runtime_topology",
                "dockerfile_runtime_topology",
            ),
        ),
        ExtractorCatalogEntry(
            name="package_metadata",
            profiles=_profiles_for_extractor("package_metadata"),
            route_roles=("package_metadata_facts",),
            replacement_aliases=("package_metadata", "package_json"),
        ),
        ExtractorCatalogEntry(
            name="heuristic",
            adapter_tier="heuristic",
        ),
        ExtractorCatalogEntry(
            name="dotnet",
            adapter_tier="dotnet",
        ),
        ExtractorCatalogEntry(
            name="byond",
            adapter_tier="byond",
        ),
        ExtractorCatalogEntry(name="frontend_api"),
    )
}


def selected_scan_extractors(
    workspace: Any,
    config: Any,
    extractor: str | None,
    profile: str | None = None,
    *,
    graphify_source_configured: Callable[[Any, Any], bool],
) -> tuple[str, ...]:
    enabled = set(workspace.extractors.enabled)
    if extractor is not None and profile is not None:
        raise ConfigError(
            "Scan accepts either an extractor or a profile, not both.",
            "Pass `--extractor` for one path or `--profile` for a named set.",
        )
    if extractor is not None:
        return (extractor,) if extractor in enabled else ()
    if profile is not None:
        return selected_profile_extractors(profile, enabled)

    return tuple(
        name
        for name in workspace.extractors.enabled
        if name != "lsp"
        and (name != "graphify" or graphify_source_configured(workspace, config))
    )


def selected_profile_extractors(
    profile: str,
    enabled_extractors: set[str],
) -> tuple[str, ...]:
    definition = EXTRACTOR_PROFILES.get(profile)
    if definition is None:
        raise ConfigError(
            f"Unknown extractor profile `{profile}`.",
            "Use one of: " + ", ".join(sorted(EXTRACTOR_PROFILES)) + ".",
        )
    return tuple(
        name for name in definition.scan_extractors if name in enabled_extractors
    )


def watch_profile_extractors(workspace: Any, profile: str | None) -> tuple[str, ...]:
    if profile is None:
        return workspace.extractors.enabled
    return selected_profile_extractors(profile, set(workspace.extractors.enabled))


def route_target_extractors(
    targets: Iterable[ExtractorRouteRole],
) -> tuple[str, ...]:
    return tuple(target.name for target in targets)


def selected_watch_route_extractors(
    route_extractors: tuple[str, ...],
    *,
    workspace: Any,
    profile: str | None,
    graphify_source_configured: bool,
) -> tuple[str, ...]:
    profile_extractors = watch_profile_extractors(workspace, profile)
    return tuple(
        extractor
        for extractor in route_extractors
        if extractor in profile_extractors
        and (extractor != "graphify" or graphify_source_configured)
    )


def replacement_extractors_for_selection(
    selected_extractors: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                "file_discovery",
                *(
                    alias
                    for selected in selected_extractors
                    for alias in _replacement_aliases(selected)
                ),
            ),
        ),
    )


def route_target(extractor_name: str, role: str) -> ExtractorRouteRole:
    entry = EXTRACTOR_CATALOG[extractor_name]
    if role not in entry.route_roles:
        raise ConfigError(
            f"Extractor `{extractor_name}` is not cataloged for route role `{role}`.",
            "Use one of: " + ", ".join(entry.route_roles) + ".",
        )
    return ExtractorRouteRole(extractor_name, role)


def _replacement_aliases(extractor_name: str) -> tuple[str, ...]:
    entry = EXTRACTOR_CATALOG.get(extractor_name)
    if entry is None or not entry.replacement_aliases:
        return (extractor_name,)
    return entry.replacement_aliases
