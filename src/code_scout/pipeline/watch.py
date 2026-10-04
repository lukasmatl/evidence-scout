from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from code_scout.core.errors import ConfigError
from code_scout.extractors import selected_watch_route_extractors
from code_scout.extractors.graphify import (
    GraphifyProcessAdapter,
    GraphifyWatchAdapter,
    GraphifyWatchEvent,
)
from code_scout.harvest import ChangedFileReplacementResult
from code_scout.models.workspace import (
    RepositoryConfig,
    Workspace,
    default_single_repository_workspace,
)
from code_scout.pipeline.incremental import replace_changed_file_active_facts
from code_scout.pipeline.watch_routes import (
    match_watch_routes,
    watch_extractors_for_paths,
)
from code_scout.workspace.config import (
    ExtractorWatchConfig,
    WorkspaceConfig,
    load_workspace_config,
)


@dataclass(frozen=True)
class WatchFileEvent:
    path: str
    event_type: str = "changed"


class WatchAdapter(Protocol):
    def changed_files(
        self,
        *,
        workspace_root: str,
        debounce: float,
        changed_only: bool,
    ) -> tuple[WatchFileEvent, ...]: ...


class EmptyWatchAdapter:
    def changed_files(
        self,
        *,
        workspace_root: str,
        debounce: float,
        changed_only: bool,
    ) -> tuple[WatchFileEvent, ...]:
        return ()


@dataclass(frozen=True)
class WatchFilePlan:
    path: str
    event_type: str
    routes: tuple[str, ...]
    extractors: tuple[str, ...]


@dataclass(frozen=True)
class WatchDryRunPlan:
    workspace: Workspace
    repository: RepositoryConfig | None
    profile: str | None
    debounce: float
    changed_only: bool
    files: tuple[WatchFilePlan, ...]
    extractors: tuple[str, ...]

    def summary(self) -> str:
        repository_label = (
            "all repositories" if self.repository is None else self.repository.id
        )
        profile_label = self.profile or "all enabled extractors"
        changed_label = "yes" if self.changed_only else "no"
        extractors = ", ".join(self.extractors) or "none"
        lines = [
            "Watch dry run.",
            f"Workspace: {self.workspace.name} ({self.workspace.id})",
            f"Repository: {repository_label}",
            f"Profile: {profile_label}",
            f"Debounce: {self.debounce:g}s",
            f"Changed only: {changed_label}",
            f"Changed files: {len(self.files)}",
            f"Planned extractors: {extractors}",
        ]

        if self.files:
            lines.append("File plans:")
            for file_plan in self.files:
                routes = ", ".join(file_plan.routes) or "unmatched"
                file_extractors = ", ".join(file_plan.extractors) or "none"
                lines.extend(
                    [
                        f"- {file_plan.path}",
                        f"  Event: {file_plan.event_type}",
                        f"  Routes: {routes}",
                        f"  Extractors: {file_extractors}",
                    ],
                )

        rebuilds = (
            "active facts by source file",
            "current graph tables",
            "change events",
        )
        lines.append("Planned updates:")
        lines.extend(f"- {rebuild}: planned" for rebuild in rebuilds)
        lines.append("Writes: skipped")
        return "\n".join(lines)


@dataclass(frozen=True)
class WatchUpdate:
    path: str
    event_type: str
    routes: tuple[str, ...]
    extractors: tuple[str, ...]
    facts_added: int = 0
    facts_removed: int = 0
    nodes_updated: int = 0
    warnings: int = 0
    skipped: bool = False


@dataclass(frozen=True)
class WatchUpdateResult:
    workspace: Workspace
    repository: RepositoryConfig | None
    profile: str | None
    debounce: float
    changed_only: bool
    updates: tuple[WatchUpdate, ...]

    def summary(self) -> str:
        repository_label = (
            "all repositories" if self.repository is None else self.repository.id
        )
        profile_label = self.profile or "all enabled extractors"
        changed_label = "yes" if self.changed_only else "no"
        lines = [
            "Watch update complete.",
            f"Workspace: {self.workspace.name} ({self.workspace.id})",
            f"Repository: {repository_label}",
            f"Profile: {profile_label}",
            f"Debounce: {self.debounce:g}s",
            f"Changed only: {changed_label}",
            f"Updates processed: {len(self.updates)}",
        ]
        if self.updates:
            lines.append("Change events:")
            for update in self.updates:
                extractors = ", ".join(update.extractors) or "none"
                routes = ", ".join(update.routes) or "unmatched"
                status = "skipped" if update.skipped else "rebuilt"
                lines.extend(
                    [
                        f"- {update.path}",
                        f"  Event: {update.event_type}",
                        f"  Routes: {routes}",
                        f"  Extractors run: {extractors}",
                        f"  Facts: +{update.facts_added}/-{update.facts_removed}",
                        f"  Nodes updated: {update.nodes_updated}",
                        f"  Warnings: {update.warnings}",
                        f"  Graph: {status}",
                    ],
                )
        return "\n".join(lines)


