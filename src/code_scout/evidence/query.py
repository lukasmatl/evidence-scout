from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any

from code_scout.evidence.assessment import (
    EvidenceAssessment,
    assess_graph_evidence,
    assess_legacy_evidence,
    assess_raw_evidence,
    combine_assessments,
)
from code_scout.evidence.embeddings import (
    EmbeddingProvider,
    LocalEmbeddingProvider,
    cosine_similarity,
)
from code_scout.storage import open_storage

DEFAULT_SEMANTIC_QUERY_LIMIT = 10
_DIRECT_EDGE_OWNER_BATCH_SIZE = 200
SEMANTIC_MATCH_REASON = "semantic match"
LEXICAL_SCORE_UNIT = 1.0
_TEST_SOURCE_SCORE_MULTIPLIER = 0.5
_QUERY_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "implementation",
        "locate",
        "of",
        "that",
        "the",
        "to",
        "where",
        "which",
    }
)


@dataclass(frozen=True)
class SemanticEvidenceMatch:
    evidence_unit_id: str
    score: float
    owner_entity_type: str
    owner_entity_id: str
    source_file: str
    start_line: int | None
    end_line: int | None
    text: str
    reason: str = SEMANTIC_MATCH_REASON
    scoring_reasons: tuple[str, ...] = (SEMANTIC_MATCH_REASON,)
    related_entity_ids: tuple[str, ...] = ()
    related_services: tuple[str, ...] = ()
    related_contracts: tuple[str, ...] = ()
    related_concepts: tuple[str, ...] = ()
    related_decisions: tuple[str, ...] = ()
    related_symbols: tuple[str, ...] = ()
    related_files: tuple[str, ...] = ()
    relationships: tuple[str, ...] = ()
    assessment: EvidenceAssessment = field(
        default_factory=lambda: assess_legacy_evidence({})
    )
    repository_id: str | None = None
    source_revision_id: str | None = None
    content_sha256: str | None = None


def semantic_evidence_search(
    workspace_path: str = ".",
    *,
    question: str,
    limit: int = DEFAULT_SEMANTIC_QUERY_LIMIT,
    provider: EmbeddingProvider | None = None,
) -> tuple[SemanticEvidenceMatch, ...]:
    with open_storage(workspace_path) as connection:
        return search_semantic_evidence(
            connection,
            question=question,
            limit=limit,
            provider=provider,
        )


