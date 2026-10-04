from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from code_scout.discovery import discover_workspace_files
from code_scout.domain.normalize import NormalizedGraphFacts
from code_scout.domain.resolver import resolve_active_facts
from code_scout.evidence.embeddings import LocalEmbeddingProvider
from code_scout.extractors import replacement_extractors_for_selection
from code_scout.extractors.graphify import GraphifyAdapter, GraphifyWatchAdapter
from code_scout.extractors.lsp import TypeScriptLSPAdapter
from code_scout.harvest import (
    ChangedFileReplacementResult,
    IncrementalFileWarning,
    harvest_incremental_evidence,
)
from code_scout.harvest.raw_evidence import raw_evidence_units_from_active_facts
from code_scout.harvest.revisions import (
    bind_harvested_revisions,
    capture_sources_before_harvest,
)
from code_scout.models.facts import ActiveFact
from code_scout.models.workspace import default_single_repository_workspace
from code_scout.storage import (
    ChangeEvent,
    RawEvidenceUnit,
    delete_stale_evidence_embeddings,
    deterministic_change_event_id,
    list_active_facts,
    list_raw_evidence_units_by_scan_scope,
    open_storage,
    record_change_event,
    refresh_evidence_embeddings,
    replace_active_facts_by_extractor_and_source_file,
    replace_current_graph,
    replace_raw_evidence_units_by_scan_scope,
    upsert_workspace,
)
from code_scout.workspace.config import load_workspace_config


