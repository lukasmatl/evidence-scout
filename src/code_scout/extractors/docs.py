from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from code_scout.discovery import DiscoveredFile
from code_scout.models.facts import (
    ActiveFact,
    EntityFact,
    ExtractorMetadata,
    FactEvidence,
    FactReference,
    RelationFact,
    entity_fact_to_active_fact,
    relation_fact_to_active_fact,
)
from code_scout.utils.active_facts import merge_active_facts_by_id
from code_scout.utils.identifiers import slugify_identifier

EXTRACTOR_NAME = "docs"
INFERRED_CONCEPT_CONFIDENCE = 0.55
EXPLICIT_LINK_CONFIDENCE = 0.85

GENERIC_HEADINGS = frozenset(
    {
        "contents",
        "introduction",
        "overview",
        "summary",
        "table of contents",
    },
)


@dataclass(frozen=True)
class MarkdownConcept:
    id: str
    name: str
    description: str | None
    start_line: int
    end_line: int
    confidence: float
    explicit_links: tuple[FactReference, ...] = ()


@dataclass(frozen=True)
class MarkdownDocument:
    file: DiscoveredFile
    concepts: tuple[MarkdownConcept, ...]


def docs_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[ActiveFact, ...]:
    documents = docs_markdown_documents(discovered_files)
    active_facts: list[ActiveFact] = []

    for document in documents:
        file_reference = FactReference(
            type="file",
            id=f"file:{document.file.repository_id}:{document.file.path}",
        )
        for concept in document.concepts:
            active_facts.append(_concept_active_fact(document.file, concept))
            concept_reference = FactReference(type="concept", id=concept.id)
            active_facts.append(
                _relation_active_fact(
                    document.file,
                    subject=file_reference,
                    relation="documents_concept",
                    object=concept_reference,
                    confidence=concept.confidence,
                    start_line=concept.start_line,
                    end_line=concept.end_line,
                    metadata={
                        "concept_name": concept.name,
                        "repository_id": document.file.repository_id,
                    },
                    properties={"concept_name": concept.name},
                ),
            )
            for explicit_link in concept.explicit_links:
                active_facts.append(
                    _relation_active_fact(
                        document.file,
                        subject=concept_reference,
                        relation="documents",
                        object=explicit_link,
                        confidence=EXPLICIT_LINK_CONFIDENCE,
                        start_line=concept.start_line,
                        end_line=concept.end_line,
                        metadata={
                            "concept_name": concept.name,
                            "repository_id": document.file.repository_id,
                            "explicit_link": True,
                        },
                        properties={"concept_name": concept.name},
                    ),
                )

    return merge_active_facts_by_id(*active_facts)