class _CachingGraphifyWatchAdapter:
    def __init__(self, inner: GraphifyWatchAdapter) -> None:
        self._inner = inner
        self._events_by_config: dict[
            ExtractorWatchConfig,
            tuple[GraphifyWatchEvent, ...],
        ] = {}
        self._errors_by_config: dict[ExtractorWatchConfig, Exception] = {}

    def watch_events(
        self,
        *,
        workspace_root: str,
        watch_config: ExtractorWatchConfig,
    ) -> Iterable[GraphifyWatchEvent]:
        if watch_config in self._errors_by_config:
            raise self._errors_by_config[watch_config]
        if watch_config not in self._events_by_config:
            try:
                self._events_by_config[watch_config] = tuple(
                    self._inner.watch_events(
                        workspace_root=workspace_root,
                        watch_config=watch_config,
                    ),
                )
            except Exception as error:
                self._errors_by_config[watch_config] = error
                raise
        return self._events_by_config[watch_config]


def watch_workspace(
    workspace_path: str = ".",
    *,
    dry_run: bool = False,
    repo: str | None = None,
    profile: str | None = None,
    debounce: float = 0.25,
    changed_only: bool = False,
    log: str | None = None,
    adapter: WatchAdapter | None = None,
    graphify_watch_adapter: GraphifyWatchAdapter | None = None,
) -> WatchDryRunPlan | WatchUpdateResult:
    if debounce < 0:
        raise ConfigError(
            "Watch debounce must be non-negative.",
            "Pass `--debounce` with a value of 0 or greater.",
        )

    config = load_workspace_config(workspace_path)
    workspace = default_single_repository_workspace(workspace_path, config=config)
    repository = _selected_repository(workspace, repo)
    events = (EmptyWatchAdapter() if adapter is None else adapter).changed_files(
        workspace_root=_repository_root(workspace, repository),
        debounce=debounce,
        changed_only=changed_only,
    )
    file_plans = _file_plans(
        events,
        workspace=workspace,
        repository=repository,
        profile=profile,
        config=config,
    )
    selected_extractors = tuple(
        dict.fromkeys(
            extractor for file_plan in file_plans for extractor in file_plan.extractors
        ),
    )
    if not dry_run:
        cached_graphify_watch_adapter = _graphify_watch_adapter_for_updates(
            config,
            selected_extractors,
            graphify_watch_adapter,
        )
        updates = _process_file_plans(
            workspace_path,
            file_plans,
            graphify_watch_adapter=cached_graphify_watch_adapter,
        )
        result = WatchUpdateResult(
            workspace=workspace,
            repository=repository,
            profile=profile,
            debounce=debounce,
            changed_only=changed_only,
            updates=updates,
        )
        _write_log(log, workspace.root_path, result.summary())
        return result

    plan = WatchDryRunPlan(
        workspace=workspace,
        repository=repository,
        profile=profile,
        debounce=debounce,
        changed_only=changed_only,
        files=file_plans,
        extractors=selected_extractors,
    )
    _write_log(log, workspace.root_path, plan.summary())
    return plan


def _process_file_plans(
    workspace_path: str,
    file_plans: tuple[WatchFilePlan, ...],
    *,
    graphify_watch_adapter: GraphifyWatchAdapter | None,
) -> tuple[WatchUpdate, ...]:
    skipped_updates = tuple(
        WatchUpdate(
            path=file_plan.path,
            event_type=file_plan.event_type,
            routes=file_plan.routes,
            extractors=file_plan.extractors,
            skipped=True,
        )
        for file_plan in file_plans
        if not file_plan.extractors
    )
    runnable_plans = tuple(
        file_plan for file_plan in file_plans if file_plan.extractors
    )
    if not runnable_plans:
        return skipped_updates

    result = replace_changed_file_active_facts(
        workspace_path,
        changed_file_paths=tuple(
            file_plan.path
            for file_plan in runnable_plans
            if not _is_deleted_event(file_plan.event_type)
        ),
        deleted_file_paths=tuple(
            file_plan.path
            for file_plan in runnable_plans
            if _is_deleted_event(file_plan.event_type)
        ),
        selected_extractors=tuple(
            dict.fromkeys(
                extractor
                for file_plan in runnable_plans
                for extractor in file_plan.extractors
            ),
        ),
        graphify_watch_adapter=graphify_watch_adapter,
    )
    return (
        *skipped_updates,
        _watch_update_from_replacement_batch(runnable_plans, result),
    )


