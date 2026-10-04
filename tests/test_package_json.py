from __future__ import annotations

from hashlib import sha256

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.package_json import (
    PackageJsonEntry,
    PackageJsonMetadata,
    RepositoryPackageFacts,
    infer_repository_package_facts,
    package_json_edges,
    package_metadata_active_facts,
    parse_package_json_file,
)


def test_parse_package_json_extracts_basic_metadata(tmp_path) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text(
        """
        {
          "name": "@acme/web",
          "private": true,
          "scripts": {
            "dev": "vite --host 0.0.0.0",
            "test": "vitest run"
          },
          "dependencies": {
            "@vitejs/plugin-react": "latest",
            "react": "^19.0.0"
          },
          "devDependencies": {
            "typescript": "^5.8.0",
            "vitest": "^3.0.0"
          }
        }
        """,
        encoding="utf-8",
    )

    metadata = parse_package_json_file(_discovered_package_file(package_file))

    assert metadata == PackageJsonMetadata(
        name="@acme/web",
        private=True,
        scripts=(
            PackageJsonEntry(name="dev", value="vite --host 0.0.0.0"),
            PackageJsonEntry(name="test", value="vitest run"),
        ),
        dependencies=(
            PackageJsonEntry(name="@vitejs/plugin-react", value="latest"),
            PackageJsonEntry(name="react", value="^19.0.0"),
        ),
        dev_dependencies=(
            PackageJsonEntry(name="typescript", value="^5.8.0"),
            PackageJsonEntry(name="vitest", value="^3.0.0"),
        ),
    )


def test_package_json_edges_attach_metadata_as_repository_fact_evidence(
    tmp_path,
) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text(
        '{"name":"code-scout-ui","scripts":{"build":"vite build"},"private":false}\n',
        encoding="utf-8",
    )

    edges = package_json_edges((_discovered_package_file(package_file),))

    assert len(edges) == 1
    assert edges[0].source_type == "repository"
    assert edges[0].source_id == "workspace"
    assert edges[0].target_type == "file"
    assert edges[0].target_id == "file:workspace:package.json"
    assert edges[0].relation_type == "declares_package_metadata"
    assert edges[0].evidence[0].metadata == {
        "name": "code-scout-ui",
        "scripts": {"build": "vite build"},
        "dependencies": {},
        "dev_dependencies": {},
        "workspaces": [],
        "private": False,
        "language_hints": [],
        "framework_hints": [],
    }


def test_package_metadata_edges_include_workspace_metadata_files(tmp_path) -> None:
    file = tmp_path / "turbo.json"
    file.write_text('{"tasks": {}}\n', encoding="utf-8")

    edges = package_json_edges((_discovered_package_file(file, path="turbo.json"),))

    assert len(edges) == 1
    assert edges[0].target_id == "file:workspace:turbo.json"
    assert edges[0].evidence[0].metadata == {
        "metadata_file": "turbo.json",
        "language_hints": [],
        "framework_hints": ["turbo"],
    }


def test_parse_package_json_ignores_non_package_files(tmp_path) -> None:
    file = tmp_path / "turbo.json"
    file.write_text('{"tasks": {}}\n', encoding="utf-8")

    assert (
        parse_package_json_file(_discovered_package_file(file, path="turbo.json"))
        is None
    )


def test_package_metadata_active_facts_emit_package_entity_and_relations(
    tmp_path,
) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text(
        """
        {
          "name": "@acme/web",
          "private": true,
          "workspaces": ["apps/*", "packages/*"],
          "scripts": {"dev": "next dev"},
          "dependencies": {"next": "^15.0.0"},
          "devDependencies": {"typescript": "^5.8.0"}
        }
        """,
        encoding="utf-8",
    )

    facts = package_metadata_active_facts((_discovered_package_file(package_file),))
    facts_by_kind = {fact.kind: fact for fact in facts}

    package_fact = facts_by_kind["package"]
    assert package_fact.extractor == "package_json"
    assert package_fact.source_file == "package.json"
    assert package_fact.identity == {
        "entity_type": "package",
        "entity_id": "package:workspace:acme-web",
    }
    assert package_fact.properties == {
        "repository_id": "workspace",
        "path": "package.json",
        "name": "@acme/web",
        "private": True,
        "scripts": {"dev": "next dev"},
        "dependencies": {"next": "^15.0.0"},
        "dev_dependencies": {"typescript": "^5.8.0"},
        "workspaces": ("apps/*", "packages/*"),
        "language_hints": ("TypeScript",),
        "framework_hints": ("Next.js",),
    }
    assert package_fact.evidence[0].metadata["repository_id"] == "workspace"
    assert package_fact.evidence[0].metadata["name"] == "@acme/web"
    assert package_fact.evidence[0].metadata["workspaces"] == [
        "apps/*",
        "packages/*",
    ]

    relation_identities = {
        fact.kind: fact.identity for fact in facts if fact.kind != "package"
    }
    assert relation_identities["declares_package_metadata"] == {
        "subject": {"type": "repository", "id": "workspace"},
        "relation": "declares_package_metadata",
        "object": {"type": "file", "id": "file:workspace:package.json"},
    }
    assert relation_identities["uses_framework"] == {
        "subject": {"type": "package", "id": "package:workspace:acme-web"},
        "relation": "uses_framework",
        "object": {"type": "framework", "id": "framework:next-js"},
    }
    workspace_relations = [
        fact.identity for fact in facts if fact.kind == "declares_workspace"
    ]
    assert workspace_relations == [
        {
            "subject": {"type": "package", "id": "package:workspace:acme-web"},
            "relation": "declares_workspace",
            "object": {"type": "workspace", "id": "workspace:workspace:apps"},
        },
        {
            "subject": {"type": "package", "id": "package:workspace:acme-web"},
            "relation": "declares_workspace",
            "object": {"type": "workspace", "id": "workspace:workspace:packages"},
        },
    ]


