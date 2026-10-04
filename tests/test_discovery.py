from __future__ import annotations

import hashlib

from code_scout.discovery import DiscoveredFile, discover_workspace_files
from code_scout.models.workspace import default_single_repository_workspace
from code_scout.utils.language_map import language_for_extension
from code_scout.workspace.config import OptionalPathConfig, WorkspaceConfig
from code_scout.workspace.config import RepositoryConfig as ConfigRepositoryConfig
from tests.identity_boundaries import RETIRED_STATE_DIRECTORY


def test_file_discovery_walks_configured_repository_paths(tmp_path) -> None:
    frontend = tmp_path / "apps" / "frontend"
    backend = tmp_path / "services" / "backend"
    unconfigured = tmp_path / "unconfigured"
    frontend.mkdir(parents=True)
    backend.mkdir(parents=True)
    unconfigured.mkdir()
    (frontend / "index.ts").write_text("export const app = true;\n", encoding="utf-8")
    (backend / "server.js").write_text("export const app = true;\n", encoding="utf-8")
    (unconfigured / "ignored.ts").write_text("ignored\n", encoding="utf-8")
    config = WorkspaceConfig(
        repositories=(
            ConfigRepositoryConfig(id="frontend", path="apps/frontend"),
            ConfigRepositoryConfig(id="backend", path="services/backend"),
        ),
    )
    workspace = default_single_repository_workspace(tmp_path, config=config)

    files = discover_workspace_files(workspace)

    assert files == (
        DiscoveredFile(
            repository_id="backend",
            path="server.js",
            absolute_path=str((backend / "server.js").resolve()),
            kind="source",
            extension=".js",
            content_hash=_sha256("export const app = true;\n"),
            modified_at=files[0].modified_at,
            language="JavaScript",
        ),
        DiscoveredFile(
            repository_id="frontend",
            path="index.ts",
            absolute_path=str((frontend / "index.ts").resolve()),
            kind="source",
            extension=".ts",
            content_hash=_sha256("export const app = true;\n"),
            modified_at=files[1].modified_at,
            language="TypeScript",
        ),
    )


def test_file_discovery_includes_supported_workspace_files(tmp_path) -> None:
    files_to_create = (
        "CLAUDE.md",
        "Dockerfile",
        "Dockerfile.dev",
        "README.md",
        "compose/docker-compose.yml",
        "config/settings.yaml",
        "config/settings.yml",
        "data/schema.json",
        "js/app.cjs",
        "js/app.js",
        "js/app.jsx",
        "js/app.mjs",
        "package.json",
        "src/app.cts",
        "src/app.mts",
        "src/app.py",
        "src/app.ts",
        "src/app.tsx",
        "turbo.json",
        "workspace/nx.json",
    )
    for path in files_to_create:
        _write_file(tmp_path / path)
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert tuple(file.path for file in files) == tuple(sorted(files_to_create))


def test_file_discovery_classifies_claude_md_as_decision(tmp_path) -> None:
    _write_file(tmp_path / "CLAUDE.md")
    _write_file(tmp_path / "services" / "api" / "CLAUDE.md")
    _write_file(tmp_path / "docs" / "notes.md")
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert {file.path: file.kind for file in files} == {
        "CLAUDE.md": "decision",
        "docs/notes.md": "documentation",
        "services/api/CLAUDE.md": "decision",
    }


def test_file_discovery_includes_representative_target_extensions(tmp_path) -> None:
    files_to_create = (
        "backend/program.cs",
        "backend/service.csproj",
        "frontend/App.vue",
        "game/world.dm",
        "game/world.dmm",
        "scripts/deploy.ps1",
        "src/lib.rs",
        "src/main.go",
        "src/main.py",
        "templates/page.razor",
    )
    for path in files_to_create:
        _write_file(tmp_path / path)
    _write_file(tmp_path / "src/ignored.unknown")
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert tuple(file.path for file in files) == tuple(sorted(files_to_create))


def test_file_discovery_classifies_metadata_extensions(tmp_path) -> None:
    metadata_files = (
        "build.gradle",
        "config/app.json",
        "config/app.yaml",
        "config/app.yml",
        "delphi/Form.dfm",
        "delphi/Form.lfm",
        "delphi/package.lpk",
        "dotnet/App.csproj",
        "dotnet/App.fsproj",
        "dotnet/App.vbproj",
        "dotnet/App.sln",
        "dotnet/App.slnx",
        "game/icon.dmi",
        "game/interface.dmf",
        "game/map.dmm",
    )
    for path in metadata_files:
        _write_file(tmp_path / path)
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert {file.path: file.kind for file in files} == {
        path: "metadata" for path in metadata_files
    }


def test_file_discovery_keeps_source_extensions_as_source(tmp_path) -> None:
    source_files = (
        "frontend/App.astro",
        "frontend/App.svelte",
        "frontend/App.vue",
        "game/world.dm",
        "game/world.dme",
        "pages/index.cshtml",
        "scripts/deploy.ps1",
        "scripts/module.psm1",
        "scripts/query.sql",
        "scripts/run.bash",
        "scripts/run.sh",
        "templates/page.razor",
    )
    for path in source_files:
        _write_file(tmp_path / path)
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert {file.path: file.kind for file in files} == {
        path: "source" for path in source_files
    }


