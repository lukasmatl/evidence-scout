from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from code_scout.core.errors import ConfigError, StorageError
from code_scout.evidence.assessment import (
    assess_legacy_evidence,
    assess_raw_evidence,
    combine_assessments,
)
from code_scout.evidence.embeddings import EmbeddingProvider
from code_scout.evidence.query import (
    SemanticEvidenceMatch,
    search_semantic_evidence,
)
from code_scout.exporters.output_warnings import resolver_conflict_warnings
from code_scout.storage import database_path
from code_scout.workspace.config import load_workspace_config

if TYPE_CHECKING:
    from code_scout.engines.dede import DedeImportResult


@dataclass(frozen=True)
class EvidencePackLimits:
    """Hard limits applied while compiling model-facing external context."""

    max_evidence_units: int = 10
    max_map_entries: int = 40
    max_characters: int = 16_000
    max_characters_per_file: int = 3_000

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")


@dataclass(frozen=True)
class PackedEvidence:
    id: str
    rank: int
    score: float
    owner_type: str
    owner_id: str
    source_file: str
    start_line: int | None
    end_line: int | None
    text: str
    confidence: float
    provenance: str
    scoring_reasons: tuple[str, ...]
    related_symbols: tuple[str, ...]
    related_files: tuple[str, ...]
    relationship_ids: tuple[str, ...]
    assessment: dict[str, Any] = field(default_factory=dict)
    repository_id: str | None = None
    source_revision_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.assessment:
            object.__setattr__(
                self,
                "assessment",
                assess_legacy_evidence(
                    {},
                    extractor_provenance=(self.provenance,),
                ).to_dict(),
            )


@dataclass(frozen=True)
class PackedRelationship:
    id: str
    source_type: str
    source_id: str
    relation_type: str
    target_type: str
    target_id: str
    confidence: float
    provenance: tuple[str, ...]
    source_provenance: tuple[PackedSourceProvenance, ...] = ()
    certainty: str | None = None
    unresolved: bool = False
    metadata: tuple[tuple[str, Any], ...] = ()
    assessment: dict[str, Any] = field(default_factory=dict)
    supporting_evidence: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.assessment:
            _set_packed_assessment(self)


@dataclass(frozen=True)
class PackedSourceProvenance:
    source_file: str
    start_line: int | None
    end_line: int | None
    extractor: str
    confidence: float | None
    assessment: dict[str, Any] = field(default_factory=dict)
    repository_id: str | None = None
    source_revision_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.assessment:
            object.__setattr__(
                self,
                "assessment",
                assess_legacy_evidence(
                    {},
                    extractor_provenance=(self.extractor,),
                ).to_dict(),
            )


@dataclass(frozen=True)
class RepositoryMapEntry:
    kind: str
    id: str
    path: str | None
    name: str
    signature: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    relationship: str | None = None
    source_provenance: tuple[PackedSourceProvenance, ...] = ()
    certainty: str | None = None
    unresolved: bool = False
    metadata: tuple[tuple[str, Any], ...] = ()
    assessment: dict[str, Any] = field(default_factory=dict)
    supporting_evidence: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.assessment:
            _set_packed_assessment(self)


@dataclass(frozen=True)
class PackedImpactPath:
    id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    certainty: str
    unresolved: bool
    provenance: str = "dede"


@dataclass(frozen=True)
class EvidencePack:
    query: str
    evidence: tuple[PackedEvidence, ...]
    relationships: tuple[PackedRelationship, ...]
    repository_map: tuple[RepositoryMapEntry, ...]
    needs_review: tuple[dict[str, Any], ...]
    context: str
    truncated: bool
    impact_paths: tuple[PackedImpactPath, ...] = ()
    warnings: tuple[dict[str, str], ...] = ()

    @property
    def character_count(self) -> int:
        return len(self.context)

    def render(self) -> str:
        return self.context

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "evidence": [asdict(item) for item in self.evidence],
            "relationships": [asdict(item) for item in self.relationships],
            "repository_map": [asdict(item) for item in self.repository_map],
            "needs_review": list(self.needs_review),
            "impact_paths": [asdict(item) for item in self.impact_paths],
            "warnings": list(self.warnings),
            "context": self.context,
            "truncated": self.truncated,
        }


