from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from code_scout.discovery import DiscoveredFile
from code_scout.models.facts import (
    ActiveFact,
    ExtractorMetadata,
    FactEvidence,
    FactReference,
    RelationFact,
    decision_to_entity_facts,
    entity_fact_to_active_fact,
    relation_fact_to_active_fact,
)
from code_scout.models.graph import Concept, Decision, Edge, Evidence
from code_scout.utils.active_facts import merge_active_facts_by_id
from code_scout.utils.identifiers import slugify_identifier

EXTRACTOR_NAME = "adr_markdown"
EXACT_PARSED_CONFIDENCE = 1.0
INFERRED_FACT_CONFIDENCE = 0.85
FIELD_HEADINGS = {
    "context": (
        "context",
        "context and problem statement",
        "problem statement",
    ),
    "decision": (
        "decision",
        "decision outcome",
        "outcome",
    ),
    "consequences": (
        "consequences",
        "positive consequences",
        "negative consequences",
    ),
    "status": ("status",),
}
INLINE_FIELDS = {
    "status": "status",
    "date": "date",
    "decided_at": "date",
    "context": "context",
    "decision": "decision",
    "consequences": "consequences",
}
GENERIC_CONCEPT_HEADINGS = frozenset(
    {normalized for headings in FIELD_HEADINGS.values() for normalized in headings}
    | {"date", "decided at"}
)


@dataclass(frozen=True)
class ADRField:
    value: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ADRMetadata:
    adr_id: str | None
    title: ADRField
    status: ADRField | None = None
    decided_at: ADRField | None = None
    context: ADRField | None = None
    decision: ADRField | None = None
    consequences: ADRField | None = None


@dataclass(frozen=True)
class ADRConceptLink:
    concept: Concept
    edge: Edge


def parse_adr_file(discovered_file: DiscoveredFile) -> ADRMetadata | None:
    if discovered_file.kind != "decision":
        return None

    text = Path(discovered_file.absolute_path).read_text(encoding="utf-8")
    return parse_adr_markdown(text, path=discovered_file.path)


def adr_decisions(discovered_files: tuple[DiscoveredFile, ...]) -> tuple[Decision, ...]:
    decisions: list[Decision] = []

    for discovered_file in discovered_files:
        metadata = parse_adr_file(discovered_file)
        if metadata is None:
            continue

        decisions.append(_decision_from_metadata(discovered_file, metadata))

    return tuple(decisions)


def adr_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[ActiveFact, ...]:
    active_facts: list[ActiveFact] = []

    for decision in adr_decisions(discovered_files):
        active_facts.extend(
            entity_fact_to_active_fact(fact)
            for fact in decision_to_entity_facts(decision)
        )

    for link in adr_concept_links(discovered_files):
        active_facts.extend(_active_facts_for_concept_link(link))

    return merge_active_facts_by_id(*active_facts)


def adr_concept_links(
    discovered_files: tuple[DiscoveredFile, ...],
) -> tuple[ADRConceptLink, ...]:
    links: list[ADRConceptLink] = []

    for discovered_file in discovered_files:
        if discovered_file.kind != "decision":
            continue
        text = Path(discovered_file.absolute_path).read_text(encoding="utf-8")
        links.extend(
            concept_links_from_adr_markdown(
                text,
                path=discovered_file.path,
                repository_id=discovered_file.repository_id,
            ),
        )

    return tuple(links)


def concept_links_from_adr_markdown(
    text: str,
    *,
    path: str,
    repository_id: str,
) -> tuple[ADRConceptLink, ...]:
    lines = text.splitlines()
    metadata = parse_adr_markdown(text, path=path)
    decision_id = _decision_id(repository_id, path)
    candidates = [
        (
            "title",
            metadata.title.value,
            metadata.title.start_line,
            metadata.title.end_line,
        )
    ]
    candidates.extend(_concept_heading_candidates(lines))

    links: list[ADRConceptLink] = []
    seen_slugs: set[str] = set()
    for source, name, start_line, end_line in candidates:
        concept_name = _concept_name(name)
        if not concept_name:
            continue
        concept_slug = slugify_identifier(concept_name, fallback="concept")
        if concept_slug in seen_slugs:
            continue
        seen_slugs.add(concept_slug)
        concept = Concept(
            id=f"concept:{repository_id}:{concept_slug}",
            name=concept_name,
        )
        links.append(
            ADRConceptLink(
                concept=concept,
                edge=Edge(
                    source_type="concept",
                    source_id=concept.id,
                    target_type="decision",
                    target_id=decision_id,
                    relation_type="decided_by",
                    confidence=INFERRED_FACT_CONFIDENCE,
                    evidence=(
                        Evidence(
                            file_path=path,
                            extractor_name=EXTRACTOR_NAME,
                            confidence=INFERRED_FACT_CONFIDENCE,
                            start_line=start_line,
                            end_line=end_line,
                            metadata={
                                "source": source,
                                "concept_slug": concept_slug,
                                "repository_id": repository_id,
                            },
                        ),
                    ),
                ),
            ),
        )

    return tuple(links)


