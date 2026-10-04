from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Protocol, TypeVar

from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    Evidence,
    File,
    Repository,
    Service,
    Symbol,
)
from code_scout.utils.identifiers import canonical_http_contract_id, slugify_identifier


class _HasId(Protocol):
    id: str


T = TypeVar("T", bound=_HasId)


@dataclass(frozen=True)
class NormalizedGraphFacts:
    repositories: tuple[Repository, ...] = ()
    files: tuple[File, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    services: tuple[Service, ...] = ()
    contracts: tuple[Contract, ...] = ()
    concepts: tuple[Concept, ...] = ()
    decisions: tuple[Decision, ...] = ()
    edges: tuple[Edge, ...] = ()


def normalize_graph_facts(
    *,
    repositories: tuple[Repository, ...] = (),
    files: tuple[File, ...] = (),
    symbols: tuple[Symbol, ...] = (),
    services: tuple[Service, ...] = (),
    contracts: tuple[Contract, ...] = (),
    concepts: tuple[Concept, ...] = (),
    decisions: tuple[Decision, ...] = (),
    edges: tuple[Edge, ...] = (),
) -> NormalizedGraphFacts:
    contract_id_map = {
        contract.id: normalized_contract_id(contract) for contract in contracts
    }
    symbol_id_map = {symbol.id: normalized_symbol_id(symbol) for symbol in symbols}
    service_id_map = _normalized_service_id_map(services)
    normalized_contracts = tuple(
        replace(contract, id=contract_id_map[contract.id]) for contract in contracts
    )
    normalized_symbols = tuple(
        replace(symbol, id=symbol_id_map[symbol.id]) for symbol in symbols
    )
    normalized_services = tuple(
        replace(
            service,
            id=service_id_map[service.id],
            aliases=_service_aliases(service),
        )
        for service in services
    )
    normalized_edges = tuple(_normalize_edge(edge, contract_id_map) for edge in edges)
    normalized_edges = tuple(
        _normalize_symbol_edge(edge, symbol_id_map) for edge in normalized_edges
    )
    normalized_edges = tuple(
        _normalize_service_edge(edge, service_id_map) for edge in normalized_edges
    )

    return NormalizedGraphFacts(
        repositories=_dedupe_by_id(
            repositories,
            _merge_repository,
            _source_precedence,
        ),
        files=_dedupe_by_id(files, _merge_file, _source_precedence),
        symbols=_dedupe_by_id(normalized_symbols, _merge_symbol, _source_precedence),
        services=_dedupe_by_id(normalized_services, _merge_service),
        contracts=_dedupe_by_id(
            normalized_contracts,
            _merge_contract,
            _source_precedence,
        ),
        concepts=_dedupe_by_id(concepts, _merge_concept, _source_precedence),
        decisions=_dedupe_by_id(decisions, _merge_decision, _source_precedence),
        edges=_dedupe_edges(normalized_edges),
    )


def normalized_contract_id(contract: Contract) -> str:
    if contract.kind != "http":
        return contract.id

    repository_id = _repository_id(contract)
    if repository_id is None:
        return contract.id

    return canonical_http_contract_id(repository_id, contract.method, contract.path)


def normalized_symbol_id(symbol: Symbol) -> str:
    parts = symbol.id.split(":")
    if len(parts) < 5 or parts[0] != "symbol":
        return symbol.id

    repository_id = parts[1]
    file_path = ":".join(parts[2:-2])
    extractor_segment = parts[-2]
    if extractor_segment not in {"graphify", "lsp", "tree_sitter"}:
        return symbol.id

    qualified_name = symbol.qualified_name or symbol.name
    symbol_slug = slugify_identifier(qualified_name, fallback="symbol")
    return f"symbol:{repository_id}:{file_path}:{symbol_slug}"


def _normalized_service_id_map(services: tuple[Service, ...]) -> dict[str, str]:
    if not services:
        return {}

    keys_by_id = {service.id: _service_alias_keys(service) for service in services}
    grouped: dict[str, list[Service]] = {}
    for service in services:
        group_key = (
            min(keys_by_id[service.id]) if keys_by_id[service.id] else service.id
        )
        matching_keys = set(keys_by_id[service.id])
        changed = True
        while changed:
            changed = False
            for other in services:
                other_keys = keys_by_id[other.id]
                if other_keys & matching_keys:
                    before = len(matching_keys)
                    matching_keys |= other_keys
                    if len(matching_keys) != before:
                        changed = True
        group_key = min(matching_keys) if matching_keys else group_key
        grouped.setdefault(group_key, []).append(service)

    service_id_map: dict[str, str] = {}
    for service_group in grouped.values():
        canonical_id = _canonical_service_id(tuple(service_group))
        for service in service_group:
            service_id_map[service.id] = canonical_id
    return service_id_map


def _canonical_service_id(services: tuple[Service, ...]) -> str:
    alias_slugs = {
        slugify_identifier(_split_camel_case(alias), fallback="service")
        for service in services
        for alias in _service_alias_values(service)
    }
    core_slugs = {
        _service_core_slug(alias)
        for service in services
        for alias in _service_alias_values(service)
    }
    service_slugs = {
        slug
        for slug in alias_slugs
        if slug.endswith("-service")
        and slug.removesuffix("-service") in core_slugs
        and slug in {_service_id_slug(service.id) for service in services}
    }
    if service_slugs:
        return f"service:{min(service_slugs)}"
    return min(service.id for service in services)


def _service_id_slug(service_id: str) -> str:
    return service_id.rsplit(":", maxsplit=1)[-1]


def _service_alias_keys(service: Service) -> set[str]:
    return {
        _service_core_slug(alias)
        for alias in _service_alias_values(service)
        if _service_core_slug(alias)
    }


def _service_aliases(service: Service) -> tuple[str, ...]:
    return tuple(sorted(_service_alias_values(service)))


def _service_alias_values(service: Service) -> set[str]:
    aliases = {service.name, *service.aliases}
    for evidence in service.evidence:
        aliases.update(
            str(value)
            for key, value in evidence.metadata.items()
            if key in {"service_name", "service_type", "symbol", "domain"}
            and isinstance(value, str)
            and value
        )
    return {alias for alias in aliases if alias}


def _service_core_slug(value: str) -> str:
    slug = slugify_identifier(_split_camel_case(value), fallback="service")
    for suffix in ("-service", "-domain"):
        if slug.endswith(suffix):
            slug = slug.removesuffix(suffix)
    return slug or "service"


def _split_camel_case(value: str) -> str:
    parts: list[str] = []
    previous = ""
    for character in value:
        if (
            previous
            and character.isupper()
            and (previous.islower() or previous.isdigit())
        ):
            parts.append(" ")
        parts.append(character)
        previous = character
    return "".join(parts)


def _normalize_edge(
    edge: Edge,
    contract_id_map: dict[str, str],
) -> Edge:
    source_id = (
        contract_id_map.get(edge.source_id, edge.source_id)
        if edge.source_type == "contract"
        else edge.source_id
    )
    target_id = (
        contract_id_map.get(edge.target_id, edge.target_id)
        if edge.target_type == "contract"
        else edge.target_id
    )
    relation_type = _normalized_relation_type(edge)
    if (
        source_id == edge.source_id
        and target_id == edge.target_id
        and relation_type == edge.relation_type
    ):
        return edge
    return replace(
        edge,
        source_id=source_id,
        target_id=target_id,
        relation_type=relation_type,
    )


def _normalized_relation_type(edge: Edge) -> str:
    if (
        edge.source_type == "service"
        and edge.target_type == "contract"
        and edge.relation_type == "exposes"
    ):
        return "produces"
    return edge.relation_type


def _normalize_symbol_edge(
    edge: Edge,
    symbol_id_map: dict[str, str],
) -> Edge:
    source_id = (
        symbol_id_map.get(edge.source_id, edge.source_id)
        if edge.source_type == "symbol"
        else edge.source_id
    )
    target_id = (
        symbol_id_map.get(edge.target_id, edge.target_id)
        if edge.target_type == "symbol"
        else edge.target_id
    )
    if source_id == edge.source_id and target_id == edge.target_id:
        return edge
    return replace(edge, source_id=source_id, target_id=target_id)


def _normalize_service_edge(
    edge: Edge,
    service_id_map: dict[str, str],
) -> Edge:
    source_id = (
        service_id_map.get(edge.source_id, edge.source_id)
        if edge.source_type == "service"
        else edge.source_id
    )
    target_id = (
        service_id_map.get(edge.target_id, edge.target_id)
        if edge.target_type == "service"
        else edge.target_id
    )
    if source_id == edge.source_id and target_id == edge.target_id:
        return edge
    return replace(edge, source_id=source_id, target_id=target_id)


def _dedupe_by_id(
    values: tuple[T, ...],
    merge: Callable[[T, T], T],
    precedence: Callable[[T], int] | None = None,
) -> tuple[T, ...]:
    merged: dict[str, T] = {}
    ordered_values = sorted(
        values,
        key=lambda value: (
            value.id,
            precedence(value) if precedence is not None else 0,
            _value_key(value),
        ),
    )
    for value in ordered_values:
        value_id = value.id
        if value_id in merged:
            merged[value_id] = merge(merged[value_id], value)
        else:
            merged[value_id] = value
    return tuple(merged[key] for key in sorted(merged))


def _merge_symbol(existing: Symbol, incoming: Symbol) -> Symbol:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        (
            "file_id",
            "name",
            "kind",
            "qualified_name",
            "start_line",
            "end_line",
        ),
        lambda field_name: _prefer_ranked(existing, incoming, field_name),
    )
    return Symbol(
        id=existing.id,
        file_id=_prefer_ranked(existing, incoming, "file_id") or existing.file_id,
        name=_prefer_ranked(existing, incoming, "name") or existing.name,
        kind=_prefer_ranked(existing, incoming, "kind") or existing.kind,
        qualified_name=_prefer_ranked(existing, incoming, "qualified_name"),
        start_line=_prefer_ranked(existing, incoming, "start_line"),
        end_line=_prefer_ranked(existing, incoming, "end_line"),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _merge_service(existing: Service, incoming: Service) -> Service:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        ("name", "repository_id", "description"),
        lambda field_name: _prefer_service_field(existing, incoming, field_name),
    )
    return Service(
        id=existing.id,
        name=_prefer_service_field(existing, incoming, "name") or existing.name,
        repository_id=_prefer_service_field(existing, incoming, "repository_id"),
        description=_prefer_service_field(existing, incoming, "description"),
        aliases=_merge_string_tuples(existing.aliases, incoming.aliases),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _merge_contract(existing: Contract, incoming: Contract) -> Contract:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        (
            "name",
            "kind",
            "path",
            "version",
            "method",
            "producer_hint",
            "operation_id",
        ),
        lambda field_name: _prefer_ranked(existing, incoming, field_name),
    )
    return Contract(
        id=existing.id,
        name=_prefer_ranked(existing, incoming, "name") or existing.name,
        kind=_prefer_ranked(existing, incoming, "kind") or existing.kind,
        path=_prefer_ranked(existing, incoming, "path"),
        version=_prefer_ranked(existing, incoming, "version"),
        method=_prefer_ranked(existing, incoming, "method"),
        producer_hint=_prefer_ranked(existing, incoming, "producer_hint"),
        operation_id=_prefer_ranked(existing, incoming, "operation_id"),
        request_dtos=_merge_string_tuples(existing.request_dtos, incoming.request_dtos),
        response_dtos=_merge_string_tuples(
            existing.response_dtos,
            incoming.response_dtos,
        ),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _merge_concept(existing: Concept, incoming: Concept) -> Concept:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        ("name", "description"),
        lambda field_name: _prefer_stable_string(
            getattr(existing, field_name),
            getattr(incoming, field_name),
        ),
    )
    return Concept(
        id=existing.id,
        name=_prefer_stable_string(existing.name, incoming.name) or existing.name,
        description=_prefer_stable_string(existing.description, incoming.description),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _merge_decision(existing: Decision, incoming: Decision) -> Decision:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        (
            "title",
            "status",
            "adr_id",
            "path",
            "decided_at",
            "context",
            "decision",
            "consequences",
        ),
        lambda field_name: _prefer_ranked(existing, incoming, field_name),
    )
    return Decision(
        id=existing.id,
        title=_prefer_ranked(existing, incoming, "title") or existing.title,
        status=_prefer_ranked(existing, incoming, "status") or existing.status,
        adr_id=_prefer_ranked(existing, incoming, "adr_id"),
        path=_prefer_ranked(existing, incoming, "path"),
        decided_at=_prefer_ranked(existing, incoming, "decided_at"),
        context=_prefer_ranked(existing, incoming, "context"),
        decision=_prefer_ranked(existing, incoming, "decision"),
        consequences=_prefer_ranked(existing, incoming, "consequences"),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _dedupe_edges(edges: tuple[Edge, ...]) -> tuple[Edge, ...]:
    merged: dict[tuple[str, str, str, str, str], Edge] = {}
    for edge in sorted(
        edges, key=lambda edge: (*_edge_key(edge), _source_precedence(edge))
    ):
        key = (
            edge.source_type,
            edge.source_id,
            edge.target_type,
            edge.target_id,
            edge.relation_type,
        )
        if key in merged:
            existing = merged[key]
            confidence = _merge_edge_confidence(existing, edge)
            merged[key] = replace(
                existing,
                confidence=confidence,
                evidence=_merge_evidence(existing.evidence, edge.evidence),
            )
        else:
            merged[key] = edge
    return tuple(merged[key] for key in sorted(merged))


def _merge_repository(existing: Repository, incoming: Repository) -> Repository:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        ("name", "path", "remote_url", "default_branch"),
        lambda field_name: _prefer_ranked(existing, incoming, field_name),
    )
    return Repository(
        id=existing.id,
        name=_prefer_ranked(existing, incoming, "name") or existing.name,
        path=_prefer_ranked(existing, incoming, "path") or existing.path,
        remote_url=_prefer_ranked(existing, incoming, "remote_url"),
        default_branch=_prefer_ranked(existing, incoming, "default_branch"),
        language_hints=_merge_string_tuples(
            existing.language_hints,
            incoming.language_hints,
        ),
        framework_hints=_merge_string_tuples(
            existing.framework_hints,
            incoming.framework_hints,
        ),
        module_hints=_merge_string_tuples(
            existing.module_hints,
            incoming.module_hints,
        ),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _merge_file(existing: File, incoming: File) -> File:
    conflicts = _scalar_conflicts(
        existing,
        incoming,
        (
            "repository_id",
            "path",
            "kind",
            "extension",
            "language",
            "content_hash",
            "modified_at",
        ),
        lambda field_name: _prefer_ranked(existing, incoming, field_name),
    )
    return File(
        id=existing.id,
        repository_id=_prefer_ranked(existing, incoming, "repository_id")
        or existing.repository_id,
        path=_prefer_ranked(existing, incoming, "path") or existing.path,
        kind=_prefer_ranked(existing, incoming, "kind") or existing.kind,
        extension=_prefer_ranked(existing, incoming, "extension") or existing.extension,
        language=_prefer_ranked(existing, incoming, "language"),
        content_hash=_prefer_ranked(existing, incoming, "content_hash"),
        modified_at=_prefer_ranked(existing, incoming, "modified_at"),
        evidence=_merge_conflict_evidence(existing, incoming, conflicts),
    )


def _merge_evidence(
    existing: tuple[Evidence, ...],
    incoming: tuple[Evidence, ...],
) -> tuple[Evidence, ...]:
    evidence_by_key: dict[str, Evidence] = {}
    for evidence in (*existing, *incoming):
        evidence_by_key.setdefault(_evidence_key(evidence), evidence)
    return tuple(evidence_by_key[key] for key in sorted(evidence_by_key))


def _merge_string_tuples(
    existing: tuple[str, ...],
    incoming: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(sorted({*existing, *incoming}))


def _merge_conflict_evidence(
    existing: object,
    incoming: object,
    conflicts: tuple[tuple[str, str, object, object], ...],
) -> tuple[Evidence, ...]:
    existing_evidence = _annotate_conflict_evidence(
        getattr(existing, "evidence", ()),
        conflicts,
        "existing",
    )
    incoming_evidence = _annotate_conflict_evidence(
        getattr(incoming, "evidence", ()),
        conflicts,
        "incoming",
    )
    return _merge_evidence(existing_evidence, incoming_evidence)


def _scalar_conflicts(
    existing: object,
    incoming: object,
    fields: tuple[str, ...],
    select: Callable[[str], object],
) -> tuple[tuple[str, str, object, object], ...]:
    conflicts: list[tuple[str, str, object, object]] = []
    for field_name in fields:
        existing_value = getattr(existing, field_name)
        incoming_value = getattr(incoming, field_name)
        if (
            _is_empty_value(existing_value)
            or _is_empty_value(incoming_value)
            or existing_value == incoming_value
        ):
            continue

        resolved_value = select(field_name)
        if existing_value != resolved_value:
            conflicts.append(
                ("existing", field_name, existing_value, resolved_value),
            )
        if incoming_value != resolved_value:
            conflicts.append(
                ("incoming", field_name, incoming_value, resolved_value),
            )
    return tuple(conflicts)


def _annotate_conflict_evidence(
    evidence_items: tuple[Evidence, ...],
    conflicts: tuple[tuple[str, str, object, object], ...],
    side: str,
) -> tuple[Evidence, ...]:
    annotations = tuple(
        {
            "field": field_name,
            "conflicting_value": conflicting_value,
            "resolved_value": resolved_value,
        }
        for conflict_side, field_name, conflicting_value, resolved_value in conflicts
        if conflict_side == side
    )
    if not annotations:
        return evidence_items

    return tuple(
        replace(
            evidence,
            metadata={
                **evidence.metadata,
                "resolver_conflicts": (
                    *tuple(evidence.metadata.get("resolver_conflicts", ())),
                    *annotations,
                ),
            },
        )
        for evidence in evidence_items
    )


def _prefer_ranked(existing: T, incoming: T, field_name: str) -> object:
    existing_value = getattr(existing, field_name)
    incoming_value = getattr(incoming, field_name)
    if _is_empty_value(existing_value):
        return incoming_value
    if _is_empty_value(incoming_value):
        return existing_value

    existing_rank = _source_precedence(existing)
    incoming_rank = _source_precedence(incoming)
    if incoming_rank > existing_rank:
        return incoming_value
    if existing_rank > incoming_rank:
        return existing_value
    return min(existing_value, incoming_value)


def _prefer_service_field(
    existing: Service,
    incoming: Service,
    field_name: str,
) -> str | None:
    existing_value = getattr(existing, field_name)
    incoming_value = getattr(incoming, field_name)
    if field_name != "name":
        return _prefer_stable_string(existing_value, incoming_value)

    id_slug = _service_id_slug(existing.id)
    for value in (existing_value, incoming_value):
        if (
            isinstance(value, str)
            and slugify_identifier(_split_camel_case(value), fallback="service")
            == id_slug
        ):
            return value
    return _prefer_stable_string(existing_value, incoming_value)


def _prefer_stable_string(existing: str | None, incoming: str | None) -> str | None:
    values = tuple(value for value in (existing, incoming) if value)
    return min(values) if values else None


def _is_empty_value(value: object) -> bool:
    return value is None or value == ""


def _source_precedence(value: object) -> int:
    evidence = getattr(value, "evidence", ())
    return max((_evidence_precedence(item) for item in evidence), default=20)


def _evidence_precedence(evidence: Evidence) -> int:
    if evidence.extractor_name == "docs" and evidence.metadata.get("explicitly_linked"):
        return 35
    return _EXTRACTOR_PRECEDENCE.get(evidence.extractor_name, 20)


_EXTRACTOR_PRECEDENCE: dict[str, int] = {
    "docs": 0,
    "adr": 10,
    "adr_markdown": 10,
    "frontend_api": 25,
    "heuristic": 25,
    "tree_sitter": 30,
    "graphify": 40,
    "lsp": 45,
    "dotnet": 45,
    "nestjs": 45,
    "nextjs": 45,
    "openapi": 60,
    "asyncapi": 60,
}


def _merge_edge_confidence(existing: Edge, incoming: Edge) -> float:
    existing_rank = _edge_confidence_precedence(existing)
    incoming_rank = _edge_confidence_precedence(incoming)
    if incoming_rank is None and existing_rank is None:
        return max(existing.confidence, incoming.confidence)
    if incoming_rank is None:
        return incoming.confidence
    if existing_rank is None:
        return existing.confidence
    if incoming_rank > existing_rank:
        return incoming.confidence
    if existing_rank > incoming_rank:
        return existing.confidence
    return max(existing.confidence, incoming.confidence)


def _edge_confidence_precedence(edge: Edge) -> int | None:
    extractors = {evidence.extractor_name for evidence in edge.evidence}
    if "lsp" in extractors:
        return _EXTRACTOR_PRECEDENCE["lsp"]
    if "tree_sitter" in extractors:
        return _EXTRACTOR_PRECEDENCE["tree_sitter"]
    if "docs" in extractors:
        return _EXTRACTOR_PRECEDENCE["docs"]
    if "adr" in extractors or "adr_markdown" in extractors:
        return _EXTRACTOR_PRECEDENCE["adr"]
    return None


def _repository_id(contract: Contract) -> str | None:
    for evidence in contract.evidence:
        repository_id = evidence.metadata.get("repository_id")
        if isinstance(repository_id, str) and repository_id:
            return repository_id
    parts = contract.id.split(":")
    if len(parts) >= 2 and parts[0] == "contract":
        return parts[1]
    return None


def _evidence_key(evidence: Evidence) -> str:
    return json.dumps(
        {key: value for key, value in asdict(evidence).items() if key != "assessment"},
        sort_keys=True,
    )


def _value_key(value: object) -> str:
    payload = asdict(value)
    for evidence in payload.get("evidence", ()):
        evidence.pop("assessment", None)
    return json.dumps(payload, sort_keys=True)


def _edge_key(edge: Edge) -> tuple[str, str, str, str, str]:
    return (
        edge.source_type,
        edge.source_id,
        edge.target_type,
        edge.target_id,
        edge.relation_type,
    )