def search_semantic_evidence(
    connection: sqlite3.Connection,
    *,
    question: str,
    limit: int = DEFAULT_SEMANTIC_QUERY_LIMIT,
    provider: EmbeddingProvider | None = None,
) -> tuple[SemanticEvidenceMatch, ...]:
    if limit <= 0:
        msg = "Semantic query limit must be greater than zero"
        raise ValueError(msg)

    embedding_provider = provider or LocalEmbeddingProvider()
    query_vector = embedding_provider.embed_text(question)
    if len(query_vector) != embedding_provider.dimensions:
        msg = (
            "Embedding provider returned a vector whose length does not "
            "match its dimensions"
        )
        raise ValueError(msg)

    rows = connection.execute(
        """
        SELECT
            evidence_embeddings.vector_json,
            raw_evidence_units.id,
            raw_evidence_units.owner_entity_type,
            raw_evidence_units.owner_entity_id,
            raw_evidence_units.source_file,
            raw_evidence_units.start_line,
            raw_evidence_units.end_line,
            raw_evidence_units.text,
            raw_evidence_units.metadata_json,
            raw_evidence_units.provenance
        FROM evidence_embeddings
        INNER JOIN raw_evidence_units
            ON raw_evidence_units.id = evidence_embeddings.evidence_unit_id
        WHERE evidence_embeddings.model_id = ?
          AND evidence_embeddings.dimensions = ?
        ORDER BY raw_evidence_units.id
        """,
        (embedding_provider.model_id, embedding_provider.dimensions),
    ).fetchall()

    semantic_matches = [
        SemanticEvidenceMatch(
            evidence_unit_id=row["id"],
            score=cosine_similarity(
                query_vector, _vector_from_json(row["vector_json"])
            ),
            owner_entity_type=row["owner_entity_type"],
            owner_entity_id=row["owner_entity_id"],
            source_file=row["source_file"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            text=row["text"],
            assessment=assess_raw_evidence(
                json.loads(row["metadata_json"]), row["provenance"]
            ),
            **_revision_fields(json.loads(row["metadata_json"])),
        )
        for row in rows
    ]
    matches = _merge_hybrid_matches(
        [*semantic_matches, *_lexical_graph_matches(connection, question)]
    )
    matches = _expand_graph_context(connection, matches)
    ranked = tuple(
        sorted(matches, key=lambda match: (-match.score, match.evidence_unit_id))[
            :limit
        ]
    )
    return _resolved_assessments(connection, ranked, {row["id"]: row for row in rows})


def _resolved_assessments(
    connection: sqlite3.Connection,
    matches: tuple[SemanticEvidenceMatch, ...],
    raw_rows: dict[str, sqlite3.Row],
) -> tuple[SemanticEvidenceMatch, ...]:
    """Retain resolver uncertainty for retrieved raw observations, without reranking."""
    tables = {"service": "services", "contract": "contracts", "decision": "decisions"}
    owners: dict[tuple[str, str], list[dict[str, Any]]] = {}
    assessed = []
    for match in matches:
        row = raw_rows.get(match.evidence_unit_id)
        table = tables.get(match.owner_entity_type)
        if row is None or table is None:
            assessed.append(match)
            continue
        key = (table, match.owner_entity_id)
        if key not in owners:
            owner = connection.execute(
                f"SELECT evidence_json FROM {table} WHERE id = ?", (key[1],)
            ).fetchone()
            owners[key] = json.loads(owner["evidence_json"]) if owner else []
        supporting = [
            item
            for item in owners[key]
            if item.get("extractor_name") == row["provenance"]
            and item.get("file_path") == match.source_file
        ]
        if supporting:
            match = replace(
                match,
                assessment=combine_assessments(
                    (
                        match.assessment,
                        assess_graph_evidence(supporting),
                    )
                ),
            )
        assessed.append(match)
    return tuple(assessed)


def _revision_fields(metadata: object) -> dict[str, str | None]:
    """Return only complete retained-source bindings from indexed evidence."""
    if not isinstance(metadata, dict):
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


def _vector_from_json(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in json.loads(value))


def _lexical_graph_matches(
    connection: sqlite3.Connection,
    question: str,
) -> tuple[SemanticEvidenceMatch, ...]:
    terms = _query_terms(question)
    if not terms:
        return ()

    matches: list[SemanticEvidenceMatch] = []
    matches.extend(
        _lexical_matches_for_rows(
            _rows(
                connection.execute(
                    """
                    SELECT id, name, description, aliases_json, evidence_json
                    FROM services
                    ORDER BY id
                    """,
                ).fetchall()
            ),
            owner_entity_type="service",
            field_specs=(
                ("name", 9, "matched service name"),
                ("aliases", 8, "matched alias"),
                ("description", 3, "matched service description"),
                ("evidence", 2, "matched service evidence"),
            ),
            terms=terms,
        )
    )
    matches.extend(
        _lexical_matches_for_rows(
            _rows(
                connection.execute(
                    """
                    SELECT id, name, kind, path, producer_hint, operation_id,
                           evidence_json
                    FROM contracts
                    ORDER BY id
                    """,
                ).fetchall()
            ),
            owner_entity_type="contract",
            field_specs=(
                ("name", 6, "matched contract name"),
                ("path", 8, "matched contract path"),
                ("operation_id", 8, "matched operation id"),
                ("producer_hint", 4, "matched contract producer"),
                ("evidence", 2, "matched contract evidence"),
            ),
            terms=terms,
        )
    )
    matches.extend(
        _lexical_matches_for_rows(
            _rows(
                connection.execute(
                    """
                    SELECT id, name, description
                    FROM concepts
                    ORDER BY id
                    """,
                ).fetchall()
            ),
            owner_entity_type="concept",
            field_specs=(
                ("name", 6, "matched concept name"),
                ("description", 3, "matched concept description"),
            ),
            terms=terms,
        )
    )
    matches.extend(
        _lexical_matches_for_rows(
            _rows(
                connection.execute(
                    """
                    SELECT id, title, path, context, decision, consequences,
                           evidence_json
                    FROM decisions
                    ORDER BY id
                    """,
                ).fetchall()
            ),
            owner_entity_type="decision",
            field_specs=(
                ("title", 8, "matched decision title"),
                ("path", 6, "matched decision path"),
                ("context", 3, "matched decision context"),
                ("decision", 3, "matched decision text"),
                ("consequences", 2, "matched decision consequences"),
                ("evidence", 2, "matched decision evidence"),
            ),
            terms=terms,
        )
    )
    matches.extend(
        _lexical_matches_for_rows(
            _rows(
                connection.execute(
                    """
                    SELECT id, repository_id, path, kind, language
                    FROM files
                    ORDER BY id
                    """,
                ).fetchall()
            ),
            owner_entity_type="file",
            field_specs=(
                ("path", 24, "matched file path"),
                ("language", 2, "matched file language"),
                ("kind", 1, "matched file kind"),
            ),
            terms=terms,
        )
    )
    matches.extend(
        _lexical_matches_for_rows(
            _rows(
                connection.execute(
                    """
                    SELECT symbols.id, symbols.file_id, symbols.name, symbols.kind,
                           symbols.qualified_name, symbols.start_line,
                           symbols.end_line, files.path AS source_file
                    FROM symbols
                    LEFT JOIN files ON files.id = symbols.file_id
                    ORDER BY symbols.id
                    """,
                ).fetchall()
            ),
            owner_entity_type="symbol",
            field_specs=(
                ("name", 8, "matched symbol name"),
                ("qualified_name", 6, "matched symbol qualified name"),
            ),
            terms=terms,
        )
    )
    return tuple(matches)


def _lexical_matches_for_rows(
    rows: tuple[dict[str, Any], ...],
    *,
    owner_entity_type: str,
    field_specs: tuple[tuple[str, int, str], ...],
    terms: tuple[str, ...],
) -> tuple[SemanticEvidenceMatch, ...]:
    matches: list[SemanticEvidenceMatch] = []
    for row in rows:
        score, reason, matched_value = _score_metadata_row(
            row,
            field_specs=field_specs,
            terms=terms,
        )
        if score <= 0:
            continue

        entity_id = str(row["id"])
        source_file, start_line, end_line = _source_location(row)
        if _is_test_source_file(source_file):
            score *= _TEST_SOURCE_SCORE_MULTIPLIER
        matches.append(
            SemanticEvidenceMatch(
                evidence_unit_id=f"lexical:{owner_entity_type}:{entity_id}",
                score=score,
                owner_entity_type=owner_entity_type,
                owner_entity_id=entity_id,
                source_file=source_file,
                start_line=start_line,
                end_line=end_line,
                text=matched_value or _entity_text(row),
                reason=reason,
                scoring_reasons=(reason,),
                assessment=assess_graph_evidence(row.get("evidence", ())),
            )
        )
    return tuple(matches)


def _merge_hybrid_matches(
    matches: list[SemanticEvidenceMatch],
) -> tuple[SemanticEvidenceMatch, ...]:
    lexical_by_owner: dict[tuple[str, str], SemanticEvidenceMatch] = {}
    semantic_matches: list[SemanticEvidenceMatch] = []
    for match in sorted(matches, key=lambda item: item.evidence_unit_id):
        if not match.evidence_unit_id.startswith("lexical:"):
            semantic_matches.append(match)
            continue

        key = (match.owner_entity_type, match.owner_entity_id)
        existing = lexical_by_owner.get(key)
        if existing is None:
            lexical_by_owner[key] = match
            continue
        lexical_by_owner[key] = _combine_matches(existing, match)

    merged_matches: list[SemanticEvidenceMatch] = []
    owners_with_semantic_matches: set[tuple[str, str]] = set()
    for match in semantic_matches:
        key = (match.owner_entity_type, match.owner_entity_id)
        owners_with_semantic_matches.add(key)
        lexical_match = lexical_by_owner.get(key)
        if lexical_match is None:
            merged_matches.append(match)
        else:
            merged_matches.append(_combine_matches(match, lexical_match))

    merged_matches.extend(
        lexical_match
        for key, lexical_match in sorted(lexical_by_owner.items())
        if key not in owners_with_semantic_matches
    )
    return tuple(merged_matches)


def _combine_matches(
    existing: SemanticEvidenceMatch,
    match: SemanticEvidenceMatch,
) -> SemanticEvidenceMatch:
    reasons = _stable_unique((*existing.scoring_reasons, *match.scoring_reasons))
    display_match = _display_match(existing, match)
    return replace(
        display_match,
        score=existing.score + match.score,
        reason="; ".join(reasons),
        scoring_reasons=reasons,
    )


def _display_match(
    first: SemanticEvidenceMatch,
    second: SemanticEvidenceMatch,
) -> SemanticEvidenceMatch:
    first_is_lexical = first.evidence_unit_id.startswith("lexical:")
    second_is_lexical = second.evidence_unit_id.startswith("lexical:")
    if first_is_lexical and not second_is_lexical:
        return second
    if second_is_lexical and not first_is_lexical:
        return first
    if second.score > first.score:
        return second
    if first.score > second.score:
        return first
    return min((first, second), key=lambda match: match.evidence_unit_id)


def _expand_graph_context(
    connection: sqlite3.Connection,
    matches: tuple[SemanticEvidenceMatch, ...],
) -> tuple[SemanticEvidenceMatch, ...]:
    owner_keys = tuple(
        sorted({(match.owner_entity_type, match.owner_entity_id) for match in matches})
    )
    if not owner_keys:
        return matches

    contexts = {key: _GraphContext.with_entity(*key) for key in owner_keys}
    edge_rows = _direct_edge_rows(connection, owner_keys)
    for row in edge_rows:
        edge_id = f"relationship:{row['id']}"
        source = (str(row["source_type"]), str(row["source_id"]))
        target = (str(row["target_type"]), str(row["target_id"]))
        for owner, related in ((source, target), (target, source)):
            context = contexts.get(owner)
            if context is None:
                continue
            context.add_relationship(edge_id)
            context.add_entity(*related)

    return tuple(
        replace(
            match, **contexts[(match.owner_entity_type, match.owner_entity_id)].fields()
        )
        for match in matches
    )


@dataclass
class _GraphContext:
    services: list[str]
    contracts: list[str]
    concepts: list[str]
    decisions: list[str]
    symbols: list[str]
    files: list[str]
    relationships: list[str]

    @classmethod
    def with_entity(cls, entity_type: str, entity_id: str) -> _GraphContext:
        context = cls([], [], [], [], [], [], [])
        context.add_entity(entity_type, entity_id)
        return context

    def add_entity(self, entity_type: str, entity_id: str) -> None:
        group = {
            "service": self.services,
            "contract": self.contracts,
            "concept": self.concepts,
            "decision": self.decisions,
            "symbol": self.symbols,
            "file": self.files,
        }.get(entity_type)
        if group is not None:
            group.append(entity_id)

    def add_relationship(self, relationship_id: str) -> None:
        self.relationships.append(relationship_id)

    def fields(self) -> dict[str, tuple[str, ...]]:
        related_services = _stable_unique(self.services)
        related_contracts = _stable_unique(self.contracts)
        related_concepts = _stable_unique(self.concepts)
        related_decisions = _stable_unique(self.decisions)
        related_symbols = _stable_unique(self.symbols)
        related_files = _stable_unique(self.files)
        relationships = _stable_unique(self.relationships)
        related_entity_ids = _stable_unique(
            (
                *related_services,
                *related_contracts,
                *related_concepts,
                *related_decisions,
                *related_symbols,
                *related_files,
            )
        )
        return {
            "related_entity_ids": related_entity_ids,
            "related_services": related_services,
            "related_contracts": related_contracts,
            "related_concepts": related_concepts,
            "related_decisions": related_decisions,
            "related_symbols": related_symbols,
            "related_files": related_files,
            "relationships": relationships,
        }


def _direct_edge_rows(
    connection: sqlite3.Connection,
    owner_keys: tuple[tuple[str, str], ...],
) -> tuple[sqlite3.Row, ...]:
    rows_by_id: dict[int, sqlite3.Row] = {}
    for batch in _chunks(owner_keys, _DIRECT_EDGE_OWNER_BATCH_SIZE):
        for row in _direct_edge_rows_batch(connection, batch):
            rows_by_id[int(row["id"])] = row
    return tuple(row for _id, row in sorted(rows_by_id.items()))


def _direct_edge_rows_batch(
    connection: sqlite3.Connection,
    owner_keys: tuple[tuple[str, str], ...],
) -> tuple[sqlite3.Row, ...]:
    clauses = []
    params: list[str] = []
    for entity_type, entity_id in owner_keys:
        clauses.append(
            "((source_type = ? AND source_id = ?) "
            "OR (target_type = ? AND target_id = ?))"
        )
        params.extend((entity_type, entity_id, entity_type, entity_id))
    return tuple(
        connection.execute(
            f"""
            SELECT id, source_type, source_id, target_type, target_id
            FROM edges
            WHERE {" OR ".join(clauses)}
            ORDER BY id
            """,
            params,
        ).fetchall()
    )


def _chunks(
    values: tuple[tuple[str, str], ...],
    size: int,
) -> tuple[tuple[tuple[str, str], ...], ...]:
    return tuple(values[index : index + size] for index in range(0, len(values), size))


def _stable_unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _score_metadata_row(
    row: dict[str, Any],
    *,
    field_specs: tuple[tuple[str, int, str], ...],
    terms: tuple[str, ...],
) -> tuple[float, str, str]:
    score = 0.0
    reason = ""
    matched_value = ""
    reason_score = 0.0
    query_text = " ".join(terms)

    for field_name, weight, field_reason in field_specs:
        values = _metadata_values(row.get(field_name))
        field_score = 0.0
        field_match = ""
        for value in values:
            normalized = value.lower()
            term_matches = sum(1 for term in terms if term in normalized)
            if not term_matches:
                continue
            multiplier = len(terms) + 1 if normalized == query_text else term_matches
            candidate_score = LEXICAL_SCORE_UNIT * weight * multiplier
            if candidate_score > field_score:
                field_score = candidate_score
                field_match = value

        if field_score:
            score += field_score
            if field_score > reason_score:
                reason_score = field_score
                reason = field_reason
                matched_value = field_match

    return score, reason, matched_value


def _metadata_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(
            item for nested in value.values() for item in _metadata_values(nested)
        )
    if isinstance(value, list | tuple):
        return tuple(item for nested in value for item in _metadata_values(nested))
    return (str(value),)


def _source_location(row: dict[str, Any]) -> tuple[str, int | None, int | None]:
    evidence = row.get("evidence")
    if isinstance(evidence, list) and evidence:
        first = evidence[0]
        if isinstance(first, dict):
            file_path = first.get("file_path")
            if isinstance(file_path, str) and file_path:
                return (
                    file_path,
                    _optional_int(first.get("start_line")),
                    _optional_int(first.get("end_line")),
                )

    source_file = row.get("source_file") or row.get("path") or ""
    return (
        str(source_file),
        _optional_int(row.get("start_line")),
        _optional_int(row.get("end_line")),
    )


def _is_test_source_file(source_file: str) -> bool:
    normalized = "/" + source_file.lower().replace("\\", "/")
    filename = normalized.rsplit("/", maxsplit=1)[-1]
    return (
        "/test/" in normalized
        or "/tests/" in normalized
        or filename.startswith("test_")
        or ".test." in filename
        or ".spec." in filename
    )


def _entity_text(row: dict[str, Any]) -> str:
    for key in ("name", "title", "path", "qualified_name", "id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return str(row["id"])


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _query_terms(question: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", question.lower().replace("_", " "))
    return tuple(
        _singular_query_term(word) for word in words if word not in _QUERY_STOP_WORDS
    )


def _singular_query_term(term: str) -> str:
    if len(term) <= 3:
        return term
    if term.endswith("ies"):
        return f"{term[:-3]}y"
    if term.endswith(("ches", "shes", "sses", "xes", "zes", "oes")):
        return term[:-2]
    if term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def _rows(rows: list[sqlite3.Row]) -> tuple[dict[str, Any], ...]:
    return tuple(_decode_json_columns(dict(row)) for row in rows)


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key, value in row.items():
        if key.endswith("_json"):
            decoded[key.removesuffix("_json")] = json.loads(value)
            del decoded[key]
    return decoded


__all__ = [
    "DEFAULT_SEMANTIC_QUERY_LIMIT",
    "LEXICAL_SCORE_UNIT",
    "SEMANTIC_MATCH_REASON",
    "SemanticEvidenceMatch",
    "search_semantic_evidence",
    "semantic_evidence_search",
]
