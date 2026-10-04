from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_scout.core.errors import ConfigError
from code_scout.workspace.config import ExtractorConfig as ConfigExtractorConfig
from code_scout.workspace.config import WorkspaceConfig

DEFAULT_EXTRACTORS = (
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
)


@dataclass(frozen=True)
class ExtractorConfig:
    enabled: tuple[str, ...] = DEFAULT_EXTRACTORS


@dataclass(frozen=True)
class WorkspaceLayout:
    docs_paths: tuple[str, ...] = ()
    adr_paths: tuple[str, ...] = ()
    openapi_paths: tuple[str, ...] = ()
    asyncapi_paths: tuple[str, ...] = ()
    infra_paths: tuple[str, ...] = ()
    package_metadata_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryConfig:
    id: str
    display_name: str
    path: str
    language_hints: tuple[str, ...] = ()
    framework_hints: tuple[str, ...] = ()
    module_hints: tuple[str, ...] = ()
    enabled_extractors: tuple[str, ...] = DEFAULT_EXTRACTORS


@dataclass(frozen=True)
class WorkspaceMetadata:
    schema_version: str = "1"
    source: str = "code-scout"


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    root_path: str
    repositories: tuple[RepositoryConfig, ...]
    layout: WorkspaceLayout = field(default_factory=WorkspaceLayout)
    extractors: ExtractorConfig = field(default_factory=ExtractorConfig)
    metadata: WorkspaceMetadata = field(default_factory=WorkspaceMetadata)

    @property
    def docs_paths(self) -> tuple[str, ...]:
        return self.layout.docs_paths

    @property
    def adr_paths(self) -> tuple[str, ...]:
        return self.layout.adr_paths

    @property
    def openapi_paths(self) -> tuple[str, ...]:
        return self.layout.openapi_paths

    @property
    def asyncapi_paths(self) -> tuple[str, ...]:
        return self.layout.asyncapi_paths

    @property
    def infra_paths(self) -> tuple[str, ...]:
        return self.layout.infra_paths

    @property
    def package_metadata_paths(self) -> tuple[str, ...]:
        return self.layout.package_metadata_paths

    @property
    def exclude_paths(self) -> tuple[str, ...]:
        return self.layout.exclude_paths

    def to_dict(self, *, generated_at: datetime | None = None) -> dict[str, Any]:
        serialized = asdict(self)
        serialized["docs_paths"] = self.docs_paths
        serialized["adr_paths"] = self.adr_paths
        serialized["openapi_paths"] = self.openapi_paths
        serialized["asyncapi_paths"] = self.asyncapi_paths
        serialized["infra_paths"] = self.infra_paths
        serialized["package_metadata_paths"] = self.package_metadata_paths
        serialized["exclude_paths"] = self.exclude_paths
        serialized["generated_at"] = _serialize_generated_at(generated_at)
        return serialized


def default_single_repository_workspace(
    root_path: str | Path = ".",
    *,
    config: WorkspaceConfig | None = None,
) -> Workspace:
    config = WorkspaceConfig() if config is None else config
    root = str(root_path)
    enabled_extractors = _enabled_extractors_from_config(config.extractors)
    repositories = tuple(
        RepositoryConfig(
            id=repository.id,
            display_name=repository.name or repository.id,
            path=repository.path,
            enabled_extractors=enabled_extractors,
        )
        for repository in config.repositories
    )
    _validate_workspace_registry(root_path, repositories)

    return Workspace(
        id=config.id,
        name=config.name,
        root_path=root,
        repositories=repositories,
        layout=WorkspaceLayout(
            docs_paths=config.paths.docs,
            adr_paths=config.paths.adr,
            openapi_paths=config.paths.openapi,
            asyncapi_paths=config.paths.asyncapi,
            infra_paths=config.paths.infra,
            package_metadata_paths=config.paths.package_metadata,
            exclude_paths=config.paths.exclude,
        ),
        extractors=ExtractorConfig(enabled=enabled_extractors),
    )


def _enabled_extractors_from_config(
    extractors: ConfigExtractorConfig,
) -> tuple[str, ...]:
    return tuple(
        name for name in DEFAULT_EXTRACTORS if getattr(extractors, name).enabled
    )


def _validate_workspace_registry(
    root_path: str | Path,
    repositories: tuple[RepositoryConfig, ...],
) -> None:
    duplicate_id = _first_duplicate_id(repositories)
    if duplicate_id is not None:
        raise ConfigError(
            f"Workspace repository id `{duplicate_id}` is configured more than once.",
            "Use unique repository ids so scan output can be attributed unambiguously.",
        )

    root = Path(root_path)
    try:
        resolved_root = root.resolve(strict=True)
    except FileNotFoundError as error:
        raise ConfigError(
            f"Workspace root does not exist: {root}.",
            "Create the workspace directory or pass `--workspace` "
            "with an existing path.",
        ) from error

    for repository in repositories:
        configured_path = Path(repository.path)
        candidate = (
            configured_path
            if configured_path.is_absolute()
            else resolved_root / configured_path
        )
        resolved_candidate = candidate.resolve(strict=False)

        try:
            resolved_candidate.relative_to(resolved_root)
        except ValueError as error:
            raise ConfigError(
                f"Repository `{repository.id}` path escapes the workspace root: "
                f"{repository.path}.",
                f"Keep repository paths under {resolved_root}.",
            ) from error

        if not resolved_candidate.exists():
            raise ConfigError(
                f"Repository `{repository.id}` path does not exist: {repository.path}.",
                "Create the repository directory or update `.code-scout/config.yaml` "
                "to point at an existing path.",
            )


def _first_duplicate_id(
    repositories: tuple[RepositoryConfig, ...],
) -> str | None:
    seen: set[str] = set()
    for repository in repositories:
        if repository.id in seen:
            return repository.id
        seen.add(repository.id)
    return None


def _serialize_generated_at(generated_at: datetime | None) -> str:
    value = datetime.now(UTC) if generated_at is None else generated_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
