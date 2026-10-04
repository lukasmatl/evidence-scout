from __future__ import annotations

from dataclasses import asdict, fields, is_dataclass

import pytest

from code_scout.core.errors import ConfigError
from code_scout.utils.identifiers import slugify_identifier
from code_scout.workspace.config import (
    DEFAULT_CONFIG_PATH,
    ExtractorConfig,
    ExtractorWatchConfig,
    OptionalPathConfig,
    RepositoryConfig,
    WorkspaceConfig,
    create_workspace_config,
    load_workspace_config,
)


def test_config_models_are_typed_dataclasses() -> None:
    assert is_dataclass(WorkspaceConfig)
    assert is_dataclass(RepositoryConfig)
    assert is_dataclass(ExtractorConfig)
    assert is_dataclass(ExtractorWatchConfig)
    assert is_dataclass(OptionalPathConfig)


def test_default_workspace_config_values() -> None:
    config = WorkspaceConfig()

    assert DEFAULT_CONFIG_PATH == ".code-scout/config.yaml"
    assert config.id == "workspace"
    assert config.name == "Workspace"
    assert config.repositories == (RepositoryConfig(),)
    assert config.repositories[0].id == "workspace"
    assert config.repositories[0].path == "."
    assert config.repositories[0].name is None


def test_slugify_identifier_is_deterministic() -> None:
    assert slugify_identifier("Company Platform", fallback="workspace") == (
        "company-platform"
    )
    assert slugify_identifier("  API & Billing_Service  ", fallback="repository") == (
        "api-billing-service"
    )
    assert slugify_identifier("!!!", fallback="repository") == "repository"


def test_default_extractor_config_includes_mvp_extractors() -> None:
    extractors = ExtractorConfig()

    assert [field.name for field in fields(extractors)] == [
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
    ]
    assert all(
        entry
        == {
            "enabled": True,
            "command": (),
            "connect": None,
            "json_path": None,
            "watch": None,
        }
        for entry in asdict(extractors).values()
    )


def test_create_workspace_config_serializes_symbol_extraction(tmp_path) -> None:
    config_path = create_workspace_config(tmp_path)
    config_text = config_path.read_text(encoding="utf-8")

    assert "  symbol_extraction:\n    enabled: true\n" in config_text
    assert "  docs:\n    enabled: true\n" in config_text
    assert "outputs:" not in config_text
    assert "paths:\n  exclude: []\n" in config_text


def test_default_optional_paths_are_empty() -> None:
    paths = OptionalPathConfig()

    assert asdict(paths) == {
        "docs": (),
        "adr": (),
        "openapi": (),
        "asyncapi": (),
        "infra": (),
        "package_metadata": (),
        "exclude": (),
    }


def test_load_workspace_config_reads_selected_workspace_config(
    tmp_path,
) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: company-platform
  name: Company Platform
repositories:
  - id: frontend
    path: ./apps/frontend
    name: Frontend
  - id: backend
    path: ./apps/backend
extractors:
  tree_sitter: false
  lsp:
    enabled: true
    command:
      - custom-typescript-language-server
      - --stdio
  graphify:
    enabled: true
    json_path: .code-scout/graphify.json
  symbol_extraction: false
paths:
  docs:
    - ./docs
  adr:
    - ./docs/adr
  asyncapi:
    - ./contracts/events.yaml
  exclude:
    - generated
    - .private