def replace_changed_file_active_facts(
    workspace_path: str = ".",
    *,
    changed_file_paths: tuple[str | Path, ...],
    deleted_file_paths: tuple[str | Path, ...] = (),
    selected_extractors: tuple[str, ...],
    lsp_adapter: TypeScriptLSPAdapter | None = None,
    graphify_adapter: GraphifyAdapter | None = None,
    graphify_watch_adapter: GraphifyWatchAdapter | None = None,
) -> ChangedFileReplacementResult:
    config = load_workspace_config(workspace_path)
    workspace = default_single_repository_workspace(workspace_path, config=config)
    changed_source_files = tuple(
        dict.fromkeys(
            _source_file_for_changed_path(workspace.root_path, path)
            for path in changed_file_paths
        ),
    )
    deleted_source_files = tuple(
        dict.fromkeys(
            _source_file_for_changed_path(workspace.root_path, path)
            for path in deleted_file_paths
        ),
    )
    try:
        discovered_files = discover_workspace_files(workspace)
    except OSError as error:
        warning_path = _source_file_for_changed_path(
            workspace.root_path,
            error.filename or workspace.root_path,
        )
        return ChangedFileReplacementResult(
            facts_added=0,
            facts_removed=0,
            nodes_updated=0,
            warnings=(
                IncrementalFileWarning(
                    path=warning_path,
                    message="Changed file could not be read for incremental scanning.",
                    hint=(
                        "Facts were left unchanged because the event was not an "
                        "explicit deletion."
                    ),
                ),
            ),
        )
    changed_file_set = set(changed_source_files)
    deleted_file_set = set(deleted_source_files)
    changed_discovered_files = tuple(
        file for file in discovered_files if file.path in changed_file_set
    )
    source_captures = capture_sources_before_harvest(
        changed_discovered_files,
        max_bytes=config.source_retention.max_bytes,
    )
    discovered_changed_file_set = {file.path for file in changed_discovered_files}
    ambiguous_changed_files = tuple(
        source_file
        for source_file in changed_source_files
        if source_file not in discovered_changed_file_set
        and source_file not in deleted_file_set
    )
    ambiguous_file_warnings = tuple(
        IncrementalFileWarning(
            path=source_file,
            message="Changed file was not available for incremental scanning.",
            hint=(
                "Facts were left unchanged because the event was not an "
                "explicit deletion."
            ),
        )
        for source_file in ambiguous_changed_files
    )
    if ambiguous_changed_files == changed_source_files and not deleted_source_files:
        return ChangedFileReplacementResult(
            facts_added=0,
            facts_removed=0,
            nodes_updated=0,
            warnings=ambiguous_file_warnings,
        )
    selected = tuple(dict.fromkeys(selected_extractors))
    harvest = harvest_incremental_evidence(
        workspace=workspace,
        config=config,
        discovered_files=discovered_files,
        changed_discovered_files=changed_discovered_files,
        selected_extractors=selected,
        lsp_adapter=lsp_adapter,
        graphify_adapter=graphify_adapter,
        graphify_watch_adapter=graphify_watch_adapter,
    )
    incremental_warnings = (
        *harvest.warnings,
        *ambiguous_file_warnings,
    )
    facts_by_source = _active_facts_by_extractor_and_source_file(harvest.active_facts)
    replacement_keys = {
        (extractor, source_file)
        for source_file in (
            *(
                source_file
                for source_file in changed_source_files
                if source_file not in ambiguous_changed_files
            ),
            *deleted_source_files,
        )
        for extractor in replacement_extractors_for_selection(selected)
    } | set(facts_by_source)

    if not replacement_keys:
        return ChangedFileReplacementResult(
            facts_added=0,
            facts_removed=0,
            nodes_updated=0,
            warnings=incremental_warnings,
        )

    with open_storage(workspace_path) as connection:
        upsert_workspace(
            connection,
            id=workspace.id,
            name=workspace.name,
            root_path=workspace.root_path,
            schema_version=workspace.metadata.schema_version,
        )
        before_graph = resolve_active_facts(list_active_facts(connection))
        bound_facts = bind_harvested_revisions(
            connection,
            harvest.active_facts,
            before=source_captures,
            max_bytes=config.source_retention.max_bytes,
        )
        facts_by_source = _active_facts_by_extractor_and_source_file(bound_facts)
        facts_removed = 0
        facts_added = 0
        for extractor_name, source_file in sorted(replacement_keys):
            counts = replace_active_facts_by_extractor_and_source_file(
                connection,
                extractor=extractor_name,
                source_file=source_file,
                facts=facts_by_source.get((extractor_name, source_file), ()),
            )
            facts_removed += counts.removed
            facts_added += counts.inserted
        _replace_incremental_raw_evidence_units(
            connection,
            replacement_keys=replacement_keys,
            facts_by_source=facts_by_source,
        )
        after_graph = resolve_active_facts(list_active_facts(connection))
        replace_current_graph(connection, workspace.id, after_graph)
        nodes_updated = _updated_graph_node_count(before_graph, after_graph)
        timestamp = _scan_timestamp()
        event_type = _change_event_type(
            changed_source_files=changed_source_files,
            deleted_source_files=deleted_source_files,
        )
        event_file = _change_event_file(
            changed_source_files=changed_source_files,
            deleted_source_files=deleted_source_files,
        )
        change_event_id = deterministic_change_event_id(
            type=event_type,
            file=event_file,
            extractors_run=selected,
            facts_added=facts_added,
            facts_removed=facts_removed,
            nodes_updated=nodes_updated,
            outputs_rebuilt=False,
            timestamp=timestamp,
            metadata={},
        )
        record_change_event(
            connection,
            ChangeEvent(
                id=change_event_id,
                type=event_type,
                file=event_file,
                extractors_run=selected,
                facts_added=facts_added,
                facts_removed=facts_removed,
                nodes_updated=nodes_updated,
                outputs_rebuilt=False,
                timestamp=timestamp,
                metadata={},
            ),
        )
        connection.commit()

    return ChangedFileReplacementResult(
        facts_added=facts_added,
        facts_removed=facts_removed,
        nodes_updated=nodes_updated,
        change_event_id=change_event_id,
        warnings=incremental_warnings,
    )