def _assess_record(item: dict[str, Any]):
    metadata = dict(item.get("metadata") or {})
    if item.get("assessment"):
        metadata["assessment"] = item["assessment"]
    return assess_legacy_evidence(
        metadata,
        extractor_provenance=(item.get("extractor_name", ""),),
        certainty=metadata.get("certainty"),
    )


def _set_packed_assessment(item: PackedRelationship | RepositoryMapEntry) -> None:
    metadata = dict(item.metadata)
    assessments = [_assess_record(record) for record in item.supporting_evidence]
    assessments.extend(
        assess_legacy_evidence({"assessment": source.assessment})
        for source in item.source_provenance
    )
    provenance = getattr(item, "provenance", ())
    if not provenance and "dede_type" in metadata:
        provenance = ("dede",)
    if not assessments or metadata or item.certainty or item.unresolved:
        assessments.append(
            assess_legacy_evidence(
                metadata,
                extractor_provenance=provenance,
                certainty=item.certainty,
                unresolved=item.unresolved,
            )
        )
    object.__setattr__(item, "assessment", combine_assessments(assessments).to_dict())


def _raw_assessment(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    if "resolved_assessment" in row.keys():
        return row["resolved_assessment"]
    return assess_raw_evidence(
        json.loads(row["metadata_json"]), str(row["provenance"])
    ).to_dict()


def compile_evidence_pack(
    workspace_path: str | Path = ".",
    *,
    question: str,
    limits: EvidencePackLimits | None = None,
    provider: EmbeddingProvider | None = None,
) -> EvidencePack:
    """Compile bounded, scan-backed context without mutating the workspace."""

    if not question.strip():
        raise ValueError("question must not be empty")
    selected_limits = limits or EvidencePackLimits()
    workspace = Path(workspace_path).resolve()

    with closing(_open_scanned_storage_read_only(workspace)) as connection:
        candidate_limit = _semantic_candidate_count(connection)
        matches = search_semantic_evidence(
            connection,
            question=question,
            limit=candidate_limit,
            provider=provider,
        )
        raw_by_id = _load_raw_evidence(connection, matches)
        all_valid_matches, rejected = _workspace_matches(workspace, matches)
        valid_matches = all_valid_matches[: selected_limits.max_evidence_units]
        relationships = _load_relationships(connection, workspace, valid_matches)
        packed_evidence, file_characters, evidence_truncated = _pack_evidence(
            valid_matches,
            raw_by_id,
            selected_limits,
        )
        map_entries = _build_repository_map(
            connection,
            workspace,
            valid_matches,
            relationships,
            selected_limits,
            file_characters,
        )
        review_items = _load_needs_review(connection, rejected)
        dede = _load_dede_enrichment(
            connection,
            workspace,
            selected_limits,
            map_entries_used=len(map_entries),
        )

    dede_relationships, dede_map, impact_paths = _pack_dede(dede)
    relationships = (*relationships, *dede_relationships)
    map_entries = (*map_entries, *dede_map)
    review_items = (*review_items, *dede.unresolved)

    payload = {
        "query": question,
        "evidence": [asdict(item) for item in packed_evidence],
        "repository_map": [asdict(item) for item in map_entries],
        "relationships": [asdict(item) for item in relationships],
        "needs_review": list(review_items),
        "impact_paths": [asdict(item) for item in impact_paths],
        "warnings": list(dede.warnings),
    }
    rendered, character_truncated = _bounded_context(
        payload, selected_limits.max_characters
    )

    return EvidencePack(
        query=question,
        evidence=packed_evidence,
        relationships=relationships,
        repository_map=map_entries,
        needs_review=review_items,
        context=rendered,
        truncated=evidence_truncated
        or character_truncated
        or len(all_valid_matches) > len(valid_matches)
        or len(valid_matches) < len(matches),
        impact_paths=impact_paths,
        warnings=dede.warnings,
    )


def _bounded_context(payload: dict[str, Any], max_characters: int) -> tuple[str, bool]:
    def render(value: dict[str, Any]) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    rendered = render(payload)
    if len(rendered) <= max_characters:
        return rendered, False

    bounded = {**payload, "context_truncated": True}
    # Relationship map entries duplicate the full relationships below. Prefer
    # retaining their provider evidence within the existing character budget.
    bounded["repository_map"] = list(bounded["repository_map"])
    for entry in reversed(tuple(bounded["repository_map"])):
        if entry["kind"] == "relationship":
            bounded["repository_map"].remove(entry)
            rendered = render(bounded)
            if len(rendered) <= max_characters:
                return rendered, True
    for key in (
        "relationships",
        "needs_review",
        "impact_paths",
        "warnings",
        "evidence",
        "repository_map",
    ):
        values = list(bounded[key])
        bounded[key] = values
        while values:
            rendered = render(bounded)
            if len(rendered) <= max_characters:
                return rendered, True
            values.pop()

    rendered = render(bounded)
    if len(rendered) <= max_characters:
        return rendered, True
    minimal = '{"context_truncated":true}'
    return (minimal if len(minimal) <= max_characters else "{}"), True


def _load_dede_enrichment(
    connection: sqlite3.Connection,
    workspace: Path,
    limits: EvidencePackLimits,
    *,
    map_entries_used: int,
) -> DedeImportResult:
    from code_scout.engines.dede import (
        DedeImportResult,
        import_configured_dede_graph,
    )

    if not _is_dotnet_workspace(connection):
        return DedeImportResult()
    try:
        config = load_workspace_config(workspace).enrichment.dede
    except ConfigError:
        return DedeImportResult()
    if not config.enabled:
        return DedeImportResult()
    return import_configured_dede_graph(
        workspace,
        config,
        max_nodes=max(0, limits.max_map_entries - map_entries_used),
        max_edges=limits.max_map_entries * 4,
        max_paths=limits.max_map_entries,
    )


def _is_dotnet_workspace(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM files
        WHERE lower(path) LIKE '%.cs'
           OR lower(path) LIKE '%.csproj'
           OR lower(path) LIKE '%.sln'
           OR lower(path) LIKE '%.slnx'
           OR lower(path) LIKE '%.fsproj'
           OR lower(path) LIKE '%.vbproj'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _pack_dede(
    imported: DedeImportResult,
) -> tuple[
    tuple[PackedRelationship, ...],
    tuple[RepositoryMapEntry, ...],
    tuple[PackedImpactPath, ...],
]:
    from code_scout.engines.dede import dede_confidence

    node_sources = {node.id: node.source for node in imported.nodes}
    map_entries = tuple(
        RepositoryMapEntry(
            kind=_dede_node_kind(node.type),
            id=node.id,
            path=node.source.source_file if node.source is not None else None,
            name=node.display_name,
            start_line=node.source.start_line if node.source is not None else None,
            end_line=node.source.end_line if node.source is not None else None,
            source_provenance=_dede_source_provenance(node.source, node.certainty),
            certainty=node.certainty,
            metadata=(
                ("dede_type", node.type),
                ("repository_name", node.repository_name),
                ("project_name", node.project_name),
                *node.metadata,
            ),
        )
        for node in imported.nodes
    )
    relationships = []
    for edge in imported.edges:
        source = (
            edge.source
            or node_sources.get(edge.source_id)
            or node_sources.get(edge.target_id)
        )
        provenance = (
            (
                PackedSourceProvenance(
                    source.source_file,
                    source.start_line,
                    source.end_line,
                    "dede",
                    dede_confidence(edge.certainty),
                ),
            )
            if source is not None
            else ()
        )
        relationships.append(
            PackedRelationship(
                id=f"dede:{edge.id}",
                source_type="dede_node",
                source_id=edge.source_id,
                relation_type=edge.type,
                target_type="dede_node",
                target_id=edge.target_id,
                confidence=dede_confidence(edge.certainty),
                provenance=("dede",),
                source_provenance=provenance,
                certainty=edge.certainty,
                unresolved=edge.unresolved,
                metadata=edge.metadata,
            )
        )
    impact_paths = tuple(
        PackedImpactPath(
            path.id,
            path.node_ids,
            tuple(
                edge_id if edge_id.startswith("dede:") else f"dede:{edge_id}"
                for edge_id in path.edge_ids
            ),
            path.certainty,
            path.unresolved,
        )
        for path in imported.impact_paths
    )
    return tuple(relationships), map_entries, impact_paths


def _dede_source_provenance(
    source: Any, certainty: str
) -> tuple[PackedSourceProvenance, ...]:
    from code_scout.engines.dede import dede_confidence

    if source is None:
        return ()
    return (
        PackedSourceProvenance(
            source.source_file,
            source.start_line,
            source.end_line,
            "dede",
            dede_confidence(certainty),
        ),
    )


def _dede_node_kind(node_type: str) -> str:
    normalized = node_type.casefold().replace("_", "").replace("-", "")
    if normalized in {
        "class",
        "controller",
        "endpoint",
        "interface",
        "method",
        "minimalapi",
        "type",
    }:
        return "symbol"
    return f"dede:{node_type}"


def _open_scanned_storage_read_only(workspace: Path) -> sqlite3.Connection:
    path = database_path(workspace)
    if not path.is_file():
        raise StorageError(
            f"Workspace has not been scanned: database not found at {path}.",
            "Run `code-scout scan` for this workspace before using verify.",
        )
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        snapshot = connection.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        if "connection" in locals():
            connection.close()
        raise StorageError(
            f"Workspace scan database could not be read at {path}.",
            "Run `code-scout scan` to rebuild the workspace database.",
        ) from exc
    if snapshot is None:
        connection.close()
        raise StorageError(
            "Workspace has not been scanned: no scan snapshot is available.",
            "Run `code-scout scan` for this workspace before using verify.",
        )
    return connection


def _load_raw_evidence(
    connection: sqlite3.Connection,
    matches: tuple[SemanticEvidenceMatch, ...],
) -> dict[str, dict[str, Any]]:
    ids = sorted(
        match.evidence_unit_id
        for match in matches
        if not match.evidence_unit_id.startswith("lexical:")
    )
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT id, owner_entity_type, owner_entity_id, source_file,
               provenance, confidence, metadata_json
        FROM raw_evidence_units
        WHERE id IN ({placeholders})
        ORDER BY id
        """,
        ids,
    ).fetchall()
    result = {str(row["id"]): dict(row) for row in rows}
    # Read the existing graph projection; raw observations keep their identities.
    tables = {
        "service": "services",
        "contract": "contracts",
        "concept": "concepts",
        "decision": "decisions",
    }
    owners: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in result.values():
        table = tables.get(row["owner_entity_type"])
        if table is None:
            continue
        key = (table, row["owner_entity_id"])
        if key not in owners:
            owner = connection.execute(
                f"SELECT evidence_json FROM {table} WHERE id = ?", (key[1],)
            ).fetchone()
            owners[key] = json.loads(owner["evidence_json"]) if owner else []
        supporting = [
            item
            for item in owners[key]
            if item.get("extractor_name") == row["provenance"]
            and item.get("file_path") == row["source_file"]
        ]
        if supporting:
            row["resolved_assessment"] = combine_assessments(
                (
                    assess_legacy_evidence({"assessment": _raw_assessment(row)}),
                    *(_assess_record(item) for item in supporting),
                )
            ).to_dict()
    return result


def _semantic_candidate_count(connection: sqlite3.Connection) -> int:
    tables = (
        "raw_evidence_units",
        "services",
        "contracts",
        "concepts",
        "decisions",
        "files",
        "symbols",
    )
    return max(
        1,
        sum(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        ),
    )


def _workspace_matches(
    workspace: Path,
    matches: tuple[SemanticEvidenceMatch, ...],
) -> tuple[tuple[SemanticEvidenceMatch, ...], tuple[dict[str, Any], ...]]:
    valid: list[SemanticEvidenceMatch] = []
    rejected: list[dict[str, Any]] = []
    for match in matches:
        normalized, reason = _workspace_file(workspace, match.source_file)
        if normalized is None:
            rejected.append(
                {
                    "code": "evidence-outside-workspace",
                    "evidence_unit_id": match.evidence_unit_id,
                    "source_file": match.source_file,
                    "reason": reason,
                }
            )
            continue
        valid.append(
            SemanticEvidenceMatch(
                **{
                    **match.__dict__,
                    "source_file": normalized,
                }
            )
        )
    return tuple(valid), tuple(rejected)


def _workspace_file(workspace: Path, value: str) -> tuple[str | None, str]:
    raw = value.strip().replace("\\", "/")
    parsed = PurePosixPath(raw)
    if not raw or parsed == PurePosixPath("."):
        return None, "source path is empty"
    if ".." in parsed.parts:
        return None, "source path contains traversal"
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(workspace)
        except ValueError:
            return None, "source path is outside the workspace"
    else:
        relative = Path(*parsed.parts)
        candidate = workspace / relative
    try:
        candidate.resolve(strict=False).relative_to(workspace)
    except (OSError, RuntimeError, ValueError):
        return None, "source path resolves outside the workspace"
    if not candidate.is_file():
        return None, "source file does not exist"
    return relative.as_posix(), ""


def _load_relationships(
    connection: sqlite3.Connection,
    workspace: Path,
    matches: tuple[SemanticEvidenceMatch, ...],
) -> tuple[PackedRelationship, ...]:
    ids = sorted(
        {
            int(value.removeprefix("relationship:"))
            for match in matches
            for value in match.relationships
            if value.startswith("relationship:")
            and value.removeprefix("relationship:").isdigit()
        }
    )
    if not ids:
        return ()
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT id, source_type, source_id, target_type, target_id,
               relation_type, confidence, evidence_json
        FROM edges WHERE id IN ({placeholders}) ORDER BY id
        """,
        ids,
    ).fetchall()
    packed = []
    for row in rows:
        evidence = json.loads(row["evidence_json"])
        provenance = tuple(
            sorted(
                {
                    str(item.get("extractor_name"))
                    for item in evidence
                    if isinstance(item, dict) and item.get("extractor_name")
                }
            )
        )
        packed.append(
            PackedRelationship(
                id=f"relationship:{row['id']}",
                source_type=str(row["source_type"]),
                source_id=str(row["source_id"]),
                relation_type=str(row["relation_type"]),
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
                confidence=float(row["confidence"]),
                provenance=provenance,
                supporting_evidence=tuple(
                    sorted(
                        (item for item in evidence if isinstance(item, dict)),
                        key=lambda item: json.dumps(item, sort_keys=True),
                    )
                ),
                source_provenance=_relationship_source_provenance(workspace, evidence),
            )
        )
    return tuple(packed)


def _relationship_source_provenance(
    workspace: Path, evidence: Any
) -> tuple[PackedSourceProvenance, ...]:
    if not isinstance(evidence, list):
        return ()
    sources: list[PackedSourceProvenance] = []
    seen: set[tuple[Any, ...]] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        normalized, _reason = _workspace_file(workspace, str(item.get("file_path", "")))
        if normalized is None:
            continue
        start_line = item.get("start_line")
        end_line = item.get("end_line")
        if not _source_range_exists(workspace / normalized, start_line, end_line):
            continue
        key = (
            normalized,
            start_line,
            end_line,
            str(item.get("extractor_name", "")),
            item.get("confidence"),
            json.dumps(_assess_record(item).to_dict(), sort_keys=True),
        )
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            PackedSourceProvenance(
                source_file=normalized,
                start_line=start_line,
                end_line=end_line,
                extractor=str(item.get("extractor_name", "")),
                confidence=item.get("confidence"),
                assessment=_assess_record(item).to_dict(),
                **_evidence_revision_fields(item),
            )
        )
    return tuple(
        sorted(sources, key=lambda source: json.dumps(asdict(source), sort_keys=True))
    )


