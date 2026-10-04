from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from code_scout.discovery import DiscoveredFile
from code_scout.models.graph import Edge, Evidence
from code_scout.utils.active_facts import merge_active_facts_by_id
from code_scout.utils.identifiers import slugify_identifier

if TYPE_CHECKING:
    from code_scout.models.facts import ActiveFact, FactEvidence

EXTRACTOR_NAME = "package_json"
PACKAGE_METADATA_FILENAMES = frozenset(
    {
        "package.json",
        "tsconfig.json",
        "turbo.json",
        "nx.json",
        "angular.json",
    },
)
FRAMEWORK_CONFIG_PREFIX_HINTS = {
    "astro.config.": "Astro",
    "next.config.": "Next.js",
    "nuxt.config.": "Nuxt",
    "remix.config.": "Remix",
    "svelte.config.": "SvelteKit",
    "vite.config.": "Vite",
    "vue.config.": "Vue",
}
FRAMEWORK_CONFIG_FILENAME_HINTS = {
    "angular.json": "Angular",
}
NEXT_DEPENDENCIES = frozenset({"next"})
NESTJS_DEPENDENCY_PREFIXES = ("@nestjs/",)
TYPESCRIPT_DEPENDENCIES = frozenset({"typescript"})
TURBO_DEPENDENCIES = frozenset({"turbo"})
NX_DEPENDENCIES = frozenset({"nx"})
NX_DEPENDENCY_PREFIXES = ("@nx/", "@nrwl/")


@dataclass(frozen=True)
class PackageJsonEntry:
    name: str
    value: str


@dataclass(frozen=True)
class PackageJsonMetadata:
    name: str | None
    scripts: tuple[PackageJsonEntry, ...] = ()
    dependencies: tuple[PackageJsonEntry, ...] = ()
    dev_dependencies: tuple[PackageJsonEntry, ...] = ()
    workspaces: tuple[str, ...] = ()
    private: bool | None = None

    def to_metadata(self) -> dict[str, Any]:
        facts = infer_package_json_facts(self)
        return {
            "name": self.name,
            "scripts": _entries_to_dict(self.scripts),
            "dependencies": _entries_to_dict(self.dependencies),
            "dev_dependencies": _entries_to_dict(self.dev_dependencies),
            "workspaces": list(self.workspaces),
            "private": self.private,
            "language_hints": list(facts.language_hints),
            "framework_hints": list(facts.framework_hints),
        }


@dataclass(frozen=True)
class RepositoryPackageFacts:
    repository_id: str
    language_hints: tuple[str, ...] = ()
    framework_hints: tuple[str, ...] = ()
    package_files_analyzed: int = 0
    evidence: tuple[Evidence, ...] = ()