def _scan_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _source_file_for_changed_path(workspace_root: str, path: str | Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return (
                candidate.resolve(strict=False)
                .relative_to(
                    Path(workspace_root).resolve(strict=False),
                )
                .as_posix()
            )
        except ValueError:
            return candidate.as_posix().removeprefix("./")
    return candidate.as_posix().removeprefix("./")


def _active_facts_by_extractor_and_source_file(
    facts: tuple[ActiveFact, ...],
) -> dict[tuple[str, str], tuple[ActiveFact, ...]]:
    grouped: dict[tuple[str, str], tuple[ActiveFact, ...]] = {}
    for fact in facts:
        key = (fact.extractor, fact.source_file)
        grouped[key] = (*grouped.get(key, ()), fact)
    return grouped


def _replace_incremental_raw_evidence_units(
    connection,
    *,
    replacement_keys: set[tuple[str, str]],
    facts_by_source: dict[tuple[str, str], tuple[ActiveFact, ...]],
) -> None:
    units_by_scope = _raw_evidence_units_by_scan_scope(
        fact for key in replacement_keys for fact in facts_by_source.get(key, ())
    )
    provider = LocalEmbeddingProvider()
    for provenance, source_file in sorted(replacement_keys):
        replace_raw_evidence_units_by_scan_scope(
            connection,
            provenance=provenance,
            source_file=source_file,
            evidence_units=units_by_scope.get((provenance, source_file), ()),
        )
        refresh_evidence_embeddings(
            connection,
            list_raw_evidence_units_by_scan_scope(
                connection,
                provenance=provenance,
                source_file=source_file,
            ),
            provider=provider,
        )
    delete_stale_evidence_embeddings(connection)


def _raw_evidence_units_by_scan_scope(
    facts: Iterable[ActiveFact],
) -> dict[tuple[str, str], list[RawEvidenceUnit]]:
    grouped: dict[tuple[str, str], list[RawEvidenceUnit]] = {}
    for unit in raw_evidence_units_from_active_facts(facts):
        key = (unit.provenance, unit.source_file)
        grouped.setdefault(key, []).append(unit)
    return grouped


def _updated_graph_node_count(
    before: NormalizedGraphFacts,
    after: NormalizedGraphFacts,
) -> int:
    before_fingerprints = _graph_node_fingerprints(before)
    after_fingerprints = _graph_node_fingerprints(after)
    keys = set(before_fingerprints) | set(after_fingerprints)
    return sum(
        1 for key in keys if before_fingerprints.get(key) != after_fingerprints.get(key)
    )


def _graph_node_fingerprints(graph: NormalizedGraphFacts) -> dict[tuple[str, str], str]:
    nodes = (
        ("repository", graph.repositories),
        ("file", graph.files),
        ("symbol", graph.symbols),
        ("service", graph.services),
        ("contract", graph.contracts),
        ("concept", graph.concepts),
        ("decision", graph.decisions),
    )
    fingerprints: dict[tuple[str, str], str] = {}
    for node_type, values in nodes:
        for value in values:
            fingerprints[(node_type, value.id)] = _stable_graph_value(value)
    for edge in graph.edges:
        edge_key = (
            "edge",
            ":".join(
                (
                    edge.source_type,
                    edge.source_id,
                    edge.relation_type,
                    edge.target_type,
                    edge.target_id,
                ),
            ),
        )
        fingerprints[edge_key] = _stable_graph_value(edge)
    return fingerprints


def _stable_graph_value(value: object) -> str:
    return json.dumps(asdict(value), sort_keys=True, separators=(",", ":"))


def _change_event_type(
    *,
    changed_source_files: tuple[str, ...],
    deleted_source_files: tuple[str, ...],
) -> str:
    if changed_source_files and deleted_source_files:
        return "incremental_mixed"
    if deleted_source_files:
        return "incremental_delete"
    return "incremental_change"


def _change_event_file(
    *,
    changed_source_files: tuple[str, ...],
    deleted_source_files: tuple[str, ...],
) -> str:
    return ",".join((*changed_source_files, *deleted_source_files))
