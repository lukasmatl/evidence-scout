from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from code_scout.core.errors import ConfigError
from code_scout.discovery import discover_workspace_file_content
from code_scout.extractors.profiles import EXTRACTOR_PROFILES
from code_scout.extractors.treesitter import GRAMMAR_PACKAGES, load_grammar
from code_scout.models.workspace import (
    DEFAULT_EXTRACTORS,
    default_single_repository_workspace,
)
from code_scout.storage import SCHEMA_VERSION, database_path
from code_scout.utils.language_map import tier_for_extension
from code_scout.workspace.config import DEFAULT_CONFIG_PATH, load_workspace_config

STATUS_REPORT_SCHEMA_VERSION = "1"

IndexState = Literal["missing", "unscanned", "ready", "unreadable", "incompatible"]
FreshnessState = Literal["fresh", "stale", "unknown"]


@dataclass(frozen=True, order=True)
class StatusPath:
    repository_id: str
    path: str

    def to_dict(self) -> dict[str, str]:
        return {"repository_id": self.repository_id, "path": self.path}


@dataclass(frozen=True)
class WorkspaceStatusReport:
    workspace: dict[str, Any]
    index: dict[str, Any]
    freshness: dict[str, Any]
    changes: dict[str, Any]
    coverage: dict[str, Any]
    recommendations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATUS_REPORT_SCHEMA_VERSION,
            "workspace": self.workspace,
            "index": self.index,
            "freshness": self.freshness,
            "changes": self.changes,
            "coverage": self.coverage,
            "recommendations": list(self.recommendations),
        }

    def render(self, output_format: str = "text") -> str:
        if output_format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True)
        if output_format != "text":
            raise ConfigError(
                f"Unsupported status format {output_format!r}.",
                "Use `--format text` or `--format json`.",
            )
        return _render_text(self)