def test_package_metadata_active_facts_emit_config_framework_relation(
    tmp_path,
) -> None:
    config_file = tmp_path / "turbo.json"
    config_file.write_text('{"tasks": {}}\n', encoding="utf-8")

    facts = package_metadata_active_facts(
        (_discovered_package_file(config_file, path="turbo.json"),)
    )

    assert [fact.kind for fact in facts] == [
        "declares_package_metadata",
        "uses_framework",
    ]
    framework_fact = facts[1]
    assert framework_fact.identity == {
        "subject": {"type": "repository", "id": "workspace"},
        "relation": "uses_framework",
        "object": {"type": "framework", "id": "framework:turbo"},
    }
    assert framework_fact.evidence[0].metadata == {
        "repository_id": "workspace",
        "metadata_file": "turbo.json",
        "language_hints": [],
        "framework_hints": ["turbo"],
    }


def test_infer_repository_package_facts_detects_nextjs_and_typescript(
    tmp_path,
) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text(
        """
        {
          "dependencies": {
            "next": "^15.0.0",
            "react": "^19.0.0"
          },
          "devDependencies": {
            "typescript": "^5.8.0"
          }
        }
        """,
        encoding="utf-8",
    )

    facts = infer_repository_package_facts((_discovered_package_file(package_file),))

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            language_hints=("TypeScript",),
            framework_hints=("Next.js",),
            package_files_analyzed=1,
        ),
    )


def test_infer_repository_package_facts_detects_nextjs_config(tmp_path) -> None:
    config_file = tmp_path / "next.config.mjs"
    config_file.write_text("export default {}\n", encoding="utf-8")

    facts = infer_repository_package_facts(
        (_discovered_package_file(config_file, path="next.config.mjs"),)
    )

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            framework_hints=("Next.js",),
            package_files_analyzed=1,
        ),
    )


def test_infer_repository_package_facts_detects_angular_config(tmp_path) -> None:
    config_file = tmp_path / "angular.json"
    config_file.write_text('{"version":1,"projects":{}}\n', encoding="utf-8")

    facts = infer_repository_package_facts(
        (_discovered_package_file(config_file, path="angular.json"),)
    )

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            framework_hints=("Angular",),
            package_files_analyzed=1,
        ),
    )


def test_infer_repository_package_facts_dedupes_package_and_config_hints(
    tmp_path,
) -> None:
    package_file = tmp_path / "package.json"
    config_file = tmp_path / "next.config.ts"
    package_file.write_text('{"dependencies":{"next":"^15.0.0"}}\n', encoding="utf-8")
    config_file.write_text(
        "const config = {}\nexport default config\n", encoding="utf-8"
    )

    facts = infer_repository_package_facts(
        (
            _discovered_package_file(package_file),
            _discovered_package_file(config_file, path="next.config.ts"),
        )
    )

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            framework_hints=("Next.js",),
            package_files_analyzed=2,
        ),
    )


def test_infer_repository_package_facts_keeps_package_only_typescript_behavior(
    tmp_path,
) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text(
        '{"devDependencies":{"typescript":"^5.8.0"}}\n',
        encoding="utf-8",
    )

    facts = infer_repository_package_facts((_discovered_package_file(package_file),))

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            language_hints=("TypeScript",),
            package_files_analyzed=1,
        ),
    )


def test_infer_repository_package_facts_detects_nestjs_dependency(tmp_path) -> None:
    package_file = tmp_path / "package.json"
    package_file.write_text(
        '{"dependencies":{"@nestjs/core":"^11.0.0"}}\n',
        encoding="utf-8",
    )

    facts = infer_repository_package_facts((_discovered_package_file(package_file),))

    assert facts[0].framework_hints == ("NestJS",)


def test_infer_repository_package_facts_detects_turbo_metadata(tmp_path) -> None:
    package_file = tmp_path / "package.json"
    turbo_file = tmp_path / "turbo.json"
    package_file.write_text('{"scripts":{"build":"turbo build"}}\n', encoding="utf-8")
    turbo_file.write_text('{"tasks":{"build":{}}}\n', encoding="utf-8")

    facts = infer_repository_package_facts(
        (
            _discovered_package_file(package_file),
            _discovered_package_file(turbo_file, path="turbo.json"),
        )
    )

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            framework_hints=("turbo",),
            package_files_analyzed=2,
        ),
    )


def test_infer_repository_package_facts_detects_nx_metadata(tmp_path) -> None:
    nx_file = tmp_path / "nx.json"
    tsconfig_file = tmp_path / "tsconfig.json"
    nx_file.write_text('{"targetDefaults":{}}\n', encoding="utf-8")
    tsconfig_file.write_text('{"compilerOptions":{}}\n', encoding="utf-8")

    facts = infer_repository_package_facts(
        (
            _discovered_package_file(nx_file, path="nx.json"),
            _discovered_package_file(tsconfig_file, path="tsconfig.json"),
        )
    )

    assert facts == (
        RepositoryPackageFacts(
            repository_id="workspace",
            language_hints=("TypeScript",),
            framework_hints=("nx",),
            package_files_analyzed=2,
        ),
    )


def _discovered_package_file(
    package_file,
    *,
    path: str = "package.json",
) -> DiscoveredFile:
    content = package_file.read_bytes()
    return DiscoveredFile(
        repository_id="workspace",
        path=path,
        absolute_path=str(package_file),
        kind="metadata",
        extension=".json",
        content_hash=sha256(content).hexdigest(),
        modified_at="2026-06-20T10:00:00Z",
        language="JSON",
    )