def _watch_update_from_replacement_batch(
    file_plans: tuple[WatchFilePlan, ...],
    result: ChangedFileReplacementResult,
) -> WatchUpdate:
    return WatchUpdate(
        path=",".join(file_plan.path for file_plan in file_plans),
        event_type=_batch_event_type(file_plans),
        routes=tuple(
            dict.fromkeys(
                route for file_plan in file_plans for route in file_plan.routes
            )
        ),
        extractors=tuple(
            dict.fromkeys(
                extractor
                for file_plan in file_plans
                for extractor in file_plan.extractors
            ),
        ),
        facts_added=result.facts_added,
        facts_removed=result.facts_removed,
        nodes_updated=result.nodes_updated,
        warnings=len(result.warnings),
    )


def _batch_event_type(file_plans: tuple[WatchFilePlan, ...]) -> str:
    event_types = tuple(dict.fromkeys(file_plan.event_type for file_plan in file_plans))
    return event_types[0] if len(event_types) == 1 else "mixed"


def _is_deleted_event(event_type: str) -> bool:
    return event_type.lower() in {"deleted", "delete", "removed", "unlink"}


def _graphify_watch_adapter_for_updates(
    config: WorkspaceConfig,
    selected_extractors: tuple[str, ...],
    adapter: GraphifyWatchAdapter | None,
) -> GraphifyWatchAdapter | None:
    if "graphify" not in selected_extractors:
        return adapter
    if config.extractors.graphify.watch is None:
        return adapter
    return _CachingGraphifyWatchAdapter(
        GraphifyProcessAdapter() if adapter is None else adapter,
    )


def _write_log(log: str | None, workspace_root: str, text: str) -> None:
    if log is None:
        return
    log_path = Path(log)
    if not log_path.is_absolute():
        log_path = Path(workspace_root) / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text + "\n", encoding="utf-8")


def _selected_repository(
    workspace: Workspace, repo: str | None
) -> RepositoryConfig | None:
    if repo is None:
        return None
    for repository in workspace.repositories:
        if repository.id == repo:
            return repository
    raise ConfigError(
        f"Workspace repository `{repo}` is not configured.",
        "Use one of: "
        + ", ".join(repository.id for repository in workspace.repositories)
        + ".",
    )


def _repository_root(
    workspace: Workspace,
    repository: RepositoryConfig | None,
) -> str:
    if repository is None:
        return workspace.root_path
    return str(Path(workspace.root_path) / repository.path)


def _file_plans(
    events: tuple[WatchFileEvent, ...],
    *,
    workspace: Workspace,
    repository: RepositoryConfig | None,
    profile: str | None,
    config: WorkspaceConfig,
) -> tuple[WatchFilePlan, ...]:
    plans = []
    for event in events:
        source_path = _source_file_path(
            event.path,
            workspace=workspace,
            repository=repository,
        )
        routes = match_watch_routes(source_path, workspace=workspace)
        route_extractors = watch_extractors_for_paths(
            (source_path,), workspace=workspace
        )
        extractors = selected_watch_route_extractors(
            route_extractors,
            workspace=workspace,
            profile=profile,
            graphify_source_configured=_graphify_source_configured(config),
        )
        plans.append(
            WatchFilePlan(
                path=source_path,
                event_type=event.event_type,
                routes=tuple(route.id for route in routes),
                extractors=extractors,
            ),
        )
    return tuple(plans)


def _graphify_source_configured(config: WorkspaceConfig) -> bool:
    graphify_config = config.extractors.graphify
    return (
        graphify_config.json_path is not None
        or bool(graphify_config.command)
        or graphify_config.watch is not None
    )


def _source_file_path(
    path: str,
    *,
    workspace: Workspace,
    repository: RepositoryConfig | None,
) -> str:
    candidate = Path(path)
    workspace_root = Path(workspace.root_path).resolve(strict=False)
    if candidate.is_absolute():
        try:
            return (
                candidate.resolve(strict=False).relative_to(workspace_root).as_posix()
            )
        except ValueError:
            return candidate.as_posix().removeprefix("./")
    if repository is None or repository.path == ".":
        return candidate.as_posix().removeprefix("./")
    return (Path(repository.path) / candidate).as_posix().removeprefix("./")