def _source_range_exists(path: Path, start_line: Any, end_line: Any) -> bool:
    if start_line is None and end_line is None:
        return True
    if (
        not isinstance(start_line, int)
        or not isinstance(end_line, int)
        or isinstance(start_line, bool)
        or isinstance(end_line, bool)
        or start_line < 1
        or end_line < start_line
    ):
        return False
    try:
        with path.open(encoding="utf-8") as source:
            line_count = sum(1 for _line in source)
    except (OSError, UnicodeError):
        return False
    return end_line <= line_count


def _build_repository_map(
    connection: sqlite3.Connection,
    workspace: Path,
    matches: tuple[SemanticEvidenceMatch, ...],
    relationships: tuple[PackedRelationship, ...],
    limits: EvidencePackLimits,
    initial_file_characters: dict[str, int],
) -> tuple[RepositoryMapEntry, ...]:
    ranked_paths: dict[str, int] = {}
    related_symbol_ids: set[str] = set()
    related_file_ids: set[str] = set()
    for rank, match in enumerate(matches, start=1):
        ranked_paths.setdefault(match.source_file, rank)
        related_symbol_ids.update(match.related_symbols)
        related_file_ids.update(match.related_files)
        if match.owner_entity_type == "symbol":
            related_symbol_ids.add(match.owner_entity_id)
        if match.owner_entity_type == "file":
            related_file_ids.add(match.owner_entity_id)

    rows = connection.execute(
        """
        SELECT symbols.id, symbols.file_id, symbols.name, symbols.kind,
               symbols.qualified_name, symbols.start_line, symbols.end_line,
               files.path
        FROM symbols JOIN files ON files.id = symbols.file_id
        ORDER BY files.path, symbols.start_line, symbols.id
        """
    ).fetchall()
    symbol_rows = []
    for row in rows:
        normalized, _reason = _workspace_file(workspace, str(row["path"]))
        if normalized is None:
            continue
        if (
            str(row["id"]) in related_symbol_ids
            or str(row["file_id"]) in related_file_ids
            or normalized in ranked_paths
        ):
            ranked_paths.setdefault(normalized, len(matches) + 1)
            symbol_rows.append((row, normalized))

    symbol_provenance = _symbol_source_provenance(
        connection,
        workspace,
        tuple(str(row["id"]) for row, _path in symbol_rows),
    )
    file_rows = connection.execute("SELECT id, path FROM files ORDER BY id").fetchall()
    file_ids = {str(row["path"]): str(row["id"]) for row in file_rows}
    file_provenance = _symbol_source_provenance(
        connection,
        workspace,
        tuple(file_ids[path] for path in ranked_paths if path in file_ids),
        owner_type="file",
    )
    candidates: list[tuple[tuple[Any, ...], RepositoryMapEntry]] = []
    for path, rank in ranked_paths.items():
        candidates.append(
            (
                (0, rank, path, 0, ""),
                RepositoryMapEntry(
                    "file",
                    f"file:{path}",
                    path,
                    path,
                    source_provenance=file_provenance.get(file_ids.get(path, ""), ()),
                ),
            )
        )
    symbol_positions: dict[str, int] = {}
    for row, path in symbol_rows:
        signature = _source_signature(workspace / path, row["start_line"])
        label = str(row["qualified_name"] or row["name"])
        symbol_position = symbol_positions.get(path, 0)
        symbol_positions[path] = symbol_position + 1
        candidates.append(
            (
                (
                    1,
                    symbol_position,
                    ranked_paths[path],
                    path,
                    int(row["start_line"] or 0),
                    row["id"],
                ),
                RepositoryMapEntry(
                    "symbol",
                    str(row["id"]),
                    path,
                    label,
                    signature,
                    row["start_line"],
                    row["end_line"],
                    source_provenance=symbol_provenance.get(str(row["id"]), ()),
                ),
            )
        )
    for relationship in relationships:
        description = (
            f"{relationship.source_type}:{relationship.source_id} "
            f"--{relationship.relation_type}--> "
            f"{relationship.target_type}:{relationship.target_id}"
        )
        candidates.append(
            (
                (2, len(matches) + 2, relationship.id, 0, ""),
                RepositoryMapEntry(
                    "relationship",
                    relationship.id,
                    None,
                    relationship.relation_type,
                    relationship=description,
                    assessment=relationship.assessment,
                    certainty=relationship.certainty,
                    unresolved=relationship.unresolved,
                    metadata=relationship.metadata,
                ),
            )
        )

    selected: list[RepositoryMapEntry] = []
    file_characters = dict(initial_file_characters)
    for _key, entry in sorted(candidates, key=lambda item: item[0]):
        if len(selected) >= limits.max_map_entries:
            break
        if entry.path is not None:
            used = file_characters.get(entry.path, 0)
            signature = entry.signature or ""
            remaining = limits.max_characters_per_file - used
            if signature and remaining <= 0:
                entry = replace(entry, signature=None)
                signature = ""
            elif len(signature) > remaining:
                entry = replace(entry, signature=signature[:remaining])
                signature = entry.signature or ""
            contribution = len(signature)
            file_characters[entry.path] = used + contribution
        selected.append(entry)
    return tuple(selected)


