from __future__ import annotations

from datetime import UTC, datetime

from code_scout.discovery import discover_workspace_files
from code_scout.domain.resolver import resolve_active_facts
from code_scout.evidence.embeddings import EmbeddingProvider, LocalEmbeddingProvider
from code_scout.extractors import (
    EXTRACTOR_PROFILES as EXTRACTOR_PROFILES,
)
from code_scout.extractors import (
    replacement_extractors_for_selection,
    selected_scan_extractors,
)
from code_scout.extractors.graphify import (
    GraphifyAdapter,
    GraphifyWatchAdapter,
)
from code_scout.extractors.lsp import (
    TypeScriptLSPAdapter,
)
from code_scout.harvest import (
    ChangedFileReplacementResult as ChangedFileReplacementResult,
)
from code_scout.harvest import (
    ScanCounts,
    ScanResult,
    active_facts_from_scan_harvest,
    graphify_source_configured,
    harvest_scan_evidence,
)
from code_scout.harvest.raw_evidence import raw_evidence_units_from_active_facts
from code_scout.harvest.revisions import (
    bind_harvested_revisions,
    capture_sources_before_harvest,
)
from code_scout.models.graph import (
    Snapshot,
)
from code_scout.models.workspace import default_single_repository_workspace
from code_scout.pipeline.incremental import (
    replace_changed_file_active_facts as replace_changed_file_active_facts,
)
from code_scout.storage import (
    create_snapshot,
    delete_stale_evidence_embeddings,
    get_source_revision,
    graph_summary_counts,
    list_active_facts,
    list_raw_evidence_units,
    open_storage,
    refresh_evidence_embeddings,
    replace_active_facts_by_extractor_and_source_file,
    replace_current_graph,
    snapshot_payload_from_records,
    upsert_active_facts,
    upsert_raw_evidence_units,
    upsert_workspace,
)
from code_scout.workspace.config import load_workspace_config

__all__ = (
    "ChangedFileReplacementResult",
    "EXTRACTOR_PROFILES",
    "ScanCounts",
    "ScanResult",
    "replace_changed_file_active_facts",
    "scan_workspace",
)