def workspace_status(workspace_path: str | Path = ".") -> WorkspaceStatusReport:
    root = Path(workspace_path).expanduser()
    if not root.exists():
        raise ConfigError(
            f"Workspace does not exist: {root}.",
            "Pass an existing directory with `--workspace`.",
        )
    if not root.is_dir():
        raise ConfigError(
            f"Workspace is not a directory: {root}.",
            "Pass a directory with `--workspace`.",
        )
    root = root.resolve()
    config_path = root / DEFAULT_CONFIG_PATH
    storage_path = database_path(root)

    if not config_path.is_file():
        return _empty_report(
            root,
            storage_path,
            state="missing",
            recommendation=f"code-scout init --workspace {root}",
        )

    config = load_workspace_config(root)
    workspace = default_single_repository_workspace(root, config=config)
    identity = _workspace_identity(workspace)

    if not storage_path.is_file():
        return _empty_report(
            root,
            storage_path,
            state="unscanned",
            identity=identity,
            workspace=workspace,
            recommendation=f"code-scout scan --workspace {root}",
        )

    try:
        connection = _open_storage_read_only(storage_path)
    except sqlite3.Error:
        return _empty_report(
            root,
            storage_path,
            state="unreadable",
            identity=identity,
            workspace=workspace,
            recommendation=f"code-scout scan --workspace {root}",
        )

    try:
        compatibility = _storage_compatibility(connection)
        if compatibility is not None:
            return _empty_report(
                root,
                storage_path,
                state="incompatible",
                identity=identity,
                workspace=workspace,
                index_schema_version=compatibility,
                recommendation=f"code-scout scan --workspace {root}",
            )

        snapshot = _latest_snapshot(connection)
        if snapshot is None:
            return _empty_report(
                root,
                storage_path,
                state="unscanned",
                identity=identity,
                workspace=workspace,
                index_schema_version=str(SCHEMA_VERSION),
                recommendation=f"code-scout scan --workspace {root}",
            )

        indexed_hashes = _indexed_hashes(connection)
        snapshot_metadata = json.loads(snapshot["metadata_json"])
        return _ready_report(
            workspace,
            storage_path,
            latest_scan_at=snapshot["created_at"],
            snapshot_metadata=snapshot_metadata,
            indexed_hashes=indexed_hashes,
            coverage=_coverage_report(connection, workspace, snapshot_metadata),
        )
    except (
        AttributeError,
        json.JSONDecodeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return _empty_report(
            root,
            storage_path,
            state="unreadable",
            identity=identity,
            workspace=workspace,
            recommendation=f"code-scout scan --workspace {root}",
        )
    finally:
        connection.close()


def _open_storage_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _storage_compatibility(connection: sqlite3.Connection) -> str | None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        return str(version)

    required_columns = {
        "files": {"repository_id", "path", "content_hash"},
        "snapshots": {"created_at", "metadata_json"},
    }
    for table, expected_columns in required_columns.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {str(row["name"]) for row in rows}
        if not expected_columns <= columns:
            return str(version)
    return None


def _latest_snapshot(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT created_at, metadata_json
        FROM snapshots
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
    ).fetchone()


def _indexed_hashes(
    connection: sqlite3.Connection,
) -> dict[StatusPath, str | None]:
    rows = connection.execute(
        """
        SELECT repository_id, path, content_hash
        FROM files
        ORDER BY repository_id, path
        """,
    ).fetchall()
    return {
        StatusPath(str(row["repository_id"]), str(row["path"])): row["content_hash"]
        for row in rows
    }


def _ready_report(
    workspace: Any,
    storage_path: Path,
    *,
    latest_scan_at: str,
    snapshot_metadata: dict[str, Any],
    indexed_hashes: dict[StatusPath, str | None],
    coverage: dict[str, Any],
) -> WorkspaceStatusReport:
    current_files = discover_workspace_file_content(workspace)
    current_hashes = {
        StatusPath(file.repository_id, file.path): file.content_hash
        for file in current_files
    }
    unreadable = tuple(
        sorted(
            StatusPath(file.repository_id, file.path)
            for file in current_files
            if file.content_hash is None
        ),
    )
    readable_current = {
        path: content_hash
        for path, content_hash in current_hashes.items()
        if content_hash is not None
    }
    indexed_paths = set(indexed_hashes)
    current_paths = set(current_hashes)
    unreadable_paths = set(unreadable)
    added = tuple(sorted((current_paths - indexed_paths) - unreadable_paths))
    deleted = tuple(sorted(indexed_paths - current_paths))
    modified = tuple(
        sorted(
            path
            for path in indexed_paths & set(readable_current)
            if indexed_hashes[path] is not None
            and indexed_hashes[path] != readable_current[path]
        ),
    )

    coverage_reasons: list[dict[str, str]] = []
    scan_scope_state = coverage["scan_scope"]["state"]
    if scan_scope_state == "unknown":
        coverage_reasons.append(
            {
                "code": "scan_scope_unknown",
                "message": (
                    "Historical scan metadata does not identify the extraction scope."
                ),
                "remediation": (
                    f"code-scout scan --workspace {Path(workspace.root_path).resolve()}"
                ),
            },
        )
    elif scan_scope_state == "partial":
        coverage_reasons.append(
            {
                "code": "partial_scan_scope",
                "message": (
                    "The latest full-scan record used a selected extractor or profile."
                ),
            },
        )
    missing_hashes = tuple(
        sorted(path for path, value in indexed_hashes.items() if not value)
    )
    if missing_hashes:
        coverage_reasons.append(
            {
                "code": "missing_index_hashes",
                "message": (
                    f"{len(missing_hashes)} indexed file(s) have no content hash."
                ),
            },
        )
    if unreadable:
        coverage_reasons.append(
            {
                "code": "unreadable_workspace_files",
                "message": f"{len(unreadable)} in-scope file(s) could not be read.",
            },
        )

    changed = bool(added or modified or deleted)
    if changed:
        freshness: FreshnessState = "stale"
    elif coverage_reasons:
        freshness = "unknown"
    else:
        freshness = "fresh"
    recommendations = (
        (f"code-scout scan --workspace {Path(workspace.root_path).resolve()}",)
        if freshness != "fresh"
        else ()
    )
    return WorkspaceStatusReport(
        workspace=_workspace_identity(workspace),
        index={
            "state": "ready",
            "database_path": str(storage_path),
            "schema_version": str(SCHEMA_VERSION),
            "latest_scan_at": latest_scan_at,
        },
        freshness={
            "state": freshness,
            "coverage": "unknown" if coverage_reasons else "complete",
            "coverage_reasons": coverage_reasons,
        },
        changes=_changes(added, modified, deleted, unreadable),
        coverage=coverage,
        recommendations=recommendations,
    )


def _empty_report(
    root: Path,
    storage_path: Path,
    *,
    state: IndexState,
    recommendation: str,
    identity: dict[str, Any] | None = None,
    workspace: Any | None = None,
    index_schema_version: str | None = None,
) -> WorkspaceStatusReport:
    return WorkspaceStatusReport(
        workspace=identity
        or {"id": None, "name": None, "root_path": str(root), "repositories": []},
        index={
            "state": state,
            "database_path": str(storage_path),
            "schema_version": index_schema_version,
            "latest_scan_at": None,
        },
        freshness={
            "state": "unknown",
            "coverage": "unknown",
            "coverage_reasons": [
                {
                    "code": f"index_{state}",
                    "message": f"Workspace index state is {state}.",
                },
            ],
        },
        changes=_changes((), (), (), ()),
        coverage=_unavailable_coverage(workspace),
        recommendations=(recommendation,),
    )


def _workspace_identity(workspace: Any) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "name": workspace.name,
        "root_path": str(Path(workspace.root_path).resolve()),
        "repositories": [
            {
                "id": repository.id,
                "name": repository.display_name,
                "path": repository.path,
            }
            for repository in workspace.repositories
        ],
    }


