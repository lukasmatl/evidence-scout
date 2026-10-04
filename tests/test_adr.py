from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.adr import (
    adr_active_facts,
    concept_links_from_adr_markdown,
    parse_adr_markdown,
)


def test_parse_madr_style_decision_fields() -> None:
    metadata = parse_adr_markdown(
        """# ADR 0003: Use SQLite for local graph storage

* Status: accepted
* Date: 2026-06-20

## Context and Problem Statement

code-scout needs a local durable graph store.

## Decision Outcome

Chosen option: SQLite, because it is available in the stdlib.

## Consequences

Scans can run offline.
""",
        path="docs/adr/0003-use-sqlite.md",
    )

    assert metadata.adr_id == "0003"
    assert metadata.title.value == "Use SQLite for local graph storage"
    assert metadata.status is not None
    assert metadata.status.value == "accepted"
    assert metadata.decided_at is not None
    assert metadata.decided_at.value == "2026-06-20"
    assert metadata.context is not None
    assert metadata.context.value == "code-scout needs a local durable graph store."
    assert metadata.context.start_line == 8
    assert metadata.context.end_line == 8
    assert metadata.decision is not None
    assert metadata.decision.value == ("SQLite, because it is available in the stdlib.")
    assert metadata.consequences is not None
    assert metadata.consequences.value == "Scans can run offline."


def test_parse_michael_nygard_style_decision_fields() -> None:
    metadata = parse_adr_markdown(
        """# 4. Preserve architectural decisions

Date: 2026-06-20

## Status

Accepted

## Context

Architectural rationale is otherwise lost in markdown prose.

## Decision

Parse ADR sections into graph decision nodes.

## Consequences

Exporters can include rationale with source evidence.
""",
        path="docs/adr/0004-preserve-decisions.md",
    )

    assert metadata.adr_id == "4"
    assert metadata.title.value == "Preserve architectural decisions"
    assert metadata.status is not None
    assert metadata.status.value == "Accepted"
    assert metadata.decided_at is not None
    assert metadata.decided_at.value == "2026-06-20"
    assert metadata.context is not None
    assert metadata.context.start_line == 11
    assert metadata.context.end_line == 11
    assert metadata.decision is not None
    assert metadata.decision.start_line == 15
    assert metadata.consequences is not None
    assert metadata.consequences.start_line == 19


def test_parse_simple_custom_decision_fields() -> None:
    metadata = parse_adr_markdown(
        """# Use evidence-backed ADR parsing

Status: Proposed
Date: 2026-06-20
Context: ADRs vary across repositories.
Decision: Support common markdown conventions without adding dependencies.
Consequences: Early scans preserve rationale before exporter work exists.
""",
        path="records/decisions/use-evidence-backed-adr-parsing.md",
    )

    assert metadata.adr_id is None
    assert metadata.title.value == "Use evidence-backed ADR parsing"
    assert metadata.status is not None
    assert metadata.status.value == "Proposed"
    assert metadata.decided_at is not None
    assert metadata.decided_at.value == "2026-06-20"
    assert metadata.context is not None
    assert metadata.context.value == "ADRs vary across repositories."
    assert metadata.context.start_line == 5
    assert metadata.decision is not None
    assert metadata.decision.value == (
        "Support common markdown conventions without adding dependencies."
    )
    assert metadata.consequences is not None
    assert metadata.consequences.value == (
        "Early scans preserve rationale before exporter work exists."
    )


def test_infer_concept_links_from_adr_title_and_headings() -> None:
    links = concept_links_from_adr_markdown(
        """# ADR 0005: Use architecture memory

## Status

Accepted

## Architecture memory

Generated memory should include decisions.

## Export pipeline

Decision records flow into memory exports.
""",
        path="docs/adr/0005-architecture-memory.md",
        repository_id="workspace",
    )

    assert [link.concept.id for link in links] == [
        "concept:workspace:use-architecture-memory",
        "concept:workspace:architecture-memory",
        "concept:workspace:export-pipeline",
    ]
    assert [link.concept.name for link in links] == [
        "Use architecture memory",
        "Architecture memory",
        "Export pipeline",
    ]
    assert [
        (
            link.edge.source_type,
            link.edge.source_id,
            link.edge.target_type,
            link.edge.target_id,
            link.edge.relation_type,
            link.edge.confidence,
            link.edge.evidence[0].confidence,
            link.edge.evidence[0].metadata["source"],
        )
        for link in links
    ] == [
        (
            "concept",
            "concept:workspace:use-architecture-memory",
            "decision",
            "decision:workspace:docs/adr/0005-architecture-memory.md",
            "decided_by",
            0.85,
            0.85,
            "title",
        ),
        (
            "concept",
            "concept:workspace:architecture-memory",
            "decision",
            "decision:workspace:docs/adr/0005-architecture-memory.md",
            "decided_by",
            0.85,
            0.85,
            "heading",
        ),
        (
            "concept",
            "concept:workspace:export-pipeline",
            "decision",
            "decision:workspace:docs/adr/0005-architecture-memory.md",
            "decided_by",
            0.85,
            0.85,
            "heading",
        ),
    ]


