from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from code_scout.models.workspace import Workspace
from code_scout.utils import language_map

INCLUDED_SUFFIXES = frozenset(
    {
        ".astro",
        ".bash",
        ".c",
        ".cjs",
        ".cpp",
        ".cs",
        ".cshtml",
        ".csproj",
        ".cts",
        ".dart",
        ".dfm",
        ".dm",
        ".dme",
        ".dmf",
        ".dmi",
        ".dmm",
        ".dpk",
        ".dpr",
        ".ex",
        ".exs",
        ".f",
        ".f03",
        ".f08",
        ".f90",
        ".f95",
        ".fsproj",
        ".go",
        ".gradle",
        ".groovy",
        ".h",
        ".hpp",
        ".inc",
        ".java",
        ".jl",
        ".js",
        ".jsx",
        ".json",
        ".kt",
        ".lfm",
        ".lpk",
        ".lpr",
        ".lua",
        ".luau",
        ".m",
        ".md",
        ".mdx",
        ".mjs",
        ".mm",
        ".mts",
        ".pas",
        ".php",
        ".pp",
        ".ps1",
        ".psm1",
        ".py",
        ".razor",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sln",
        ".slnx",
        ".sql",
        ".sv",
        ".svelte",
        ".svh",
        ".swift",
        ".ts",
        ".tsx",
        ".v",
        ".vbproj",
        ".vue",
        ".yaml",
        ".yml",
        ".zig",
    },
)
INCLUDED_FILENAMES = frozenset(
    {"claude.md", "dockerfile", "package.json", "turbo.json", "nx.json"}
)
METADATA_SUFFIXES = frozenset(
    {
        ".csproj",
        ".dfm",
        ".dmf",
        ".dmi",
        ".dmm",
        ".fsproj",
        ".gradle",
        ".json",
        ".lfm",
        ".lpk",
        ".sln",
        ".slnx",
        ".vbproj",
        ".yaml",
        ".yml",
    },
)
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        "." + bytes((112, 101, 109)).decode("ascii"),
        ".code-scout",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "bin",
        "build",
        "cache",
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
        "target",
        "venv",
        "virtualenv",
    },
)
DEFAULT_ADR_PATHS = (
    "docs/adr",
    "adr",
    "architecture/decisions",
)


@dataclass(frozen=True)
class DiscoveredFile:
    repository_id: str
    path: str
    absolute_path: str
    kind: str
    extension: str
    content_hash: str
    modified_at: str
    language: str | None = None


@dataclass(frozen=True)
class DiscoveredFileContent:
    repository_id: str
    path: str
    content_hash: str | None
    error: str | None = None


def discover_workspace_files(workspace: Workspace) -> tuple[DiscoveredFile, ...]:
    root_path = Path(workspace.root_path).resolve(strict=True)
    discovered_files: list[DiscoveredFile] = []
    adr_paths = _normalized_adr_paths(workspace.adr_paths)
    exclude_paths = _normalized_exclude_paths(workspace.exclude_paths)

    for repository in workspace.repositories:
        repository_path = Path(repository.path)
        repository_root = (
            repository_path
            if repository_path.is_absolute()
            else root_path / repository_path
        ).resolve(strict=True)

        for path in _walk_repository(repository_root, exclude_paths):
            discovered_files.append(
                DiscoveredFile(
                    repository_id=repository.id,
                    path=path.relative_to(repository_root).as_posix(),
                    absolute_path=str(path),
                    kind=_file_kind(path, repository_root, adr_paths),
                    extension=_file_extension(path),
                    content_hash=_content_hash(path),
                    modified_at=_modified_at(path),
                    language=language_map.language_for_extension(path.suffix.lower()),
                ),
            )

    return tuple(
        sorted(discovered_files, key=lambda file: (file.repository_id, file.path)),
    )