def _symbol_source_provenance(
    connection: sqlite3.Connection,
    workspace: Path,
    symbol_ids: tuple[str, ...],
    *,
    owner_type: str = "symbol",
) -> dict[str, tuple[PackedSourceProvenance, ...]]:
    if not symbol_ids:
        return {}
    placeholders = ",".join("?" for _ in symbol_ids)
    rows = connection.execute(
        f"""
        SELECT owner_entity_id, source_file, start_line, end_line,
               provenance, confidence, metadata_json
        FROM raw_evidence_units
        WHERE owner_entity_type = ?
          AND owner_entity_id IN ({placeholders})
        ORDER BY owner_entity_id, provenance, source_file, start_line, end_line
        """,
        (owner_type, *symbol_ids),
    ).fetchall()
    grouped: dict[str, list[PackedSourceProvenance]] = {}
    for row in rows:
        source = _validated_source_provenance(
            workspace,
            source_file=str(row["source_file"]),
            start_line=row["start_line"],
            end_line=row["end_line"],
            extractor=str(row["provenance"]),
            confidence=float(row["confidence"]),
            metadata_json=str(row["metadata_json"]),
        )
        if source is not None:
            grouped.setdefault(str(row["owner_entity_id"]), []).append(
                replace(source, assessment=_raw_assessment(row))
            )
    return {key: tuple(value) for key, value in grouped.items()}