def test_file_discovery_populates_language_extension_and_kind(tmp_path) -> None:
    files_to_create = (
        "backend/program.cs",
        "config/app.json",
        "config/app.yaml",
        "dotnet/App.csproj",
        "dotnet/App.sln",
        "src/lib.rs",
        "src/main.go",
        "src/main.py",
    )
    for path in files_to_create:
        _write_file(tmp_path / path)
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert {
        file.path: (file.language, file.extension, file.kind) for file in files
    } == {
        "backend/program.cs": ("C#", ".cs", "source"),
        "config/app.json": (language_for_extension(".json"), ".json", "metadata"),
        "config/app.yaml": (language_for_extension(".yaml"), ".yaml", "metadata"),
        "dotnet/App.csproj": (
            language_for_extension(".csproj"),
            ".csproj",
            "metadata",
        ),
        "dotnet/App.sln": (language_for_extension(".sln"), ".sln", "metadata"),
        "src/lib.rs": ("Rust", ".rs", "source"),
        "src/main.go": ("Go", ".go", "source"),
        "src/main.py": ("Python", ".py", "source"),
    }


def test_file_discovery_records_file_metadata(tmp_path) -> None:
    readme = tmp_path / "README.md"
    _write_file(readme, content="# code-scout\n")
    package_json = tmp_path / "package.json"
    _write_file(package_json, content='{"name": "code-scout"}\n')
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert files[0] == DiscoveredFile(
        repository_id="workspace",
        path="README.md",
        absolute_path=str(readme.resolve()),
        kind="documentation",
        extension=".md",
        content_hash=_sha256("# code-scout\n"),
        modified_at=files[0].modified_at,
        language="Markdown",
    )
    assert files[0].modified_at.endswith("Z")
    assert files[1] == DiscoveredFile(
        repository_id="workspace",
        path="package.json",
        absolute_path=str(package_json.resolve()),
        kind="metadata",
        extension=".json",
        content_hash=_sha256('{"name": "code-scout"}\n'),
        modified_at=files[1].modified_at,
        language="JSON",
    )


def test_file_discovery_classifies_default_adr_paths_as_decisions(tmp_path) -> None:
    _write_file(tmp_path / "README.md")
    _write_file(tmp_path / "docs" / "adr" / "0001-use-sqlite.md")
    _write_file(tmp_path / "adr" / "0002-use-uv.md")
    _write_file(tmp_path / "architecture" / "decisions" / "0003-use-ruff.mdx")
    _write_file(tmp_path / "apps" / "backend" / "docs" / "adr" / "0004-use-api.md")
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert {file.path: file.kind for file in files} == {
        "README.md": "documentation",
        "adr/0002-use-uv.md": "decision",
        "apps/backend/docs/adr/0004-use-api.md": "decision",
        "architecture/decisions/0003-use-ruff.mdx": "decision",
        "docs/adr/0001-use-sqlite.md": "decision",
    }


def test_file_discovery_classifies_configured_adr_paths_as_decisions(tmp_path) -> None:
    _write_file(tmp_path / "docs" / "notes.md")
    _write_file(tmp_path / "records" / "decisions" / "0004-use-stdlib.md")
    config = WorkspaceConfig(
        paths=OptionalPathConfig(adr=("./records/decisions",)),
    )
    workspace = default_single_repository_workspace(tmp_path, config=config)

    files = discover_workspace_files(workspace)

    assert {file.path: file.kind for file in files} == {
        "docs/notes.md": "documentation",
        "records/decisions/0004-use-stdlib.md": "decision",
    }


def test_file_discovery_excludes_generated_and_cache_directories(tmp_path) -> None:
    included = tmp_path / "src" / "app.ts"
    _write_file(included)
    for directory_name in (
        ".git",
        RETIRED_STATE_DIRECTORY,
        ".code-scout",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "artifacts",
        "bin",
        "build",
        "coverage",
        "debug",
        "dist",
        "env",
        "node_modules",
        "obj",
        "out",
        "outputs",
        "packages",
        "publish",
        "release",
        ".some_name",
        "target",
        "tmp-cache",
        "venv",
        "virtualenv",
    ):
        _write_file(tmp_path / directory_name / "ignored.ts")
    workspace = default_single_repository_workspace(tmp_path)

    files = discover_workspace_files(workspace)

    assert tuple(file.path for file in files) == ("src/app.ts",)


def test_file_discovery_excludes_configured_paths(tmp_path) -> None:
    _write_file(tmp_path / "src" / "app.ts")
    _write_file(tmp_path / "generated" / "client.ts")
    _write_file(tmp_path / "vendor" / "api" / "schema.yaml")
    _write_file(tmp_path / "src" / "generated.ts")
    config = WorkspaceConfig(
        paths=OptionalPathConfig(exclude=("generated", "vendor/api/schema.yaml")),
    )
    workspace = default_single_repository_workspace(tmp_path, config=config)

    files = discover_workspace_files(workspace)

    assert tuple(file.path for file in files) == ("src/app.ts", "src/generated.ts")


def test_scan_counts_discovered_files(tmp_path) -> None:
    from datetime import UTC, datetime

    from code_scout.pipeline.scan import scan_workspace
    from code_scout.workspace.config import create_workspace_config

    create_workspace_config(tmp_path)
    _write_file(tmp_path / "src" / "app.ts")
    _write_file(tmp_path / ".code-scout" / "ignored.ts")

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    assert result.counts.files == 1


def _write_file(path, *, content: str = "content\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