""",
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.id == "company-platform"
    assert config.name == "Company Platform"
    assert config.repositories == (
        RepositoryConfig(
            id="frontend",
            path="./apps/frontend",
            name="Frontend",
        ),
        RepositoryConfig(id="backend", path="./apps/backend"),
    )
    assert config.extractors.tree_sitter.enabled is False
    assert config.extractors.lsp.enabled is True
    assert config.extractors.lsp.command == (
        "custom-typescript-language-server",
        "--stdio",
    )
    assert config.extractors.graphify.enabled is True
    assert config.extractors.graphify.json_path == ".code-scout/graphify.json"
    assert config.extractors.symbol_extraction.enabled is False
    assert config.paths.docs == ("./docs",)
    assert config.paths.adr == ("./docs/adr",)
    assert config.paths.asyncapi == ("./contracts/events.yaml",)
    assert config.paths.exclude == ("generated", ".private")


def test_load_workspace_config_reads_graphify_watch_config(tmp_path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: .
extractors:
  graphify:
    enabled: true
    command:
      - graphify
      - scan
      - --json
    json_path: .code-scout/graphify.json
    watch:
      command:
        - graphify
        - watch
        - --jsonl
      output_path: .code-scout/graphify-watch.jsonl
""",
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.extractors.graphify.command == ("graphify", "scan", "--json")
    assert config.extractors.graphify.json_path == ".code-scout/graphify.json"
    assert config.extractors.graphify.watch == ExtractorWatchConfig(
        command=("graphify", "watch", "--jsonl"),
        output_path=".code-scout/graphify-watch.jsonl",
    )


def test_load_workspace_config_accepts_graphify_watch_output_only(tmp_path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: .
extractors:
  graphify:
    watch:
      output_path: .code-scout/graphify-watch.jsonl
""",
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.extractors.graphify.watch == ExtractorWatchConfig(
        output_path=".code-scout/graphify-watch.jsonl",
    )


def test_load_workspace_config_keeps_legacy_graphify_config(tmp_path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: .
extractors:
  graphify:
    enabled: true
    command:
      - graphify
      - scan
    json_path: .code-scout/graphify.json
""",
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.extractors.graphify.command == ("graphify", "scan")
    assert config.extractors.graphify.json_path == ".code-scout/graphify.json"
    assert config.extractors.graphify.watch is None


def test_load_workspace_config_defaults_omitted_symbol_extraction(tmp_path) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: ./apps/frontend
extractors:
  tree_sitter: false
""",
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.extractors.tree_sitter.enabled is False
    assert config.extractors.symbol_extraction.enabled is True


def test_load_workspace_config_generates_missing_ids_from_slugs(
    tmp_path,
) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: ./apps/frontend
    name: Web Frontend
  - path: ./apps/api-service
""",
        encoding="utf-8",
    )

    config = load_workspace_config(tmp_path)

    assert config.id == "company-platform"
    assert config.repositories == (
        RepositoryConfig(
            id="web-frontend",
            path="./apps/frontend",
            name="Web Frontend",
        ),
        RepositoryConfig(id="api-service", path="./apps/api-service"),
    )


def test_load_workspace_config_missing_config_returns_actionable_error(
    tmp_path,
) -> None:
    with pytest.raises(ConfigError) as error:
        load_workspace_config(tmp_path)

    assert "code-scout config not found" in error.value.message
    assert ".code-scout/config.yaml" in error.value.message
    assert "code-scout init --workspace" in (error.value.hint or "")


def test_load_workspace_config_invalid_yaml_returns_actionable_error(
    tmp_path,
) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace: [
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_workspace_config(tmp_path)

    assert "is not valid YAML" in error.value.message
    assert "line 2" in (error.value.hint or "")


def test_load_workspace_config_invalid_schema_returns_actionable_error(
    tmp_path,
) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: ./apps/frontend
extractors:
  tree_sitter:
    enabled: sometimes
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_workspace_config(tmp_path)

    assert "has an invalid schema" in error.value.message
    assert "`extractors.tree_sitter.enabled` must be a boolean" in (
        error.value.hint or ""
    )


def test_load_workspace_config_invalid_graphify_watch_returns_actionable_error(
    tmp_path,
) -> None:
    config_path = tmp_path / DEFAULT_CONFIG_PATH
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  name: Company Platform
repositories:
  - path: .
extractors:
  graphify:
    watch:
      command: graphify watch
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as error:
        load_workspace_config(tmp_path)

    assert "has an invalid schema" in error.value.message
    assert "`extractors.graphify.watch.command` must be a list of strings" in (
        error.value.hint or ""
    )