def _validated_source_provenance(
    workspace: Path,
    *,
    source_file: str,
    start_line: Any,
    end_line: Any,
    extractor: str,
    confidence: float | None,
    metadata_json: str = "{}",
) -> PackedSourceProvenance | None:
    normalized, _reason = _workspace_file(workspace, source_file)
    if normalized is None or not _source_range_exists(
        workspace / normalized, start_line, end_line
    ):
        return None
    return PackedSourceProvenance(
        source_file=normalized,
        start_line=start_line,
        end_line=end_line,
        extractor=extractor,
        confidence=confidence,
        **_metadata_revision_fields(metadata_json),
    )


def _evidence_revision_fields(evidence: dict[str, Any]) -> dict[str, str | None]:
    metadata = evidence.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return _revision_values(metadata)


def _metadata_revision_fields(metadata_json: str) -> dict[str, str | None]:
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError):
        return {}
    return _revision_values(metadata) if isinstance(metadata, dict) else {}


def _revision_values(metadata: dict[str, Any]) -> dict[str, str | None]:
    values = {
        key: metadata.get(key)
        for key in ("repository_id", "source_revision_id", "content_sha256")
    }
    return (
        values
        if all(isinstance(value, str) and value for value in values.values())
        else {}
    )


def _source_signature(path: Path, start_line: int | None) -> str | None:
    if start_line is None or start_line < 1:
        return None
    try:
        with path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, start=1):
                if number == start_line:
                    value = line.strip()
                    return value or None
    except (OSError, UnicodeError):
        return None
    return None


