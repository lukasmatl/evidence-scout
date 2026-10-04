from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_scout.core.errors import ConfigError
from code_scout.utils.identifiers import slugify_identifier
from code_scout.workspace.paths import CONFIG_PATH

DEFAULT_CONFIG_PATH = CONFIG_PATH
_EXTRACTOR_NAMES = (
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
_PATH_NAMES = (
    "docs",
    "adr",
    "openapi",
    "asyncapi",
    "infra",
    "package_metadata",
    "exclude",
)


@dataclass(frozen=True)
class SourceRetentionConfig:
    """Bounded local source retention used for revision-bound evidence."""

    max_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class ExtractorWatchConfig:
    command: tuple[str, ...] = ()
    output_path: str | None = None


@dataclass(frozen=True)
class ExtractorEntryConfig:
    enabled: bool = True
    command: tuple[str, ...] = ()
    connect: str | None = None
    json_path: str | None = None
    watch: ExtractorWatchConfig | None = None


@dataclass(frozen=True)
class ExtractorConfig:
    tree_sitter: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    lsp: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    graphify: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    adr: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    docs: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    infra: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    openapi: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    asyncapi: ExtractorEntryConfig = field(default_factory=ExtractorEntryConfig)
    package_metadata: ExtractorEntryConfig = field(
        default_factory=ExtractorEntryConfig,
    )
    symbol_extraction: ExtractorEntryConfig = field(
        default_factory=ExtractorEntryConfig,
    )


@dataclass(frozen=True)
class OptionalPathConfig:
    docs: tuple[str, ...] = ()
    adr: tuple[str, ...] = ()
    openapi: tuple[str, ...] = ()
    asyncapi: tuple[str, ...] = ()
    infra: tuple[str, ...] = ()
    package_metadata: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositoryConfig:
    id: str = "workspace"
    path: str = "."
    name: str | None = None


@dataclass(frozen=True)
class SerenaNavigationConfig:
    enabled: bool = False
    command: tuple[str, ...] = ()
    url: str | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class NavigationConfig:
    serena: SerenaNavigationConfig = field(default_factory=SerenaNavigationConfig)


@dataclass(frozen=True)
class DedeEnrichmentConfig:
    enabled: bool = False
    graph_path: str = ".code-scout/dede-graph.json"
    executable: str = "dede"
    max_age_seconds: float = 86_400.0


@dataclass(frozen=True)
class EnrichmentConfig:
    dede: DedeEnrichmentConfig = field(default_factory=DedeEnrichmentConfig)


@dataclass(frozen=True)
class WorkspaceConfig:
    id: str = "workspace"
    name: str = "Workspace"
    repositories: tuple[RepositoryConfig, ...] = field(
        default_factory=lambda: (RepositoryConfig(),),
    )
    extractors: ExtractorConfig = field(default_factory=ExtractorConfig)
    paths: OptionalPathConfig = field(default_factory=OptionalPathConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    enrichment: EnrichmentConfig = field(default_factory=EnrichmentConfig)
    source_retention: SourceRetentionConfig = field(
        default_factory=SourceRetentionConfig
    )


def create_workspace_config(
    workspace: str | Path,
    *,
    force: bool = False,
) -> Path:
    workspace_path = Path(workspace)
    config_path = workspace_path / DEFAULT_CONFIG_PATH

    if config_path.exists() and not force:
        raise ConfigError(
            f"code-scout config already exists at {config_path}.",
            "Pass `--force` to overwrite it.",
        )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_default_config_yaml(), encoding="utf-8")
    return config_path


def load_workspace_config(workspace: str | Path) -> WorkspaceConfig:
    workspace_path = Path(workspace)
    config_path = workspace_path / DEFAULT_CONFIG_PATH

    try:
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError(
            f"code-scout config not found at {config_path}.",
            f"Run `code-scout init --workspace {workspace_path}` to create it.",
        ) from error

    try:
        raw_config = _parse_yaml_subset(config_text)
    except _YAMLError as error:
        raise ConfigError(
            f"code-scout config at {config_path} is not valid YAML.",
            f"Fix the YAML syntax near line {error.line}: {error.message}.",
        ) from error

    try:
        return _workspace_config_from_mapping(raw_config)
    except _SchemaError as error:
        raise ConfigError(
            f"code-scout config at {config_path} has an invalid schema.",
            error.message,
        ) from error


def _default_config_yaml() -> str:
    config = WorkspaceConfig()

    lines = [
        "workspace:",
        f"  id: {config.id}",
        f"  name: {config.name}",
        "repositories:",
    ]

    for repository in config.repositories:
        lines.extend(
            [
                f"  - id: {repository.id}",
                f"    path: {repository.path}",
            ],
        )
        if repository.name is not None:
            lines.append(f"    name: {repository.name}")

    lines.append("extractors:")
    for name in _EXTRACTOR_NAMES:
        entry = getattr(config.extractors, name)
        lines.extend(
            [
                f"  {name}:",
                f"    enabled: {_yaml_bool(entry.enabled)}",
            ],
        )

    lines.extend(
        [
            "paths:",
            "  exclude: []",
            "source_retention:",
            f"  max_bytes: {config.source_retention.max_bytes}",
        ],
    )

    return "\n".join(lines) + "\n"


def _yaml_bool(value: bool) -> str:
    return "true" if value else "false"


class _YAMLError(Exception):
    def __init__(self, line: int, message: str) -> None:
        self.line = line
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class _YAMLLine:
    number: int
    indent: int
    text: str


class _SchemaError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _parse_yaml_subset(config_text: str) -> dict[str, Any]:
    lines = _yaml_lines(config_text)
    if not lines:
        raise _YAMLError(1, "configuration is empty")

    value, index = _parse_yaml_block(lines, 0, lines[0].indent)
    if index != len(lines):
        line = lines[index]
        raise _YAMLError(line.number, "unexpected content")
    if not isinstance(value, dict):
        raise _YAMLError(lines[0].number, "top-level config must be a mapping")
    return value


def _yaml_lines(config_text: str) -> list[_YAMLLine]:
    yaml_lines: list[_YAMLLine] = []

    for number, raw_line in enumerate(config_text.splitlines(), start=1):
        content = raw_line.split("#", 1)[0].rstrip()
        if not content.strip():
            continue

        indent = len(content) - len(content.lstrip(" "))
        if "\t" in raw_line[:indent]:
            raise _YAMLError(number, "tabs are not supported for indentation")

        yaml_lines.append(_YAMLLine(number, indent, content.lstrip(" ")))

    return yaml_lines


def _parse_yaml_block(
    lines: list[_YAMLLine],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    line = lines[index]
    if line.indent != indent:
        raise _YAMLError(line.number, "unexpected indentation")

    if line.text.startswith("- "):
        return _parse_yaml_list(lines, index, indent)

    return _parse_yaml_mapping(lines, index, indent)


def _parse_yaml_mapping(
    lines: list[_YAMLLine],
    index: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}

    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _YAMLError(line.number, "unexpected indentation")
        if line.text.startswith("- "):
            break

        key, value_text = _split_yaml_key_value(line)
        if key in mapping:
            raise _YAMLError(line.number, f"duplicate key `{key}`")

        if value_text:
            mapping[key] = _parse_yaml_scalar(line.number, value_text)
            index += 1
            continue

        if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
            mapping[key] = {}
            index += 1
            continue

        mapping[key], index = _parse_yaml_block(
            lines, index + 1, lines[index + 1].indent
        )

    return mapping, index


def _parse_yaml_list(
    lines: list[_YAMLLine],
    index: int,
    indent: int,
) -> tuple[list[Any], int]:
    values: list[Any] = []

    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _YAMLError(line.number, "unexpected indentation")
        if not line.text.startswith("- "):
            break

        item_text = line.text[2:].strip()
        if not item_text:
            if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
                raise _YAMLError(line.number, "list item is empty")
            value, index = _parse_yaml_block(lines, index + 1, lines[index + 1].indent)
            values.append(value)
            continue

        if ":" in item_text and not item_text.startswith(("'", '"')):
            key, value_text = _split_yaml_key_value(
                _YAMLLine(line.number, line.indent, item_text),
            )
            item: dict[str, Any] = {
                key: _parse_yaml_scalar(line.number, value_text) if value_text else {}
            }
            index += 1

            if index < len(lines) and lines[index].indent > indent:
                nested, index = _parse_yaml_block(lines, index, lines[index].indent)
                if not isinstance(nested, dict):
                    raise _YAMLError(line.number, "list item mapping is invalid")
                item.update(nested)

            values.append(item)
            continue

        values.append(_parse_yaml_scalar(line.number, item_text))
        index += 1

    return values, index


def _split_yaml_key_value(line: _YAMLLine) -> tuple[str, str]:
    key, separator, value = line.text.partition(":")
    if not separator:
        raise _YAMLError(line.number, "expected `key: value`")
    key = key.strip()
    if not key:
        raise _YAMLError(line.number, "mapping key is empty")
    return key, value.strip()


def _parse_yaml_scalar(line_number: int, value: str) -> str | bool | list[Any]:
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value == "[]":
        return []
    if value.startswith(("'", '"')):
        quote = value[0]
        if not value.endswith(quote) or len(value) == 1:
            raise _YAMLError(line_number, "quoted string is not closed")
        return value[1:-1]
    if value.startswith(("[", "{")) or value.endswith(("]", "}")):
        raise _YAMLError(line_number, "flow-style collections are not supported")
    return value


def _workspace_config_from_mapping(raw_config: dict[str, Any]) -> WorkspaceConfig:
    _reject_unknown_keys(
        raw_config,
        {
            "workspace",
            "repositories",
            "extractors",
            "paths",
            "navigation",
            "enrichment",
            "source_retention",
        },
        "",
    )

    workspace = _required_mapping(raw_config, "workspace")
    _reject_unknown_keys(workspace, {"id", "name"}, "workspace")
    workspace_name = _required_string(workspace, "name", "workspace.name")
    repositories = _repositories_from_value(_required_value(raw_config, "repositories"))

    return WorkspaceConfig(
        id=_optional_slug(
            workspace,
            "id",
            slugify_identifier(workspace_name, fallback=WorkspaceConfig.id),
            "workspace.id",
        ),
        name=workspace_name,
        repositories=repositories,
        extractors=_extractors_from_value(raw_config.get("extractors", {})),
        paths=_paths_from_value(raw_config.get("paths", {})),
        navigation=_navigation_from_value(raw_config.get("navigation", {})),
        enrichment=_enrichment_from_value(raw_config.get("enrichment", {})),
        source_retention=_source_retention_from_value(
            raw_config.get("source_retention", {}),
        ),
    )


def _source_retention_from_value(value: Any) -> SourceRetentionConfig:
    if not isinstance(value, dict):
        raise _SchemaError("`source_retention` must be a mapping.")
    _reject_unknown_keys(value, {"max_bytes"}, "source_retention")
    max_bytes = value.get("max_bytes", SourceRetentionConfig.max_bytes)
    if isinstance(max_bytes, str) and max_bytes.isdecimal():
        max_bytes = int(max_bytes)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
        raise _SchemaError("`source_retention.max_bytes` must be an integer.")
    if not 1 <= max_bytes <= 100 * 1024 * 1024:
        raise _SchemaError(
            "`source_retention.max_bytes` must be between 1 and 104857600."
        )
    return SourceRetentionConfig(max_bytes=max_bytes)


def _enrichment_from_value(value: Any) -> EnrichmentConfig:
    if not isinstance(value, dict):
        raise _SchemaError("`enrichment` must be a mapping.")
    _reject_unknown_keys(value, {"dede"}, "enrichment")
    raw = value.get("dede", {})
    if not isinstance(raw, dict):
        raise _SchemaError("`enrichment.dede` must be a mapping.")
    _reject_unknown_keys(
        raw,
        {"enabled", "graph_path", "executable", "max_age_seconds"},
        "enrichment.dede",
    )
    max_age = raw.get("max_age_seconds", 86_400.0)
    try:
        max_age_seconds = float(max_age)
    except (TypeError, ValueError) as error:
        raise _SchemaError(
            "`enrichment.dede.max_age_seconds` must be a number."
        ) from error
    config = DedeEnrichmentConfig(
        enabled=_optional_bool(raw, "enabled", False, "enrichment.dede.enabled"),
        graph_path=_optional_string(
            raw,
            "graph_path",
            DedeEnrichmentConfig.graph_path,
            "enrichment.dede.graph_path",
        ),
        executable=_optional_string(
            raw,
            "executable",
            DedeEnrichmentConfig.executable,
            "enrichment.dede.executable",
        ),
        max_age_seconds=max_age_seconds,
    )
    if not 0 < config.max_age_seconds <= 31_536_000:
        raise _SchemaError(
            "`enrichment.dede.max_age_seconds` must be between 0 and 31536000."
        )
    return EnrichmentConfig(dede=config)


def _navigation_from_value(value: Any) -> NavigationConfig:
    if not isinstance(value, dict):
        raise _SchemaError("`navigation` must be a mapping.")
    _reject_unknown_keys(value, {"serena"}, "navigation")
    raw = value.get("serena", {})
    if not isinstance(raw, dict):
        raise _SchemaError("`navigation.serena` must be a mapping.")
    _reject_unknown_keys(
        raw, {"enabled", "command", "url", "timeout_seconds"}, "navigation.serena"
    )
    timeout = raw.get("timeout_seconds", 5.0)
    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError) as error:
        raise _SchemaError(
            "`navigation.serena.timeout_seconds` must be a number."
        ) from error
    config = SerenaNavigationConfig(
        enabled=_optional_bool(raw, "enabled", False, "navigation.serena.enabled"),
        command=_optional_string_tuple(raw, "command", "navigation.serena.command"),
        url=_optional_string_or_none(raw, "url", "navigation.serena.url"),
        timeout_seconds=timeout_seconds,
    )
    if config.enabled and bool(config.command) == bool(config.url):
        raise _SchemaError(
            "enabled `navigation.serena` must include exactly one `command` or `url`."
        )
    if not 0 < config.timeout_seconds <= 120:
        raise _SchemaError(
            "`navigation.serena.timeout_seconds` must be between 0 and 120."
        )
    return NavigationConfig(serena=config)


def _repositories_from_value(value: Any) -> tuple[RepositoryConfig, ...]:
    if not isinstance(value, list):
        raise _SchemaError("`repositories` must be a list of repository mappings.")
    if not value:
        raise _SchemaError("`repositories` must include at least one repository.")

    repositories: list[RepositoryConfig] = []
    for index, item in enumerate(value):
        path = f"repositories[{index}]"
        if not isinstance(item, dict):
            raise _SchemaError(f"`{path}` must be a mapping.")
        _reject_unknown_keys(item, {"id", "path", "name"}, path)
        repository_path = _required_string(item, "path", f"{path}.path")
        repository_name = _optional_string_or_none(item, "name", f"{path}.name")
        repositories.append(
            RepositoryConfig(
                id=_optional_slug(
                    item,
                    "id",
                    _repository_default_id(
                        repository_name,
                        repository_path,
                        index,
                    ),
                    f"{path}.id",
                ),
                path=repository_path,
                name=repository_name,
            ),
        )

    return tuple(repositories)


def _repository_default_id(
    repository_name: str | None,
    repository_path: str,
    index: int,
) -> str:
    if repository_name is not None:
        source = repository_name
    else:
        source = Path(repository_path).name
    return slugify_identifier(source, fallback=f"repository-{index + 1}")


def _extractors_from_value(value: Any) -> ExtractorConfig:
    if not isinstance(value, dict):
        raise _SchemaError("`extractors` must be a mapping.")
    _reject_unknown_keys(value, set(_EXTRACTOR_NAMES), "extractors")

    entries = {
        name: _extractor_entry_from_value(value.get(name, {}), f"extractors.{name}")
        for name in _EXTRACTOR_NAMES
    }
    return ExtractorConfig(**entries)


def _extractor_entry_from_value(value: Any, path: str) -> ExtractorEntryConfig:
    if isinstance(value, bool):
        return ExtractorEntryConfig(enabled=value)
    if not isinstance(value, dict):
        raise _SchemaError(f"`{path}` must be a boolean or mapping.")
    _reject_unknown_keys(
        value,
        {"enabled", "command", "connect", "json_path", "watch"},
        path,
    )
    return ExtractorEntryConfig(
        enabled=_optional_bool(value, "enabled", True, f"{path}.enabled"),
        command=_optional_string_tuple(value, "command", f"{path}.command"),
        connect=_optional_string_or_none(value, "connect", f"{path}.connect"),
        json_path=_optional_string_or_none(value, "json_path", f"{path}.json_path"),
        watch=_optional_watch_config(value, "watch", f"{path}.watch"),
    )


def _optional_watch_config(
    mapping: dict[str, Any],
    key: str,
    path: str,
) -> ExtractorWatchConfig | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _SchemaError(f"`{path}` must be a mapping.")
    _reject_unknown_keys(value, {"command", "output_path"}, path)
    watch = ExtractorWatchConfig(
        command=_optional_string_tuple(value, "command", f"{path}.command"),
        output_path=_optional_string_or_none(
            value,
            "output_path",
            f"{path}.output_path",
        ),
    )
    if not watch.command and watch.output_path is None:
        raise _SchemaError(
            f"`{path}` must include `command` or `output_path` when provided.",
        )
    return watch


def _paths_from_value(value: Any) -> OptionalPathConfig:
    if not isinstance(value, dict):
        raise _SchemaError("`paths` must be a mapping.")
    _reject_unknown_keys(value, set(_PATH_NAMES), "paths")
    values = {
        name: _optional_string_tuple(value, name, f"paths.{name}")
        for name in _PATH_NAMES
    }
    return OptionalPathConfig(**values)


def _required_mapping(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = _required_value(mapping, key)
    if not isinstance(value, dict):
        raise _SchemaError(f"`{key}` must be a mapping.")
    return value


def _required_value(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise _SchemaError(f"`{key}` is required.")
    return mapping[key]


def _required_string(mapping: dict[str, Any], key: str, path: str) -> str:
    value = _required_value(mapping, key)
    if not isinstance(value, str) or not value:
        raise _SchemaError(f"`{path}` must be a non-empty string.")
    return value


def _optional_string(
    mapping: dict[str, Any],
    key: str,
    default: str,
    path: str,
) -> str:
    value = mapping.get(key, default)
    if not isinstance(value, str) or not value:
        raise _SchemaError(f"`{path}` must be a non-empty string.")
    return value


def _optional_slug(
    mapping: dict[str, Any],
    key: str,
    default: str,
    path: str,
) -> str:
    value = _optional_string(mapping, key, default, path)
    return slugify_identifier(value, fallback=default)


def _optional_string_or_none(
    mapping: dict[str, Any],
    key: str,
    path: str,
) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _SchemaError(f"`{path}` must be a non-empty string when provided.")
    return value


def _optional_bool(
    mapping: dict[str, Any],
    key: str,
    default: bool,
    path: str,
) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise _SchemaError(f"`{path}` must be a boolean.")
    return value


def _optional_string_tuple(
    mapping: dict[str, Any],
    key: str,
    path: str,
) -> tuple[str, ...]:
    value = mapping.get(key, [])
    if not isinstance(value, list):
        raise _SchemaError(f"`{path}` must be a list of strings.")
    if not all(isinstance(item, str) and item for item in value):
        raise _SchemaError(f"`{path}` must be a list of non-empty strings.")
    return tuple(value)


def _reject_unknown_keys(
    mapping: dict[str, Any],
    allowed_keys: set[str],
    path: str,
) -> None:
    unknown_keys = set(mapping) - allowed_keys
    if unknown_keys:
        prefix = f"{path}." if path else ""
        unknown = sorted(unknown_keys)[0]
        raise _SchemaError(f"`{prefix}{unknown}` is not a supported config field.")