def _changes(
    added: tuple[StatusPath, ...],
    modified: tuple[StatusPath, ...],
    deleted: tuple[StatusPath, ...],
    unreadable: tuple[StatusPath, ...],
) -> dict[str, Any]:
    paths_by_kind = {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unreadable": unreadable,
    }
    return {
        "counts": {kind: len(paths) for kind, paths in paths_by_kind.items()},
        **{
            kind: [path.to_dict() for path in paths]
            for kind, paths in paths_by_kind.items()
        },
    }


def _coverage_report(
    connection: sqlite3.Connection,
    workspace: Any,
    snapshot_metadata: dict[str, Any],
) -> dict[str, Any]:
    enabled = tuple(workspace.extractors.enabled)
    disabled = tuple(name for name in DEFAULT_EXTRACTORS if name not in enabled)
    fact_counts = {
        str(row["extractor"]): int(row["fact_count"])
        for row in connection.execute(
            """
            SELECT extractor, COUNT(*) AS fact_count
            FROM facts
            GROUP BY extractor
            ORDER BY extractor
            """,
        ).fetchall()
    }
    fact_paths: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT extractor, source_file FROM facts",
    ).fetchall():
        fact_paths[str(row["extractor"])].add(str(row["source_file"]))

    file_rows = connection.execute(
        """
        SELECT path, extension, language
        FROM files
        ORDER BY path
        """,
    ).fetchall()
    language_counts = Counter(
        str(row["language"]) for row in file_rows if row["language"] is not None
    )
    unknown_language_count = sum(row["language"] is None for row in file_rows)
    scan_scope = _scan_scope(snapshot_metadata, enabled)
    grammar_support = _grammar_support(file_rows, enabled, fact_paths)
    extractor_states = _extractor_states(
        enabled,
        disabled,
        fact_counts,
        grammar_support,
    )
    symbol_count = int(connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
    relationship_count = int(
        connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    )
    gaps = _coverage_gaps(
        scan_scope,
        grammar_support,
        file_count=len(file_rows),
        symbol_count=symbol_count,
        relationship_count=relationship_count,
        enabled=enabled,
    )
    return {
        "scan_scope": scan_scope,
        "extractors": {
            "enabled": list(enabled),
            "disabled": list(disabled),
            "states": extractor_states,
            "observed_evidence": {
                "total_facts": sum(fact_counts.values()),
                "by_extractor": fact_counts,
            },
        },
        "file_discovery": {
            "state": "observed",
            "file_count": len(file_rows),
            "languages": [
                {"language": language, "file_count": count}
                for language, count in sorted(language_counts.items())
            ],
            "unknown_language_file_count": unknown_language_count,
            "meaning": (
                "Files were discovered; this does not prove symbol or call extraction."
            ),
        },
        "symbol_extraction": {
            "state": "observed" if symbol_count else "unknown",
            "symbol_count": symbol_count,
            "meaning": "Persisted symbols observed in the resolved graph.",
        },
        "relationship_extraction": {
            "state": "observed" if relationship_count else "unknown",
            "relationship_count": relationship_count,
            "meaning": (
                "Persisted relationships observed; file counts do not imply "
                "call coverage."
            ),
        },
        "grammar_support": grammar_support,
        "gaps": gaps,
    }


def _scan_scope(
    metadata: dict[str, Any],
    enabled: tuple[str, ...],
) -> dict[str, Any]:
    selection_type = metadata.get("selection_type")
    selected = metadata.get("selected_extractors")
    if (
        selection_type in {"all_enabled", "extractor", "profile"}
        and isinstance(selected, list)
        and all(isinstance(item, str) for item in selected)
    ):
        return {
            "state": "full" if selection_type == "all_enabled" else "partial",
            "metadata": "available",
            "selection_type": selection_type,
            "selection": metadata.get("selection"),
            "selected_extractors": selected,
        }

    legacy_selection = metadata.get("extractor")
    if isinstance(legacy_selection, str) and legacy_selection:
        if legacy_selection in EXTRACTOR_PROFILES:
            selected_extractors = [
                name
                for name in EXTRACTOR_PROFILES[legacy_selection].scan_extractors
                if name in enabled
            ]
            legacy_type = "profile"
        else:
            selected_extractors = [legacy_selection]
            legacy_type = "extractor"
        return {
            "state": "partial",
            "metadata": "legacy",
            "selection_type": legacy_type,
            "selection": legacy_selection,
            "selected_extractors": selected_extractors,
        }

    return {
        "state": "unknown",
        "metadata": "unavailable",
        "selection_type": None,
        "selection": None,
        "selected_extractors": [],
    }


def _extractor_states(
    enabled: tuple[str, ...],
    disabled: tuple[str, ...],
    fact_counts: dict[str, int],
    grammar_support: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    aliases = {
        "symbol_extraction": (
            "tree_sitter",
            "heuristic",
            "dotnet",
            "byond",
            "frontend_api",
        ),
        "adr": ("adr_markdown",),
        "package_metadata": ("package_json",),
    }
    all_names = (
        *DEFAULT_EXTRACTORS,
        *sorted(set(fact_counts) - set(DEFAULT_EXTRACTORS)),
    )
    states: list[dict[str, Any]] = []
    for name in all_names:
        observed_names = (name, *aliases.get(name, ()))
        evidence_count = sum(fact_counts.get(alias, 0) for alias in observed_names)
        if name in disabled:
            state = "disabled"
        elif evidence_count:
            state = "observed"
        elif name == "tree_sitter" and any(
            grammar["state"] == "unavailable" for grammar in grammar_support
        ):
            state = "unavailable"
        else:
            state = "unknown"
        states.append(
            {
                "name": name,
                "configuration": (
                    "enabled"
                    if name in enabled
                    else "disabled"
                    if name in DEFAULT_EXTRACTORS
                    else "internal"
                ),
                "state": state,
                "evidence_count": evidence_count,
            },
        )
    return states


def _grammar_support(
    file_rows: list[sqlite3.Row],
    enabled: tuple[str, ...],
    fact_paths: dict[str, set[str]],
) -> list[dict[str, Any]]:
    grouped: Counter[tuple[str, str]] = Counter()
    grouped_paths: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in file_rows:
        extension = str(row["extension"])
        if tier_for_extension(extension) != "tree_sitter":
            continue
        language = str(row["language"])
        grammar_language = (
            "TSX" if language == "TypeScript" and extension == ".tsx" else language
        )
        key = (language, grammar_language)
        grouped[key] += 1
        grouped_paths[key].add(str(row["path"]))

    results: list[dict[str, Any]] = []
    extraction_enabled = "symbol_extraction" in enabled and "tree_sitter" in enabled
    for (language, grammar_language), file_count in sorted(grouped.items()):
        grammar = GRAMMAR_PACKAGES.get(grammar_language)
        if not extraction_enabled:
            installation = "unknown"
            state = "disabled"
            reason = "Tree-sitter symbol extraction is disabled."
        else:
            loaded = load_grammar(grammar_language)
            installation = "installed" if loaded.available else "missing"
            if not loaded.available:
                state = "unavailable"
            elif (
                grouped_paths[(language, grammar_language)] & fact_paths["tree_sitter"]
            ):
                state = "observed"
            else:
                state = "unknown"
            reason = loaded.missing_reason
        results.append(
            {
                "language": language,
                "grammar_language": grammar_language,
                "file_count": file_count,
                "package": None if grammar is None else grammar.package,
                "installation": installation,
                "state": state,
                "reason": reason,
            },
        )
    return results


def _coverage_gaps(
    scan_scope: dict[str, Any],
    grammar_support: list[dict[str, Any]],
    *,
    file_count: int,
    symbol_count: int,
    relationship_count: int,
    enabled: tuple[str, ...],
) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    if scan_scope["state"] == "unknown":
        gaps.append(
            {
                "code": "scan_scope_unknown",
                "message": (
                    "Historical scan metadata does not identify the extraction scope."
                ),
                "remediation": (
                    "Run `code-scout scan --workspace PATH` to record current "
                    "scope metadata."
                ),
            },
        )
    elif scan_scope["state"] == "partial":
        gaps.append(
            {
                "code": "partial_scan_scope",
                "message": (
                    "The latest scan selected only part of the configured "
                    "extraction scope."
                ),
                "remediation": (
                    "Run `code-scout scan --workspace PATH` without `--extractor` "
                    "or `--profile`."
                ),
            },
        )
    missing = [
        item["grammar_language"]
        for item in grammar_support
        if item["installation"] == "missing"
    ]
    if missing:
        gaps.append(
            {
                "code": "missing_grammars",
                "message": "Missing Tree-sitter grammar support for: "
                + ", ".join(missing)
                + ".",
                "remediation": (
                    "Install grammar support with `uv sync --extra "
                    "tree-sitter-all`, then rescan."
                ),
            },
        )
    if file_count and "symbol_extraction" not in enabled:
        gaps.append(
            {
                "code": "symbol_extraction_disabled",
                "message": "Files were discovered while symbol extraction is disabled.",
                "remediation": (
                    "Enable `extractors.symbol_extraction` in "
                    "`.code-scout/config.yaml`, then rescan."
                ),
            },
        )
    elif file_count and not symbol_count:
        gaps.append(
            {
                "code": "symbols_unobserved",
                "message": (
                    "Files were discovered but no symbols are observed in the "
                    "resolved graph."
                ),
                "remediation": (
                    "Review language support and extractor selection, then run "
                    "a full scan."
                ),
            },
        )
    if file_count and not relationship_count:
        gaps.append(
            {
                "code": "relationships_unobserved",
                "message": (
                    "No extracted relationships are observed; discovered files "
                    "do not prove call coverage."
                ),
                "remediation": (
                    "Enable a relationship-capable extractor and run a full scan."
                ),
            },
        )
    return gaps


def _unavailable_coverage(workspace: Any | None) -> dict[str, Any]:
    enabled = [] if workspace is None else list(workspace.extractors.enabled)
    disabled = [name for name in DEFAULT_EXTRACTORS if name not in enabled]
    return {
        "scan_scope": {
            "state": "unknown",
            "metadata": "unavailable",
            "selection_type": None,
            "selection": None,
            "selected_extractors": [],
        },
        "extractors": {
            "enabled": enabled,
            "disabled": disabled,
            "states": [],
            "observed_evidence": {"total_facts": 0, "by_extractor": {}},
        },
        "file_discovery": {
            "state": "unknown",
            "file_count": 0,
            "languages": [],
            "unknown_language_file_count": 0,
            "meaning": "No indexed discovery evidence is available.",
        },
        "symbol_extraction": {
            "state": "unknown",
            "symbol_count": 0,
            "meaning": "No indexed symbol evidence is available.",
        },
        "relationship_extraction": {
            "state": "unknown",
            "relationship_count": 0,
            "meaning": "No indexed relationship evidence is available.",
        },
        "grammar_support": [],
        "gaps": [],
    }


def _render_text(report: WorkspaceStatusReport) -> str:
    workspace = report.workspace
    index = report.index
    freshness = report.freshness
    changes = report.changes
    coverage = report.coverage
    workspace_name = workspace["name"] or "unconfigured"
    workspace_id = workspace["id"] or "unknown"
    lines = [
        "Workspace status:",
        f"Workspace: {workspace_name} ({workspace_id})",
        f"Root: {workspace['root_path']}",
        f"Index state: {index['state']}",
        f"Latest scan: {index['latest_scan_at'] or 'unknown'}",
        f"Freshness: {freshness['state']}",
        f"Coverage: {freshness['coverage']}",
        f"Scan scope: {coverage['scan_scope']['state']}",
        "Extractors:",
        "  Enabled: " + (", ".join(coverage["extractors"]["enabled"]) or "none"),
        "  Disabled: " + (", ".join(coverage["extractors"]["disabled"]) or "none"),
        "Observed evidence by extractor:",
    ]
    evidence_counts = coverage["extractors"]["observed_evidence"]["by_extractor"]
    lines.extend(
        [f"  {name}: {count}" for name, count in evidence_counts.items()] or ["  none"]
    )
    lines.extend(
        [
            "Discovery and extraction:",
            f"  Discovered files: {coverage['file_discovery']['file_count']}",
            "  Extracted symbols: "
            f"{coverage['symbol_extraction']['symbol_count']} "
            f"({coverage['symbol_extraction']['state']})",
            "  Extracted relationships: "
            f"{coverage['relationship_extraction']['relationship_count']} "
            f"({coverage['relationship_extraction']['state']})",
            "Languages:",
        ],
    )
    lines.extend(
        [
            f"  {item['language']}: {item['file_count']} file(s)"
            for item in coverage["file_discovery"]["languages"]
        ]
        or ["  none"]
    )
    if coverage["grammar_support"]:
        lines.append("Grammar support:")
        lines.extend(
            f"  {item['grammar_language']}: {item['installation']} ({item['state']})"
            for item in coverage["grammar_support"]
        )
    lines.extend(
        [
            "Changes:",
        ]
    )
    for kind in ("added", "modified", "deleted", "unreadable"):
        lines.append(f"  {kind.capitalize()}: {changes['counts'][kind]}")
        lines.extend(
            f"    - {path['repository_id']}:{path['path']}" for path in changes[kind]
        )
    if freshness["coverage_reasons"]:
        lines.append("Coverage notes:")
        lines.extend(
            f"- {reason['message']}" for reason in freshness["coverage_reasons"]
        )
    if report.recommendations:
        lines.append("Recommended:")
        lines.extend(f"- {recommendation}" for recommendation in report.recommendations)
    if coverage["gaps"]:
        lines.append("Coverage gaps:")
        for gap in coverage["gaps"]:
            lines.append(f"- {gap['message']}")
            lines.append(f"  Remediation: {gap['remediation']}")
    return "\n".join(lines)
