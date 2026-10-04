from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime

import pytest

from code_scout.core.errors import ConfigError
from code_scout.models.workspace import (
    ExtractorConfig,
    RepositoryConfig,
    Workspace,
    WorkspaceLayout,
    WorkspaceMetadata,
    default_single_repository_workspace,
)
from code_scout.workspace.config import (
    ExtractorConfig as ConfigExtractorConfig,
)
from code_scout.workspace.config import (
    ExtractorEntryConfig,
    OptionalPathConfig,
    WorkspaceConfig,
)
from code_scout.workspace.config import (
    RepositoryConfig as ConfigRepositoryConfig,
)


def test_workspace_models_are_typed_dataclasses() -> None:
    assert is_dataclass(Workspace)
    assert is_dataclass(RepositoryConfig)
    assert is_dataclass(WorkspaceLayout)
    assert is_dataclass(ExtractorConfig)
    assert is_dataclass(WorkspaceMetadata)


def test_default_single_repository_workspace_construction(tmp_path) -> None:
    workspace = default_single_repository_workspace(tmp_path)

    assert workspace.id == "workspace"
    assert workspace.name == "Workspace"
    assert workspace.root_path == str(tmp_path)
    assert workspace.repositories == (
        RepositoryConfig(
            id="workspace",
            display_name="workspace",
            path=".",
            enabled_extractors=(
                "tree_sitter",
                "lsp",
                "graphify",
                "adr",
                "docs",
                "infra",
                "openapi",
                "asyncapi",
                "package_metadata",
                "symbol_extraction",
            ),
        ),
    )
    assert workspace.docs_paths == ()
    assert workspace.adr_paths == ()
    assert workspace.openapi_paths == ()
    assert workspace.asyncapi_paths == ()
    assert workspace.infra_paths == ()
    assert workspace.package_metadata_paths == ()
    assert workspace.exclude_paths == ()
    assert workspace.metadata.source == "code-scout"
    assert workspace.to_dict()["metadata"] == {
        "schema_version": "1",
        "source": "code-scout",
    }


def test_workspace_construction_preserves_config_layout_and_repository_hints(
    tmp_path,
) -> None:
    (tmp_path / "apps" / "frontend").mkdir(parents=True)
    config = WorkspaceConfig(
        id="company-platform",
        name="Company Platform",
        repositories=(
            ConfigRepositoryConfig(
                id="frontend",
                path="apps/frontend",
                name="Frontend",
            ),
        ),
        extractors=ConfigExtractorConfig(
            lsp=ExtractorEntryConfig(enabled=False),
            graphify=ExtractorEntryConfig(enabled=False),
        ),
        paths=OptionalPathConfig(
            docs=("./docs",),
            adr=("./docs/adr",),
            openapi=("./openapi.yaml",),
            asyncapi=("./asyncapi.yaml",),
            infra=("./infra",),
            package_metadata=("pyproject.toml",),
            exclude=("generated", ".idea"),
        ),
    )

    workspace = default_single_repository_workspace(tmp_path, config=config)

    assert workspace.repositories == (
        RepositoryConfig(
            id="frontend",
            display_name="Frontend",
            path="apps/frontend",
            enabled_extractors=(
                "tree_sitter",
                "adr",
                "docs",
                "infra",
                "openapi",
                "asyncapi",
                "package_metadata",
                "symbol_extraction",
            ),
        ),
    )
    assert workspace.layout == WorkspaceLayout(
        docs_paths=("./docs",),
        adr_paths=("./docs/adr",),
        openapi_paths=("./openapi.yaml",),
        asyncapi_paths=("./asyncapi.yaml",),
        infra_paths=("./infra",),
        package_metadata_paths=("pyproject.toml",),
        exclude_paths=("generated", ".idea"),
    )
    assert workspace.extractors == ExtractorConfig(
        enabled=(
            "tree_sitter",
            "adr",
            "docs",
            "infra",
            "openapi",
            "asyncapi",
            "package_metadata",
            "symbol_extraction",
        ),
    )


def test_workspace_validates_duplicate_repository_ids(tmp_path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    config = WorkspaceConfig(
        repositories=(
            ConfigRepositoryConfig(id="app", path="frontend"),
            ConfigRepositoryConfig(id="app", path="backend"),
        ),
    )

    with pytest.raises(ConfigError) as error:
        default_single_repository_workspace(tmp_path, config=config)

    assert "repository id `app`" in error.value.message
    assert "unique repository ids" in (error.value.hint or "")


def test_workspace_rejects_repository_paths_that_escape_root(tmp_path) -> None:
    config = WorkspaceConfig(
        repositories=(ConfigRepositoryConfig(id="outside", path="../outside"),),
    )

    with pytest.raises(ConfigError) as error:
        default_single_repository_workspace(tmp_path, config=config)

    assert "escapes the workspace root" in error.value.message
    assert "../outside" in error.value.message
    assert str(tmp_path.resolve()) in (error.value.hint or "")


def test_workspace_rejects_missing_repository_paths(tmp_path) -> None:
    config = WorkspaceConfig(
        repositories=(ConfigRepositoryConfig(id="missing", path="apps/missing"),),
    )

    with pytest.raises(ConfigError) as error:
        default_single_repository_workspace(tmp_path, config=config)

    assert "path does not exist" in error.value.message
    assert "apps/missing" in error.value.message
    assert ".code-scout/config.yaml" in (error.value.hint or "")


def test_workspace_serialization_includes_generated_at(tmp_path) -> None:
    generated_at = datetime(2026, 6, 20, 9, 30, tzinfo=UTC)
    workspace = default_single_repository_workspace(tmp_path)

    serialized = workspace.to_dict(generated_at=generated_at)

    assert serialized == {
        **asdict(workspace),
        "docs_paths": (),
        "adr_paths": (),
        "openapi_paths": (),
        "asyncapi_paths": (),
        "infra_paths": (),
        "package_metadata_paths": (),
        "exclude_paths": (),
        "generated_at": "2026-06-20T09:30:00Z",
    }
    assert serialized["root_path"] == str(tmp_path)
    assert serialized["repositories"][0]["language_hints"] == ()
    assert serialized["repositories"][0]["framework_hints"] == ()
    assert serialized["repositories"][0]["module_hints"] == ()