def docs_markdown_documents(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[MarkdownDocument, ...]:
    documentation_files = tuple(
        file
        for file in discovered_files
        if file.kind == "documentation" and file.extension.lower() in {".md", ".mdx"}
    )
    known_files = {
        (file.repository_id, file.path): FactReference(
            type="file",
            id=f"file:{file.repository_id}:{file.path}",
        )
        for file in discovered_files
    }

    documents: list[MarkdownDocument] = []
    for discovered_file in documentation_files:
        text = Path(discovered_file.absolute_path).read_text(encoding="utf-8")
        documents.append(
            MarkdownDocument(
                file=discovered_file,
                concepts=_concepts_from_markdown(
                    text,
                    discovered_file=discovered_file,
                    known_files=known_files,
                ),
            ),
        )

    return tuple(documents)


def _concepts_from_markdown(
    text: str,
    *,
    discovered_file: DiscoveredFile,
    known_files: dict[tuple[str, str], FactReference],
) -> tuple[MarkdownConcept, ...]:
    lines = text.splitlines()
    sections = _heading_sections(lines)
    concepts: list[MarkdownConcept] = []
    seen_slugs: set[str] = set()

    for section in sections:
        level, title, start_line, end_line = section
        if level > 3:
            continue
        concept_name = _concept_name(title)
        if not concept_name:
            continue
        concept_slug = slugify_identifier(concept_name, fallback="concept")
        if concept_slug in seen_slugs:
            continue
        seen_slugs.add(concept_slug)
        body_lines = lines[start_line:end_line]
        explicit_links = _explicit_file_links(
            title,
            *body_lines,
            discovered_file=discovered_file,
            known_files=known_files,
        )
        concepts.append(
            MarkdownConcept(
                id=f"concept:{discovered_file.repository_id}:{concept_slug}",
                name=concept_name,
                description=_description_from_section(body_lines),
                start_line=start_line,
                end_line=start_line,
                confidence=(
                    EXPLICIT_LINK_CONFIDENCE
                    if explicit_links
                    else INFERRED_CONCEPT_CONFIDENCE
                ),
                explicit_links=explicit_links,
            ),
        )

    return tuple(concepts)


def _heading_sections(lines: list[str]) -> tuple[tuple[int, str, int, int], ...]:
    headings: list[tuple[int, str, int]] = []
    for line_number, line in enumerate(lines, start=1):
        heading = _heading(line)
        if heading is not None:
            headings.append((*heading, line_number))

    sections: list[tuple[int, str, int, int]] = []
    for index, (level, title, line_number) in enumerate(headings):
        next_line = len(lines) + 1
        for next_level, _next_title, candidate_line in headings[index + 1 :]:
            if next_level <= level:
                next_line = candidate_line
                break
        sections.append((level, title, line_number, next_line - 1))

    return tuple(sections)


def _heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _concept_name(value: str) -> str:
    without_links = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    name = re.sub(r"[*_`]", "", without_links).strip()
    name = re.sub(r"\s+", " ", name)
    if name.lower() in GENERIC_HEADINGS:
        return ""
    return name


def _description_from_section(lines: list[str]) -> str | None:
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if stripped.startswith(("#", "-", "*", ">", "```")):
            if paragraph:
                break
            continue
        paragraph.append(stripped)

    if not paragraph:
        return None
    return re.sub(r"\s+", " ", " ".join(paragraph))


def _explicit_file_links(
    *values: str,
    discovered_file: DiscoveredFile,
    known_files: dict[tuple[str, str], FactReference],
) -> tuple[FactReference, ...]:
    links: list[FactReference] = []
    seen: set[tuple[str, str]] = set()

    for value in values:
        for raw_target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", value):
            target_path = _resolved_link_target(discovered_file.path, raw_target)
            if target_path is None:
                continue
            key = (discovered_file.repository_id, target_path)
            reference = known_files.get(key)
            if reference is None or (reference.type, reference.id) in seen:
                continue
            seen.add((reference.type, reference.id))
            links.append(reference)

    return tuple(links)


def _resolved_link_target(source_path: str, raw_target: str) -> str | None:
    target = raw_target.strip().split(maxsplit=1)[0]
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not parsed.path:
        return None
    path = unquote(parsed.path)
    if path.startswith("/"):
        return path.removeprefix("/")
    base_dir = posixpath.dirname(source_path)
    normalized = posixpath.normpath(posixpath.join(base_dir, path))
    return normalized.removeprefix("./")


def _concept_active_fact(
    discovered_file: DiscoveredFile,
    concept: MarkdownConcept,
) -> ActiveFact:
    return entity_fact_to_active_fact(
        EntityFact(
            entity_type="concept",
            entity_id=concept.id,
            properties={
                "name": concept.name,
                "description": concept.description,
            },
            evidence=(
                FactEvidence(
                    file_path=discovered_file.path,
                    extractor_name=EXTRACTOR_NAME,
                    confidence=concept.confidence,
                    start_line=concept.start_line,
                    end_line=concept.end_line,
                    metadata={
                        "source": "heading",
                        "repository_id": discovered_file.repository_id,
                        "explicit_link_count": len(concept.explicit_links),
                    },
                ),
            ),
            confidence=concept.confidence,
            extractor_metadata=ExtractorMetadata(name=EXTRACTOR_NAME),
        ),
    )


def _relation_active_fact(
    discovered_file: DiscoveredFile,
    *,
    subject: FactReference,
    relation: str,
    object: FactReference,
    confidence: float,
    start_line: int,
    end_line: int,
    metadata: dict[str, object],
    properties: dict[str, object],
) -> ActiveFact:
    return relation_fact_to_active_fact(
        RelationFact(
            subject=subject,
            relation=relation,
            object=object,
            properties=properties,
            evidence=(
                FactEvidence(
                    file_path=discovered_file.path,
                    extractor_name=EXTRACTOR_NAME,
                    confidence=confidence,
                    start_line=start_line,
                    end_line=end_line,
                    metadata=metadata,
                ),
            ),
            confidence=confidence,
            extractor_metadata=ExtractorMetadata(name=EXTRACTOR_NAME),
        ),
    )