def discover_workspace_file_content(
    workspace: Workspace,
) -> tuple[DiscoveredFileContent, ...]:
    """Discover in-scope paths and hash each readable file independently."""

    root_path = Path(workspace.root_path).resolve(strict=True)
    discovered_files: list[DiscoveredFileContent] = []
    exclude_paths = _normalized_exclude_paths(workspace.exclude_paths)

    for repository in workspace.repositories:
        repository_path = Path(repository.path)
        repository_root = (
            repository_path
            if repository_path.is_absolute()
            else root_path / repository_path
        ).resolve(strict=True)

        for path in _walk_repository(repository_root, exclude_paths):
            try:
                content_hash = _content_hash(path)
            except OSError as error:
                discovered_files.append(
                    DiscoveredFileContent(
                        repository_id=repository.id,
                        path=path.relative_to(repository_root).as_posix(),
                        content_hash=None,
                        error=str(error),
                    ),
                )
                continue
            discovered_files.append(
                DiscoveredFileContent(
                    repository_id=repository.id,
                    path=path.relative_to(repository_root).as_posix(),
                    content_hash=content_hash,
                ),
            )

    return tuple(
        sorted(discovered_files, key=lambda file: (file.repository_id, file.path)),
    )


def _walk_repository(
    repository_root: Path,
    exclude_paths: tuple[str, ...],
) -> tuple[Path, ...]:
    files: list[Path] = []

    for directory, directory_names, filenames in os.walk(repository_root):
        relative_directory = Path(directory).relative_to(repository_root).as_posix()
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not _is_excluded_directory(name)
            and not _is_excluded_path(
                _join_repository_path(relative_directory, name),
                exclude_paths,
            )
        )

        for filename in sorted(filenames):
            path = Path(directory) / filename
            relative_path = _join_repository_path(relative_directory, filename)
            if _is_included_file(path) and not _is_excluded_path(
                relative_path,
                exclude_paths,
            ):
                files.append(path.resolve(strict=True))

    return tuple(files)


def _is_excluded_directory(name: str) -> bool:
    normalized_name = name.lower()
    return (
        normalized_name.startswith(".")
        or normalized_name in EXCLUDED_DIRECTORY_NAMES
        or normalized_name.endswith("_cache")
        or normalized_name.endswith("-cache")
    )


def _is_excluded_path(relative_path: str, exclude_paths: tuple[str, ...]) -> bool:
    normalized_path = _normalize_repository_relative_path(relative_path)
    return any(
        normalized_path == exclude_path
        or normalized_path.startswith(f"{exclude_path}/")
        for exclude_path in exclude_paths
    )


def _join_repository_path(relative_directory: str, name: str) -> str:
    if relative_directory == ".":
        return name
    return f"{relative_directory}/{name}"


def _is_included_file(path: Path) -> bool:
    filename = path.name.lower()
    return (
        filename in INCLUDED_FILENAMES
        or filename.startswith("dockerfile.")
        or filename.startswith("docker-compose.")
        or path.suffix.lower() in INCLUDED_SUFFIXES
    )


def _file_kind(
    path: Path,
    repository_root: Path,
    adr_paths: tuple[str, ...],
) -> str:
    filename = path.name.lower()
    suffix = path.suffix.lower()
    relative_path = path.relative_to(repository_root).as_posix()
    if filename == "claude.md":
        return "decision"
    if suffix in {".md", ".mdx"} and _is_adr_path(
        relative_path,
        adr_paths,
    ):
        return "decision"
    if suffix in {".md", ".mdx"}:
        return "documentation"
    if (
        filename.startswith("dockerfile")
        or filename.startswith("docker-compose.")
        or suffix in METADATA_SUFFIXES
    ):
        return "metadata"
    return "source"


def _file_extension(path: Path) -> str:
    filename = path.name.lower()
    if filename == "dockerfile":
        return ""
    return path.suffix.lower()


def _content_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _modified_at(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalized_adr_paths(configured_paths: tuple[str, ...]) -> tuple[str, ...]:
    normalized_paths = {
        _normalize_repository_relative_path(path)
        for path in (*DEFAULT_ADR_PATHS, *configured_paths)
    }
    return tuple(sorted(path for path in normalized_paths if path))


def _normalized_exclude_paths(configured_paths: tuple[str, ...]) -> tuple[str, ...]:
    normalized_paths = {
        _normalize_repository_relative_path(path) for path in configured_paths
    }
    return tuple(sorted(path for path in normalized_paths if path))


def _normalize_repository_relative_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./").strip("/")


def _is_adr_path(relative_path: str, adr_paths: tuple[str, ...]) -> bool:
    normalized_path = _normalize_repository_relative_path(relative_path)
    return any(
        normalized_path == adr_path
        or normalized_path.startswith(f"{adr_path}/")
        or f"/{adr_path}/" in normalized_path
        for adr_path in adr_paths
    )