def parse_package_json_file(
    discovered_file: DiscoveredFile,
) -> PackageJsonMetadata | None:
    if Path(discovered_file.path).name != "package.json":
        return None

    with Path(discovered_file.absolute_path).open(encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        return PackageJsonMetadata(name=None)

    return PackageJsonMetadata(
        name=_optional_string(payload.get("name")),
        scripts=_string_map_entries(payload.get("scripts")),
        dependencies=_string_map_entries(payload.get("dependencies")),
        dev_dependencies=_string_map_entries(payload.get("devDependencies")),
        workspaces=_workspace_entries(payload.get("workspaces")),
        private=_optional_bool(payload.get("private")),
    )


def package_json_edges(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[Edge, ...]:
    return package_metadata_edges(discovered_files)


def package_metadata_edges(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[Edge, ...]:
    edges: list[Edge] = []

    for discovered_file in discovered_files:
        metadata = _metadata_for_discovered_file(discovered_file)
        if metadata is None:
            continue

        edges.append(
            Edge(
                source_type="repository",
                source_id=discovered_file.repository_id,
                target_type="file",
                target_id=_file_id(discovered_file.repository_id, discovered_file.path),
                relation_type="declares_package_metadata",
                confidence=1.0,
                evidence=(
                    Evidence(
                        file_path=discovered_file.path,
                        extractor_name=EXTRACTOR_NAME,
                        confidence=1.0,
                        start_line=1,
                        end_line=_line_count(discovered_file.absolute_path),
                        metadata=metadata,
                    ),
                ),
            ),
        )

    return tuple(edges)


def package_metadata_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[ActiveFact, ...]:
    from code_scout.models.facts import (
        EntityFact,
        ExtractorMetadata,
        FactReference,
        RelationFact,
        entity_fact_to_active_fact,
        relation_fact_to_active_fact,
    )

    active_facts: list[ActiveFact] = []
    extractor_metadata = ExtractorMetadata(name=EXTRACTOR_NAME)

    for discovered_file in discovered_files:
        metadata = _metadata_for_discovered_file(discovered_file)
        if metadata is None:
            continue

        evidence = _fact_evidence(discovered_file, metadata)
        file_reference = FactReference(
            type="file",
            id=_file_id(discovered_file.repository_id, discovered_file.path),
        )
        repository_reference = FactReference(
            type="repository",
            id=discovered_file.repository_id,
        )
        active_facts.append(
            relation_fact_to_active_fact(
                RelationFact(
                    subject=repository_reference,
                    relation="declares_package_metadata",
                    object=file_reference,
                    properties={
                        "repository_id": discovered_file.repository_id,
                        "path": discovered_file.path,
                    },
                    evidence=(evidence,),
                    confidence=1.0,
                    extractor_metadata=extractor_metadata,
                ),
            ),
        )

        package_reference = None
        package_metadata = parse_package_json_file(discovered_file)
        if package_metadata is not None:
            package_reference = FactReference(
                type="package",
                id=_package_id(
                    discovered_file.repository_id,
                    package_metadata.name,
                    discovered_file.path,
                ),
            )
            active_facts.append(
                entity_fact_to_active_fact(
                    EntityFact(
                        entity_type="package",
                        entity_id=package_reference.id,
                        properties={
                            "repository_id": discovered_file.repository_id,
                            "path": discovered_file.path,
                            "name": package_metadata.name,
                            "private": package_metadata.private,
                            "scripts": _entries_to_dict(package_metadata.scripts),
                            "dependencies": _entries_to_dict(
                                package_metadata.dependencies
                            ),
                            "dev_dependencies": _entries_to_dict(
                                package_metadata.dev_dependencies
                            ),
                            "workspaces": package_metadata.workspaces,
                            "language_hints": tuple(metadata["language_hints"]),
                            "framework_hints": tuple(metadata["framework_hints"]),
                        },
                        evidence=(evidence,),
                        confidence=1.0,
                        extractor_metadata=extractor_metadata,
                    ),
                ),
            )
            for workspace_pattern in package_metadata.workspaces:
                active_facts.append(
                    relation_fact_to_active_fact(
                        RelationFact(
                            subject=package_reference,
                            relation="declares_workspace",
                            object=FactReference(
                                type="workspace",
                                id=_workspace_id(
                                    discovered_file.repository_id,
                                    workspace_pattern,
                                ),
                            ),
                            properties={
                                "repository_id": discovered_file.repository_id,
                                "path": discovered_file.path,
                                "workspace_pattern": workspace_pattern,
                            },
                            evidence=(evidence,),
                            confidence=1.0,
                            extractor_metadata=extractor_metadata,
                        ),
                    ),
                )

        framework_subject = package_reference or repository_reference
        for framework_hint in metadata["framework_hints"]:
            active_facts.append(
                relation_fact_to_active_fact(
                    RelationFact(
                        subject=framework_subject,
                        relation="uses_framework",
                        object=FactReference(
                            type="framework",
                            id=_framework_id(framework_hint),
                        ),
                        properties={
                            "repository_id": discovered_file.repository_id,
                            "path": discovered_file.path,
                            "framework": framework_hint,
                        },
                        evidence=(evidence,),
                        confidence=1.0,
                        extractor_metadata=extractor_metadata,
                    ),
                ),
            )

    return merge_active_facts_by_id(*active_facts)


def infer_repository_package_facts(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[RepositoryPackageFacts, ...]:
    mutable_facts: dict[str, dict[str, Any]] = {}

    for discovered_file in discovered_files:
        filename = Path(discovered_file.path).name
        if not _is_package_metadata_file(filename):
            continue

        fact = mutable_facts.setdefault(
            discovered_file.repository_id,
            {
                "language_hints": set(),
                "framework_hints": set(),
                "package_files_analyzed": 0,
            },
        )
        fact["package_files_analyzed"] += 1
        language_hints, framework_hints = _file_hints(discovered_file)
        fact["language_hints"].update(language_hints)
        fact["framework_hints"].update(framework_hints)

    return tuple(
        RepositoryPackageFacts(
            repository_id=repository_id,
            language_hints=tuple(sorted(fact["language_hints"])),
            framework_hints=tuple(sorted(fact["framework_hints"])),
            package_files_analyzed=fact["package_files_analyzed"],
        )
        for repository_id, fact in sorted(mutable_facts.items())
    )


def infer_package_json_facts(metadata: PackageJsonMetadata) -> RepositoryPackageFacts:
    dependency_names = {
        entry.name for entry in (*metadata.dependencies, *metadata.dev_dependencies)
    }
    language_hints: set[str] = set()
    framework_hints: set[str] = set()

    if dependency_names & TYPESCRIPT_DEPENDENCIES:
        language_hints.add("TypeScript")
    if dependency_names & NEXT_DEPENDENCIES:
        framework_hints.add("Next.js")
    if any(name.startswith(NESTJS_DEPENDENCY_PREFIXES) for name in dependency_names):
        framework_hints.add("NestJS")
    if dependency_names & TURBO_DEPENDENCIES:
        framework_hints.add("turbo")
    if dependency_names & NX_DEPENDENCIES or any(
        name.startswith(NX_DEPENDENCY_PREFIXES) for name in dependency_names
    ):
        framework_hints.add("nx")

    return RepositoryPackageFacts(
        repository_id="",
        language_hints=tuple(sorted(language_hints)),
        framework_hints=tuple(sorted(framework_hints)),
        package_files_analyzed=1,
    )


def _string_map_entries(value: Any) -> tuple[PackageJsonEntry, ...]:
    if not isinstance(value, dict):
        return ()

    return tuple(
        PackageJsonEntry(name=str(name), value=str(entry_value))
        for name, entry_value in sorted(value.items())
    )


def _workspace_entries(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(sorted(str(item) for item in value if isinstance(item, str)))
    if isinstance(value, dict):
        packages = value.get("packages")
        if isinstance(packages, list):
            return tuple(
                sorted(str(item) for item in packages if isinstance(item, str))
            )
    return ()


def _entries_to_dict(entries: tuple[PackageJsonEntry, ...]) -> dict[str, str]:
    return {entry.name: entry.value for entry in entries}


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _line_count(path: str) -> int:
    with Path(path).open(encoding="utf-8") as file:
        return sum(1 for _line in file)


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _package_id(repository_id: str, name: str | None, path: str) -> str:
    label = name or path
    return f"package:{repository_id}:{slugify_identifier(label, fallback='package')}"


def _workspace_id(repository_id: str, pattern: str) -> str:
    return f"workspace:{repository_id}:{slugify_identifier(pattern, fallback='root')}"


def _framework_id(framework: str) -> str:
    return f"framework:{slugify_identifier(framework, fallback='framework')}"


def _fact_evidence(
    discovered_file: DiscoveredFile, metadata: dict[str, Any]
) -> FactEvidence:
    from code_scout.models.facts import FactEvidence

    return FactEvidence(
        file_path=discovered_file.path,
        extractor_name=EXTRACTOR_NAME,
        confidence=1.0,
        start_line=1,
        end_line=_line_count(discovered_file.absolute_path),
        metadata={
            "repository_id": discovered_file.repository_id,
            **metadata,
        },
    )


def _metadata_for_discovered_file(
    discovered_file: DiscoveredFile,
) -> dict[str, Any] | None:
    filename = Path(discovered_file.path).name
    if filename == "package.json":
        package_metadata = parse_package_json_file(discovered_file)
        return None if package_metadata is None else package_metadata.to_metadata()

    language_hints, framework_hints = _file_hints(discovered_file)
    if not _is_package_metadata_file(filename):
        return None

    return {
        "metadata_file": filename,
        "language_hints": list(language_hints),
        "framework_hints": list(framework_hints),
    }


def _file_hints(
    discovered_file: DiscoveredFile,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    filename = Path(discovered_file.path).name
    if filename == "package.json":
        metadata = parse_package_json_file(discovered_file)
        if metadata is None:
            return (), ()
        facts = infer_package_json_facts(metadata)
        return facts.language_hints, facts.framework_hints
    if filename == "tsconfig.json":
        return ("TypeScript",), ()
    if filename == "turbo.json":
        return (), ("turbo",)
    if filename == "nx.json":
        return (), ("nx",)
    framework_hint = _framework_hint_for_config_filename(filename)
    if framework_hint is not None:
        return (), (framework_hint,)
    return (), ()


def _is_package_metadata_file(filename: str) -> bool:
    normalized_filename = filename.lower()
    return (
        normalized_filename in PACKAGE_METADATA_FILENAMES
        or _framework_hint_for_config_filename(normalized_filename) is not None
    )


def _framework_hint_for_config_filename(filename: str) -> str | None:
    normalized_filename = filename.lower()
    exact_hint = FRAMEWORK_CONFIG_FILENAME_HINTS.get(normalized_filename)
    if exact_hint is not None:
        return exact_hint

    for prefix, framework in FRAMEWORK_CONFIG_PREFIX_HINTS.items():
        if normalized_filename.startswith(prefix):
            return framework

    return None