def parse_adr_markdown(text: str, *, path: str) -> ADRMetadata:
    lines = text.splitlines()
    title = _title_field(lines, path)
    fields = _section_fields(lines) | _inline_fields(lines)

    return ADRMetadata(
        adr_id=_adr_id(lines, path),
        title=title,
        status=fields.get("status"),
        decided_at=fields.get("date"),
        context=fields.get("context"),
        decision=fields.get("decision"),
        consequences=fields.get("consequences"),
    )


def _title_field(lines: list[str], path: str) -> ADRField:
    for line_number, line in enumerate(lines, start=1):
        heading = _heading(line)
        if heading is not None and heading[0] == 1:
            return ADRField(
                value=_title_without_id(heading[1]),
                start_line=line_number,
                end_line=line_number,
            )

    return ADRField(value=_title_from_path(path), start_line=1, end_line=1)


def _section_fields(lines: list[str]) -> dict[str, ADRField]:
    headings: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(lines):
        heading = _heading(line)
        if heading is None:
            continue
        field = _field_for_heading(heading[1])
        if field is not None:
            headings.append((index, heading[0], field, heading[1]))

    fields: dict[str, ADRField] = {}
    for heading_position, (index, level, field, heading_text) in enumerate(headings):
        next_index = _next_section_index(lines, headings, heading_position, level)
        value, start_line, end_line = _section_value(lines, index + 1, next_index)
        if not value:
            continue
        if (
            field == "decision"
            and _normalized_heading(heading_text) == "decision outcome"
        ):
            value = _strip_madr_decision_marker(value)
        fields.setdefault(
            field, ADRField(value=value, start_line=start_line, end_line=end_line)
        )

    return fields


def _inline_fields(lines: list[str]) -> dict[str, ADRField]:
    fields: dict[str, ADRField] = {}

    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z _-]*):\s*(.+?)\s*$", line)
        if match is None:
            continue
        field = INLINE_FIELDS.get(match.group(1).strip().lower().replace(" ", "_"))
        if field is None:
            continue
        value = _strip_markdown_emphasis(match.group(2).strip())
        if value:
            fields[field] = ADRField(
                value=value, start_line=index + 1, end_line=index + 1
            )

    return fields


def _next_section_index(
    lines: list[str],
    headings: list[tuple[int, int, str, str]],
    heading_position: int,
    level: int,
) -> int:
    for index in range(heading_position + 1, len(headings)):
        next_line_index, next_level, _field, _heading_text = headings[index]
        if next_level <= level:
            return next_line_index

    for index in range(headings[heading_position][0] + 1, len(lines)):
        heading = _heading(lines[index])
        if heading is not None and heading[0] <= level:
            return index

    return len(lines)


def _section_value(
    lines: list[str],
    start_index: int,
    end_index: int,
) -> tuple[str, int, int]:
    while start_index < end_index and not lines[start_index].strip():
        start_index += 1
    while end_index > start_index and not lines[end_index - 1].strip():
        end_index -= 1
    if start_index >= end_index:
        return "", start_index + 1, start_index + 1
    return "\n".join(lines[start_index:end_index]).strip(), start_index + 1, end_index


def _field_evidence(
    discovered_file: DiscoveredFile,
    metadata: ADRMetadata,
) -> tuple[Evidence, ...]:
    evidence: list[Evidence] = []
    for field_name in (
        "title",
        "status",
        "decided_at",
        "context",
        "decision",
        "consequences",
    ):
        field = getattr(metadata, field_name)
        if field is None:
            continue
        evidence.append(
            Evidence(
                file_path=discovered_file.path,
                extractor_name=EXTRACTOR_NAME,
                confidence=EXACT_PARSED_CONFIDENCE,
                start_line=field.start_line,
                end_line=field.end_line,
                metadata={
                    "field": field_name,
                    "repository_id": discovered_file.repository_id,
                },
            ),
        )
    return tuple(evidence)