def scan_workspace(
    workspace_path: str = ".",
    *,
    extractor: str | None = None,
    profile: str | None = None,
    created_at: datetime | None = None,
    lsp_adapter: TypeScriptLSPAdapter | None = None,
    graphify_adapter: GraphifyAdapter | None = None,
    graphify_watch_adapter: GraphifyWatchAdapter | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> ScanResult:
    config = load_workspace_config(workspace_path)
    workspace = default_single_repository_workspace(workspace_path, config=config)
    discovered_files = discover_workspace_files(workspace)
    source_captures = capture_sources_before_harvest(
        discovered_files,
        max_bytes=config.source_retention.max_bytes,
    )
    selected_extractors = selected_scan_extractors(
        workspace,
        config,
        extractor,
        profile,
        graphify_source_configured=graphify_source_configured,
    )
    harvest = harvest_scan_evidence(
        workspace=workspace,
        config=config,
        discovered_files=discovered_files,
        selected_extractors=selected_extractors,
        lsp_adapter=lsp_adapter,
        graphify_adapter=graphify_adapter,
        graphify_watch_adapter=graphify_watch_adapter,
        graphify_required=extractor == "graphify"
        or (profile is not None and "graphify" in selected_extractors),
    )
    timestamp = _scan_timestamp(created_at)
    snapshot = Snapshot(
        id=_snapshot_id(workspace.id, timestamp),
        workspace_id=workspace.id,
        created_at=timestamp,
    )

    with open_storage(workspace_path) as connection:
        connection.execute("BEGIN")
        try:
            upsert_workspace(
                connection,
                id=workspace.id,
                name=workspace.name,
                root_path=workspace.root_path,
                schema_version=workspace.metadata.schema_version,
            )
            existing_resolved_graph_facts = resolve_active_facts(
                list_active_facts(connection),
            )
            refreshed_active_facts = active_facts_from_scan_harvest(
                harvest,
                existing_resolved_graph_facts=existing_resolved_graph_facts,
            )
            refreshed_active_facts = bind_harvested_revisions(
                connection,
                refreshed_active_facts,
                before=source_captures,
                max_bytes=config.source_retention.max_bytes,
            )
            if extractor is None and profile is None:
                _delete_replaced_active_facts(connection, selected_extractors)
                upsert_active_facts(connection, refreshed_active_facts)
                refreshed_buckets = _fact_buckets(refreshed_active_facts)
            else:
                refreshed_buckets = _replace_refreshed_selected_buckets(
                    connection,
                    selected_extractors=selected_extractors,
                    discovered_files=discovered_files,
                    refreshed_active_facts=refreshed_active_facts,
                    warnings=harvest.warnings,
                )
            persisted_active_facts = list_active_facts(connection)
            _replace_raw_evidence_units(connection, persisted_active_facts)
            refresh_evidence_embeddings(
                connection,
                list_raw_evidence_units(connection),
                provider=embedding_provider or LocalEmbeddingProvider(),
            )
            delete_stale_evidence_embeddings(connection)
            resolved_graph_facts = resolve_active_facts(persisted_active_facts)
            replace_current_graph(connection, workspace.id, resolved_graph_facts)
            persisted_raw_evidence = list_raw_evidence_units(connection)
            revision_references = _referenced_source_revisions(
                connection, persisted_active_facts
            )
            scan_scope = _scan_scope(extractor, profile, selected_extractors)
            create_snapshot(
                connection,
                snapshot,
                metadata={
                    **scan_scope,
                    "extractor_count": len(selected_extractors),
                },
                payload=snapshot_payload_from_records(
                    active_facts=persisted_active_facts,
                    resolved_graph=resolved_graph_facts,
                    raw_evidence=persisted_raw_evidence,
                    revision_references=revision_references,
                    scan_scope=scan_scope,
                    coverage=_known_scan_coverage(
                        discovered_file_count=len(discovered_files),
                        refreshed_active_facts=refreshed_active_facts,
                        refreshed_buckets=refreshed_buckets,
                        persisted_active_facts=persisted_active_facts,
                        revision_count=len(revision_references),
                        raw_evidence_count=len(persisted_raw_evidence),
                    ),
                ),
            )
            persisted_counts = graph_summary_counts(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return ScanResult(
        workspace=workspace,
        snapshot_id=snapshot.id,
        extractor=extractor or profile,
        counts=ScanCounts(
            files=persisted_counts["files"],
            package_files=harvest.package_counts.package_files,
            repositories_detected=harvest.package_counts.repositories_detected,
            symbols=persisted_counts["symbols"],
            services=persisted_counts["services"],
            contracts=persisted_counts["contracts"],
            concepts=persisted_counts["concepts"],
            decisions=persisted_counts["decisions"],
            edges=persisted_counts["edges"],
        ),
        warnings=harvest.warnings,
    )


def _scan_timestamp(created_at: datetime | None) -> str:
    value = datetime.now(UTC) if created_at is None else created_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _snapshot_id(workspace_id: str, created_at: str) -> str:
    safe_timestamp = (
        created_at.replace(":", "").replace("-", "").replace(".", "").replace("+", "")
    )
    return f"snapshot:{workspace_id}:{safe_timestamp}"


def _replace_raw_evidence_units(connection, active_facts) -> None:
    connection.execute("DELETE FROM raw_evidence_units")
    upsert_raw_evidence_units(
        connection,
        raw_evidence_units_from_active_facts(active_facts),
    )


def _scan_scope(
    extractor: str | None,
    profile: str | None,
    selected_extractors: tuple[str, ...],
) -> dict[str, object]:
    return {
        "extractor": extractor or profile or "",
        "selection_type": (
            "extractor"
            if extractor is not None
            else "profile"
            if profile is not None
            else "all_enabled"
        ),
        "selection": extractor or profile,
        "selected_extractors": selected_extractors,
    }


def _known_scan_coverage(
    *,
    discovered_file_count: int,
    refreshed_active_facts,
    refreshed_buckets,
    persisted_active_facts,
    revision_count: int,
    raw_evidence_count: int,
) -> dict[str, object]:
    """Record what this scan observed without treating retained facts as refreshed."""
    refreshed_fact_ids = {fact.id for fact in refreshed_active_facts}
    return {
        "discovered_files": discovered_file_count,
        "refreshed_active_fact_ids": sorted(refreshed_fact_ids),
        "refreshed_source_files": sorted(
            {
                *(fact.source_file for fact in refreshed_active_facts),
                *(
                    source_file
                    for _repository_id, _extractor, source_file in refreshed_buckets
                ),
            }
        ),
        "inherited_active_fact_ids": sorted(
            fact.id
            for fact in persisted_active_facts
            if fact.id not in refreshed_fact_ids
        ),
        "retained_revisions": revision_count,
        "raw_evidence_units": raw_evidence_count,
    }


def _referenced_source_revisions(connection, active_facts):
    revision_ids = sorted(
        {
            evidence.metadata.get("source_revision_id")
            for fact in active_facts
            for evidence in fact.evidence
            if isinstance(evidence.metadata.get("source_revision_id"), str)
        }
    )
    revisions = tuple(
        revision
        for revision_id in revision_ids
        if (revision := get_source_revision(connection, revision_id)) is not None
    )
    if len(revisions) != len(revision_ids):
        raise ValueError("Active fact references a missing source revision")
    return revisions


def _delete_replaced_active_facts(connection, selected_extractors) -> None:
    replaced = replacement_extractors_for_selection(selected_extractors)
    placeholders = ", ".join("?" for _item in replaced)
    connection.execute(
        f"DELETE FROM facts WHERE extractor IN ({placeholders})",  # noqa: S608
        replaced,
    )


def _replace_refreshed_selected_buckets(
    connection,
    *,
    selected_extractors,
    discovered_files,
    refreshed_active_facts,
    warnings,
):
    """Replace only selected, successfully observed source buckets.

    A bucket includes the repository because repository-relative paths can repeat
    in multi-repository workspaces.  Existing selected facts with a still
    discoverable source represent successful empty extractor output.
    """
    selected = set(replacement_extractors_for_selection(selected_extractors))
    discovered_sources = {(file.repository_id, file.path) for file in discovered_files}
    failed_buckets = {
        (warning.repository_id, warning.path, extractor_name)
        for warning in warnings
        if hasattr(warning, "repository_id") and hasattr(warning, "path")
        for extractor_name in _warning_failed_extractors(warning)
    }
    failed_extractors = {
        "graphify"
        for warning in warnings
        if type(warning).__name__ == "GraphifyWarning"
    }
    existing = list_active_facts(connection)
    buckets = {
        bucket
        for fact in (*existing, *refreshed_active_facts)
        if (bucket := _fact_bucket(fact)) is not None
        and bucket[1] in selected
        and bucket[1] not in failed_extractors
        and (
            fact in refreshed_active_facts
            or (bucket[0], bucket[2]) in discovered_sources
        )
        and (bucket[0], bucket[2], bucket[1]) not in failed_buckets
    }
    facts_by_bucket = {
        bucket: tuple(
            fact for fact in refreshed_active_facts if _fact_bucket(fact) == bucket
        )
        for bucket in buckets
    }
    for repository_id, extractor_name, source_file in sorted(buckets):
        replace_active_facts_by_extractor_and_source_file(
            connection,
            extractor=extractor_name,
            source_file=source_file,
            repository_id=repository_id,
            facts=facts_by_bucket[(repository_id, extractor_name, source_file)],
        )
    # Workspace-level facts do not have a repository-qualified source bucket,
    # but are rebuilt on every scan and are required before current graph rows.
    upsert_active_facts(
        connection,
        (fact for fact in refreshed_active_facts if _fact_bucket(fact) is None),
    )
    return buckets


def _fact_buckets(facts):
    return {bucket for fact in facts if (bucket := _fact_bucket(fact)) is not None}


def _fact_bucket(fact):
    repository_id = _fact_repository_id(fact)
    if repository_id is None:
        return None
    return (repository_id, fact.extractor, fact.source_file)


def _fact_repository_id(fact):
    for evidence in fact.evidence:
        repository_id = evidence.metadata.get("repository_id")
        if isinstance(repository_id, str):
            return repository_id
    repository_id = fact.properties.get("repository_id")
    return repository_id if isinstance(repository_id, str) else None


def _warning_failed_extractors(warning):
    return {
        "OpenAPIWarning": ("openapi",),
        "AsyncAPIWarning": ("asyncapi",),
    }.get(type(warning).__name__, ())