def _load_needs_review(
    connection: sqlite3.Connection,
    rejected: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    node_rows: list[tuple[str, tuple[dict[str, Any], ...]]] = []
    for node_type, table in (
        ("service", "services"),
        ("contract", "contracts"),
        ("decision", "decisions"),
    ):
        rows = connection.execute(
            f"SELECT id, evidence_json FROM {table} ORDER BY id"
        ).fetchall()
        node_rows.append(
            (
                node_type,
                tuple(
                    {"id": row["id"], "evidence": json.loads(row["evidence_json"])}
                    for row in rows
                ),
            )
        )
    edge_rows = connection.execute(
        """
        SELECT source_type, source_id, target_type, target_id, relation_type,
               evidence_json FROM edges
        ORDER BY source_type, source_id, relation_type, target_type, target_id
        """
    ).fetchall()
    edges = tuple(
        {
            **{key: row[key] for key in row.keys() if key != "evidence_json"},
            "evidence": json.loads(row["evidence_json"]),
        }
        for row in edge_rows
    )
    conflicts = tuple(
        warning.to_dict()
        for warning in resolver_conflict_warnings(
            node_rows=tuple(node_rows),
            edges=edges,
        )
    )
    return (*conflicts, *rejected)


def _pack_evidence(
    matches: tuple[SemanticEvidenceMatch, ...],
    raw_by_id: dict[str, dict[str, Any]],
    limits: EvidencePackLimits,
) -> tuple[tuple[PackedEvidence, ...], dict[str, int], bool]:
    packed: list[PackedEvidence] = []
    per_file: dict[str, int] = {}
    truncated = False
    for rank, match in enumerate(matches, start=1):
        used = per_file.get(match.source_file, 0)
        remaining = limits.max_characters_per_file - used
        if remaining <= 0:
            truncated = True
            continue
        text = match.text
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        per_file[match.source_file] = used + len(text)
        raw = raw_by_id.get(match.evidence_unit_id)
        packed.append(
            PackedEvidence(
                id=match.evidence_unit_id,
                rank=rank,
                score=match.score,
                owner_type=match.owner_entity_type,
                owner_id=match.owner_entity_id,
                source_file=match.source_file,
                start_line=match.start_line,
                end_line=match.end_line,
                text=text,
                confidence=float(raw["confidence"]) if raw is not None else 0.5,
                provenance=(
                    str(raw["provenance"]) if raw is not None else "graph-metadata"
                ),
                scoring_reasons=match.scoring_reasons,
                related_symbols=match.related_symbols,
                related_files=match.related_files,
                relationship_ids=match.relationships,
                assessment=_raw_assessment(raw) if raw is not None else {},
                **_revision_fields(raw),
            )
        )
    return tuple(packed), per_file, truncated


def _revision_fields(raw: dict[str, Any] | None) -> dict[str, str | None]:
    if raw is None:
        return {}
    try:
        metadata = json.loads(str(raw.get("metadata_json") or "{}"))
    except (TypeError, ValueError):
        return {}
    values = {
        key: metadata.get(key)
        for key in ("repository_id", "source_revision_id", "content_sha256")
    }
    return (
        values
        if all(isinstance(value, str) and value for value in values.values())
        else {}
    )
