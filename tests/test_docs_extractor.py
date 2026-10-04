from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.docs import docs_active_facts


def test_docs_active_facts_emit_low_confidence_concepts(tmp_path) -> None:
    docs_file = tmp_path / "docs" / "architecture.md"
    docs_file.parent.mkdir()
    docs_file.write_text(
        """# Architecture Memory

Architecture memory captures durable engineering context.

## Export Pipeline

Exports compile the memory graph for downstream tools.
""",
        encoding="utf-8",
    )

    facts = docs_active_facts(
        (_discovered_file(docs_file, relative_path="docs/architecture.md"),),
    )
    concept_facts = [fact for fact in facts if fact.kind == "concept"]
    documentation_links = [fact for fact in facts if fact.kind == "documents_concept"]

    assert [fact.identity["entity_id"] for fact in concept_facts] == [
        "concept:workspace:architecture-memory",
        "concept:workspace:export-pipeline",
    ]
    assert [fact.properties["description"] for fact in concept_facts] == [
        "Architecture memory captures durable engineering context.",
        "Exports compile the memory graph for downstream tools.",
    ]
    assert {fact.confidence for fact in concept_facts} == {0.55}
    assert [fact.identity["relation"] for fact in documentation_links] == [
        "documents_concept",
        "documents_concept",
    ]
    assert {fact.confidence for fact in documentation_links} == {0.55}


def test_docs_active_facts_raise_confidence_for_explicit_known_file_links(
    tmp_path,
) -> None:
    source_file = tmp_path / "src" / "payments.ts"
    source_file.parent.mkdir()
    source_file.write_text("export function processPayments() {}\n", encoding="utf-8")
    docs_file = tmp_path / "docs" / "payments.md"
    docs_file.parent.mkdir()
    docs_file.write_text(
        """# Payments

Payments are implemented in [payments.ts](../src/payments.ts).
""",
        encoding="utf-8",
    )

    facts = docs_active_facts(
        (
            _discovered_file(docs_file, relative_path="docs/payments.md"),
            _discovered_file(
                source_file,
                relative_path="src/payments.ts",
                kind="source",
                extension=".ts",
                language="typescript",
            ),
        ),
    )
    concept_fact = next(fact for fact in facts if fact.kind == "concept")
    explicit_link_fact = next(fact for fact in facts if fact.kind == "documents")

    assert concept_fact.confidence == 0.85
    assert concept_fact.evidence[0].metadata["explicit_link_count"] == 1
    assert explicit_link_fact.confidence == 0.85
    assert explicit_link_fact.identity == {
        "subject": {
            "type": "concept",
            "id": "concept:workspace:payments",
        },
        "relation": "documents",
        "object": {
            "type": "file",
            "id": "file:workspace:src/payments.ts",
        },
    }
    assert explicit_link_fact.evidence[0].metadata["explicit_link"] is True


def _discovered_file(
    file_path: Path,
    *,
    relative_path: str,
    kind: str = "documentation",
    extension: str = ".md",
    language: str | None = "markdown",
    repository_id: str = "workspace",
) -> DiscoveredFile:
    content = file_path.read_bytes()
    return DiscoveredFile(
        repository_id=repository_id,
        path=relative_path,
        absolute_path=str(file_path),
        kind=kind,
        extension=extension,
        content_hash=sha256(content).hexdigest(),
        modified_at="2026-06-21T10:00:00Z",
        language=language,
    )