def _decision_from_metadata(
    discovered_file: DiscoveredFile,
    metadata: ADRMetadata,
) -> Decision:
    return Decision(
        id=_decision_id(discovered_file.repository_id, discovered_file.path),
        title=metadata.title.value,
        status=_field_value(metadata.status, default="candidate").lower(),
        adr_id=metadata.adr_id,
        path=discovered_file.path,
        decided_at=_field_value(metadata.decided_at),
        context=_field_value(metadata.context),
        decision=_field_value(metadata.decision),
        consequences=_field_value(metadata.consequences),
        evidence=_field_evidence(discovered_file, metadata),
    )


def _active_facts_for_concept_link(link: ADRConceptLink) -> tuple[ActiveFact, ...]:
    edge = link.edge
    return tuple(
        relation_fact_to_active_fact(
            RelationFact(
                subject=FactReference(type="decision", id=edge.target_id),
                relation="affects",
                object=FactReference(type="concept", id=edge.source_id),
                properties={
                    "concept_name": link.concept.name,
                    "inferred_from": edge.relation_type,
                },
                evidence=(
                    FactEvidence(
                        file_path=evidence.file_path,
                        extractor_name=evidence.extractor_name,
                        confidence=evidence.confidence or INFERRED_FACT_CONFIDENCE,
                        start_line=evidence.start_line,
                        end_line=evidence.end_line,
                        metadata=dict(evidence.metadata),
                    ),
                ),
                confidence=edge.confidence,
                extractor_metadata=ExtractorMetadata(name=EXTRACTOR_NAME),
            ),
        )
        for evidence in edge.evidence
    )


def _concept_heading_candidates(
    lines: list[str],
) -> tuple[tuple[str, str, int, int], ...]:
    candidates: list[tuple[str, str, int, int]] = []
    for line_number, line in enumerate(lines, start=1):
        heading = _heading(line)
        if heading is None:
            continue
        level, heading_text = heading
        if level == 1:
            continue
        if _normalized_heading(heading_text) in GENERIC_CONCEPT_HEADINGS:
            continue
        candidates.append(("heading", heading_text, line_number, line_number))
    return tuple(candidates)


def _concept_name(value: str) -> str:
    name = _strip_markdown_emphasis(_title_without_id(value)).strip()
    return re.sub(r"\s+", " ", name)


def _heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
    if match is None:
        return None
    return len(match.group(1)), match.group(2).strip()


def _field_for_heading(text: str) -> str | None:
    normalized = _normalized_heading(text)
    for field, headings in FIELD_HEADINGS.items():
        if normalized in headings:
            return field
    return None


def _normalized_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _title_without_id(title: str) -> str:
    original = title.strip()
    without_id = re.sub(
        r"^(?:adr[- ]*)?\d{1,5}(?:\s*[:.\)-]|\s+)\s*",
        "",
        original,
        flags=re.I,
    )
    return without_id.strip() or original


def _title_from_path(path: str) -> str:
    stem = path.rsplit("/", maxsplit=1)[-1].rsplit(".", maxsplit=1)[0]
    return _title_without_id(stem.replace("-", " ").replace("_", " ").strip()) or path


def _adr_id(lines: list[str], path: str) -> str | None:
    for line in lines:
        heading = _heading(line)
        if heading is None or heading[0] != 1:
            continue
        title_match = re.match(r"^(?:adr[- ]*)?(\d{1,5})\b", heading[1], flags=re.I)
        if title_match is not None:
            return title_match.group(1)

    filename = path.rsplit("/", maxsplit=1)[-1]
    path_match = re.match(r"^(?:adr[- ]*)?(\d{1,5})\b", filename, flags=re.I)
    if path_match is not None:
        return path_match.group(1)

    return None


def _strip_madr_decision_marker(value: str) -> str:
    return re.sub(r"^Chosen option:\s*", "", value.strip(), flags=re.I).strip()


def _strip_markdown_emphasis(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"*", "_", "`"}:
        return value[1:-1].strip()
    return value


def _field_value(field: ADRField | None, *, default: str | None = None) -> str | None:
    return default if field is None else field.value


def _decision_id(repository_id: str, path: str) -> str:
    return f"decision:{repository_id}:{path}"