def test_infer_concept_links_deduplicates_repeated_adr_concepts() -> None:
    links = concept_links_from_adr_markdown(
        """# Use architecture memory

## Architecture memory

## Architecture memory
""",
        path="docs/adr/use-architecture-memory.md",
        repository_id="workspace",
    )

    assert [link.concept.id for link in links] == [
        "concept:workspace:use-architecture-memory",
        "concept:workspace:architecture-memory",
    ]


def test_adr_active_facts_emit_decision_entity_fact(tmp_path) -> None:
    adr_file = tmp_path / "0001-use-sqlite.md"
    adr_file.write_text(
        """# ADR 0001: Use SQLite

Date: 2026-06-20
Status: Accepted

## Context

code-scout needs portable local storage.

## Decision

Use SQLite through the stdlib sqlite3 module.

## Consequences

The database remains easy to inspect and migrate.
""",
        encoding="utf-8",
    )

    facts = adr_active_facts(
        (_discovered_adr_file(adr_file, relative_path="docs/adr/0001-use-sqlite.md"),)
    )
    decision_fact = next(fact for fact in facts if fact.kind == "decision")

    assert decision_fact.extractor == "adr_markdown"
    assert decision_fact.identity == {
        "entity_type": "decision",
        "entity_id": "decision:workspace:docs/adr/0001-use-sqlite.md",
    }
    assert decision_fact.properties == {
        "adr_id": "0001",
        "title": "Use SQLite",
        "status": "accepted",
        "path": "docs/adr/0001-use-sqlite.md",
        "decided_at": "2026-06-20",
        "context": "code-scout needs portable local storage.",
        "decision": "Use SQLite through the stdlib sqlite3 module.",
        "consequences": "The database remains easy to inspect and migrate.",
    }
    assert decision_fact.source_file == "docs/adr/0001-use-sqlite.md"
    assert decision_fact.confidence == 1.0
    assert [evidence.metadata["field"] for evidence in decision_fact.evidence] == [
        "title",
        "status",
        "decided_at",
        "context",
        "decision",
        "consequences",
    ]


def test_adr_active_facts_emit_decision_affects_concept_relation(tmp_path) -> None:
    adr_file = tmp_path / "0002-architecture-memory.md"
    adr_file.write_text(
        """# ADR 0002: Use architecture memory

## Architecture memory

Decision records flow into memory exports.
""",
        encoding="utf-8",
    )

    facts = adr_active_facts(
        (
            _discovered_adr_file(
                adr_file,
                relative_path="docs/adr/0002-architecture-memory.md",
            ),
        )
    )
    affects_facts = [fact for fact in facts if fact.kind == "affects"]

    assert [fact.identity for fact in affects_facts] == [
        {
            "subject": {
                "type": "decision",
                "id": "decision:workspace:docs/adr/0002-architecture-memory.md",
            },
            "relation": "affects",
            "object": {
                "type": "concept",
                "id": "concept:workspace:use-architecture-memory",
            },
        },
        {
            "subject": {
                "type": "decision",
                "id": "decision:workspace:docs/adr/0002-architecture-memory.md",
            },
            "relation": "affects",
            "object": {
                "type": "concept",
                "id": "concept:workspace:architecture-memory",
            },
        },
    ]
    assert affects_facts[0].properties == {
        "concept_name": "Use architecture memory",
        "inferred_from": "decided_by",
    }
    assert affects_facts[0].confidence == 0.85
    assert affects_facts[0].evidence[0].metadata == {
        "source": "title",
        "concept_slug": "use-architecture-memory",
        "repository_id": "workspace",
    }


def _discovered_adr_file(
    file_path: Path,
    *,
    relative_path: str,
    repository_id: str = "workspace",
) -> DiscoveredFile:
    content = file_path.read_bytes()
    return DiscoveredFile(
        repository_id=repository_id,
        path=relative_path,
        absolute_path=str(file_path),
        kind="decision",
        extension=file_path.suffix,
        content_hash=sha256(content).hexdigest(),
        modified_at="2026-06-20T10:00:00Z",
        language="markdown",
    )
