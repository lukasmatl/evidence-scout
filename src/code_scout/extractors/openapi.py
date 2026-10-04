from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_scout.discovery import DiscoveredFile
from code_scout.domain.normalize import normalized_contract_id
from code_scout.models.facts import (
    ActiveFact,
    EntityFact,
    ExtractorMetadata,
    FactEvidence,
    FactReference,
    RelationFact,
    contract_to_entity_facts,
    entity_fact_to_active_fact,
    relation_fact_to_active_fact,
)
from code_scout.models.graph import Concept, Contract, Edge, Evidence
from code_scout.utils.identifiers import canonical_http_contract_id, slugify_identifier

EXTRACTOR_NAME = "openapi"
EXACT_PARSED_CONFIDENCE = 1.0
INFERRED_FACT_CONFIDENCE = 0.85
HTTP_METHODS = frozenset(
    {
        "get",
        "put",
        "post",
        "delete",
        "options",
        "head",
        "patch",
        "trace",
    },
)
OPENAPI_CANDIDATE_NAMES = frozenset(
    {
        "openapi.json",
        "openapi.yaml",
        "openapi.yml",
        "swagger.json",
        "swagger.yaml",
        "swagger.yml",
    },
)


@dataclass(frozen=True)
class OpenAPIOperation:
    method: str
    path: str
    operation_id: str | None = None
    tags: tuple[str, ...] = ()
    request_dtos: tuple[str, ...] = ()
    response_dtos: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenAPISchema:
    name: str
    schema_type: str | None = None
    required: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenAPIMetadata:
    title: str
    version: str
    spec_version: str
    path_count: int
    operations: tuple[OpenAPIOperation, ...] = ()
    schemas: tuple[OpenAPISchema, ...] = ()


@dataclass(frozen=True)
class OpenAPIWarning:
    repository_id: str
    path: str
    message: str


@dataclass(frozen=True)
class OpenAPIConceptLink:
    concept: Concept
    edge: Edge


def parse_openapi_file(
    discovered_file: DiscoveredFile,
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[OpenAPIMetadata | None, OpenAPIWarning | None]:
    if discovered_file.extension not in {".json", ".yaml", ".yml"}:
        return None, None

    is_named_candidate = _is_named_candidate(discovered_file.path)
    is_configured_candidate = _is_configured_candidate(
        discovered_file.path,
        configured_paths,
    )

    try:
        payload = _load_mapping(discovered_file)
    except (OSError, ValueError) as error:
        if is_named_candidate or is_configured_candidate:
            return None, _warning(discovered_file, f"OpenAPI file is invalid: {error}.")
        return None, None

    metadata, reason = _metadata_from_payload(payload)
    if metadata is not None:
        return metadata, None
    if is_named_candidate or is_configured_candidate or _looks_like_openapi(payload):
        return None, _warning(discovered_file, reason)
    return None, None


def openapi_contracts(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[tuple[Contract, ...], tuple[OpenAPIWarning, ...]]:
    contracts: list[Contract] = []
    warnings: list[OpenAPIWarning] = []

    for discovered_file in discovered_files:
        metadata, warning = parse_openapi_file(
            discovered_file,
            configured_paths=configured_paths,
        )
        if warning is not None:
            warnings.append(warning)
        if metadata is None:
            continue
        contracts.extend(
            _operation_contracts(discovered_file, metadata),
        )

    return tuple(contracts), tuple(warnings)


def openapi_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[tuple[ActiveFact, ...], tuple[OpenAPIWarning, ...]]:
    active_facts: list[ActiveFact] = []
    warnings: list[OpenAPIWarning] = []

    for discovered_file in discovered_files:
        metadata, warning = parse_openapi_file(
            discovered_file,
            configured_paths=configured_paths,
        )
        if warning is not None:
            warnings.append(warning)
        if metadata is None:
            continue
        active_facts.extend(_active_facts_for_metadata(discovered_file, metadata))

    return tuple(active_facts), tuple(warnings)


def openapi_contract_edges(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[Edge, ...]:
    edges: list[Edge] = []

    for discovered_file in discovered_files:
        metadata, _warning_value = parse_openapi_file(
            discovered_file,
            configured_paths=configured_paths,
        )
        if metadata is None:
            continue
        edges.extend(
            Edge(
                source_type="file",
                source_id=_file_id(discovered_file.repository_id, discovered_file.path),
                target_type="contract",
                target_id=_operation_contract_id(
                    discovered_file.repository_id,
                    discovered_file.path,
                    operation,
                ),
                relation_type="declares_contract",
                confidence=1.0,
                evidence=(_operation_evidence(discovered_file, metadata, operation),),
            )
            for operation in metadata.operations
        )

    return tuple(edges)


def openapi_concept_links(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[OpenAPIConceptLink, ...]:
    links: list[OpenAPIConceptLink] = []

    for discovered_file in discovered_files:
        metadata, _warning_value = parse_openapi_file(
            discovered_file,
            configured_paths=configured_paths,
        )
        if metadata is None:
            continue
        for operation in metadata.operations:
            links.extend(_operation_concept_links(discovered_file, operation))

    return tuple(links)


def _load_mapping(discovered_file: DiscoveredFile) -> dict[str, Any]:
    text = Path(discovered_file.absolute_path).read_text(encoding="utf-8")
    if discovered_file.extension == ".json":
        payload = json.loads(text)
    else:
        payload = _parse_yaml_subset(text)
    if not isinstance(payload, dict):
        raise ValueError("top-level document must be a mapping")
    return payload


def _metadata_from_payload(
    payload: dict[str, Any],
) -> tuple[OpenAPIMetadata | None, str]:
    spec_version = _spec_version(payload)
    if spec_version is None:
        return None, "OpenAPI file is missing an `openapi` or `swagger` version."

    info = payload.get("info")
    if not isinstance(info, dict):
        return None, "OpenAPI file is missing an `info` mapping."

    title = info.get("title")
    if not isinstance(title, str) or not title:
        return None, "OpenAPI file is missing a non-empty `info.title` string."

    version = info.get("version")
    if not isinstance(version, str) or not version:
        return None, "OpenAPI file is missing a non-empty `info.version` string."

    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return None, "OpenAPI file is missing a `paths` mapping."
    operations = _operations_from_paths(paths)
    schemas = _schemas_from_payload(payload)

    return (
        OpenAPIMetadata(
            title=title,
            version=version,
            spec_version=spec_version,
            path_count=len(paths),
            operations=operations,
            schemas=schemas,
        ),
        "",
    )


def _operations_from_paths(paths: dict[str, Any]) -> tuple[OpenAPIOperation, ...]:
    operations: list[OpenAPIOperation] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue
        for method, operation_payload in path_item.items():
            normalized_method = method.lower()
            if normalized_method not in HTTP_METHODS:
                continue
            if not isinstance(operation_payload, dict):
                continue
            operation_id = operation_payload.get("operationId")
            raw_tags = operation_payload.get("tags", ())
            operations.append(
                OpenAPIOperation(
                    method=normalized_method.upper(),
                    path=path,
                    operation_id=operation_id
                    if isinstance(operation_id, str) and operation_id
                    else None,
                    tags=_string_tuple(raw_tags),
                    request_dtos=_request_dtos(operation_payload),
                    response_dtos=_response_dtos(operation_payload),
                ),
            )
    return tuple(operations)


def _schemas_from_payload(payload: dict[str, Any]) -> tuple[OpenAPISchema, ...]:
    components = payload.get("components")
    if not isinstance(components, dict):
        return ()
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return ()

    parsed: list[OpenAPISchema] = []
    for name, schema_payload in schemas.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(schema_payload, dict)
        ):
            continue
        schema_type = schema_payload.get("type")
        required = schema_payload.get("required", ())
        properties = schema_payload.get("properties", {})
        parsed.append(
            OpenAPISchema(
                name=name,
                schema_type=schema_type if isinstance(schema_type, str) else None,
                required=_string_tuple(required),
                properties=tuple(
                    property_name
                    for property_name in properties
                    if isinstance(property_name, str)
                )
                if isinstance(properties, dict)
                else (),
            ),
        )
    return tuple(parsed)


def _operation_contracts(
    discovered_file: DiscoveredFile,
    metadata: OpenAPIMetadata,
) -> tuple[Contract, ...]:
    return tuple(
        Contract(
            id=_operation_contract_id(
                discovered_file.repository_id,
                discovered_file.path,
                operation,
            ),
            name=operation.operation_id or f"{operation.method} {operation.path}",
            kind="http",
            path=operation.path,
            version=metadata.version,
            method=operation.method,
            producer_hint=metadata.title,
            operation_id=operation.operation_id,
            request_dtos=operation.request_dtos,
            response_dtos=operation.response_dtos,
            evidence=(_operation_evidence(discovered_file, metadata, operation),),
        )
        for operation in metadata.operations
    )


def _active_facts_for_metadata(
    discovered_file: DiscoveredFile,
    metadata: OpenAPIMetadata,
) -> tuple[ActiveFact, ...]:
    evidence = _metadata_fact_evidence(discovered_file, metadata)
    extractor_metadata = ExtractorMetadata(name=EXTRACTOR_NAME)
    contracts = _operation_contracts(discovered_file, metadata)
    endpoint_facts = tuple(
        entity_fact_to_active_fact(fact)
        for contract in contracts
        for fact in contract_to_entity_facts(contract)
    )
    operation_facts = tuple(
        entity_fact_to_active_fact(
            EntityFact(
                entity_type="operation",
                entity_id=_operation_id(
                    discovered_file.repository_id,
                    discovered_file.path,
                    operation,
                ),
                properties={
                    "repository_id": discovered_file.repository_id,
                    "spec_path": discovered_file.path,
                    "service_id": _service_id(
                        discovered_file.repository_id,
                        metadata.title,
                    ),
                    "endpoint_id": normalized_contract_id(contract),
                    "method": operation.method,
                    "path": operation.path,
                    "operation_id": operation.operation_id,
                    "tags": operation.tags,
                    "request_dtos": operation.request_dtos,
                    "response_dtos": operation.response_dtos,
                },
                evidence=(evidence,),
                confidence=EXACT_PARSED_CONFIDENCE,
                extractor_metadata=extractor_metadata,
            ),
        )
        for operation, contract in zip(metadata.operations, contracts, strict=True)
    )
    schema_facts = tuple(
        entity_fact_to_active_fact(
            EntityFact(
                entity_type="schema",
                entity_id=_schema_id_for_fact(discovered_file.repository_id, schema),
                properties={
                    "repository_id": discovered_file.repository_id,
                    "spec_path": discovered_file.path,
                    "name": schema.name,
                    "type": schema.schema_type,
                    "required": schema.required,
                    "properties": schema.properties,
                    "title": metadata.title,
                    "version": metadata.version,
                },
                evidence=(evidence,),
                confidence=EXACT_PARSED_CONFIDENCE,
                extractor_metadata=extractor_metadata,
            ),
        )
        for schema in metadata.schemas
    )
    service_contract_relation_facts = (
        ()
        if _is_external_spec_path(discovered_file.path)
        else tuple(
            relation_fact_to_active_fact(
                RelationFact(
                    subject=FactReference(
                        type="service",
                        id=_service_id(discovered_file.repository_id, metadata.title),
                    ),
                    relation="produces",
                    object=FactReference(
                        type="contract", id=normalized_contract_id(contract)
                    ),
                    properties={
                        "repository_id": discovered_file.repository_id,
                        "spec_path": discovered_file.path,
                        "service_name": metadata.title,
                        "method": operation.method,
                        "path": operation.path,
                        "operation_id": operation.operation_id,
                    },
                    evidence=(evidence,),
                    confidence=EXACT_PARSED_CONFIDENCE,
                    extractor_metadata=extractor_metadata,
                ),
            )
            for operation, contract in zip(metadata.operations, contracts, strict=True)
        )
    )
    return (
        *endpoint_facts,
        *operation_facts,
        *schema_facts,
        *service_contract_relation_facts,
    )


def _metadata_fact_evidence(
    discovered_file: DiscoveredFile,
    metadata: OpenAPIMetadata,
) -> FactEvidence:
    return FactEvidence(
        file_path=discovered_file.path,
        extractor_name=EXTRACTOR_NAME,
        confidence=EXACT_PARSED_CONFIDENCE,
        start_line=1,
        end_line=_line_count(discovered_file.absolute_path),
        metadata={
            "repository_id": discovered_file.repository_id,
            "title": metadata.title,
            "version": metadata.version,
            "spec_version": metadata.spec_version,
            "path_count": metadata.path_count,
        },
    )


def _operation_evidence(
    discovered_file: DiscoveredFile,
    metadata: OpenAPIMetadata,
    operation: OpenAPIOperation,
) -> Evidence:
    return Evidence(
        file_path=discovered_file.path,
        extractor_name=EXTRACTOR_NAME,
        confidence=EXACT_PARSED_CONFIDENCE,
        start_line=1,
        end_line=_line_count(discovered_file.absolute_path),
        metadata={
            "repository_id": discovered_file.repository_id,
            "title": metadata.title,
            "version": metadata.version,
            "spec_version": metadata.spec_version,
            "path_count": metadata.path_count,
            "method": operation.method,
            "path": operation.path,
            "operation_id": operation.operation_id,
            "tags": list(operation.tags),
            "request_dtos": list(operation.request_dtos),
            "response_dtos": list(operation.response_dtos),
        },
    )


def _operation_concept_links(
    discovered_file: DiscoveredFile,
    operation: OpenAPIOperation,
) -> tuple[OpenAPIConceptLink, ...]:
    links: list[OpenAPIConceptLink] = []
    candidates = [("tag", tag, tag) for tag in operation.tags] + [
        ("operation_id", operation.operation_id, operation.operation_id)
        for _value in (operation.operation_id,)
        if operation.operation_id is not None
    ]
    seen_slugs: set[str] = set()
    for source, name, raw_slug in candidates:
        if not name or not raw_slug:
            continue
        concept_slug = slugify_identifier(raw_slug, fallback="openapi")
        if concept_slug in seen_slugs:
            continue
        seen_slugs.add(concept_slug)
        concept = Concept(
            id=f"concept:{discovered_file.repository_id}:{concept_slug}",
            name=name,
        )
        links.append(
            OpenAPIConceptLink(
                concept=concept,
                edge=Edge(
                    source_type="concept",
                    source_id=concept.id,
                    target_type="contract",
                    target_id=_operation_contract_id(
                        discovered_file.repository_id,
                        discovered_file.path,
                        operation,
                    ),
                    relation_type="describes_contract",
                    confidence=INFERRED_FACT_CONFIDENCE,
                    evidence=(
                        Evidence(
                            file_path=discovered_file.path,
                            extractor_name=EXTRACTOR_NAME,
                            confidence=INFERRED_FACT_CONFIDENCE,
                            start_line=1,
                            end_line=_line_count(discovered_file.absolute_path),
                            metadata={
                                "source": source,
                                "concept_slug": concept_slug,
                                "repository_id": discovered_file.repository_id,
                                "method": operation.method,
                                "path": operation.path,
                            },
                        ),
                    ),
                ),
            ),
        )
    return tuple(links)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _request_dtos(operation_payload: dict[str, Any]) -> tuple[str, ...]:
    request_body = operation_payload.get("requestBody")
    if not isinstance(request_body, dict):
        return ()
    content = request_body.get("content")
    if not isinstance(content, dict):
        return ()
    return _content_schema_ids(content, "request")


def _response_dtos(operation_payload: dict[str, Any]) -> tuple[str, ...]:
    responses = operation_payload.get("responses")
    if not isinstance(responses, dict):
        return ()
    dtos: list[str] = []
    for status, response in responses.items():
        if not _is_success_status(status) or not isinstance(response, dict):
            continue
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        dtos.extend(_content_schema_ids(content, f"response:{status}"))
    return _dedupe(dtos)


def _content_schema_ids(content: dict[str, Any], inline_prefix: str) -> tuple[str, ...]:
    dtos: list[str] = []
    for media_type, media_payload in content.items():
        if not isinstance(media_type, str) or not isinstance(media_payload, dict):
            continue
        schema = media_payload.get("schema")
        if not isinstance(schema, dict):
            continue
        dto = _schema_id(schema, inline_prefix, media_type)
        if dto is not None:
            dtos.append(dto)
    return _dedupe(dtos)


def _schema_id(
    schema: dict[str, Any],
    inline_prefix: str,
    media_type: str,
) -> str | None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref:
        return ref.rsplit("/", 1)[-1]

    title = schema.get("title")
    if isinstance(title, str) and title:
        return title

    if schema:
        media_slug = slugify_identifier(media_type, fallback="body")
        prefix_slug = slugify_identifier(inline_prefix, fallback="schema")
        return f"inline:{prefix_slug}:{media_slug}"
    return None


def _is_success_status(status: Any) -> bool:
    if not isinstance(status, str):
        return False
    return len(status) == 3 and status.startswith("2") and status[1:].isdigit()


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)


def _looks_like_openapi(payload: dict[str, Any]) -> bool:
    return "openapi" in payload or "swagger" in payload


def _spec_version(payload: dict[str, Any]) -> str | None:
    value = payload.get("openapi", payload.get("swagger"))
    return value if isinstance(value, str) and value else None


def _is_named_candidate(path: str) -> bool:
    return Path(path).name.lower() in OPENAPI_CANDIDATE_NAMES


def _is_configured_candidate(path: str, configured_paths: tuple[str, ...]) -> bool:
    normalized_path = _normalize_path(path)
    for configured_path in configured_paths:
        normalized_candidate = _normalize_path(configured_path)
        if normalized_path == normalized_candidate or normalized_path.startswith(
            f"{normalized_candidate}/",
        ):
            return True
    return False


def _normalize_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./").strip("/")


def _is_external_spec_path(path: str) -> bool:
    normalized_path = _normalize_path(path).lower()
    parts = tuple(part for part in normalized_path.split("/") if part)
    return bool(
        (parts and parts[0] in {"external", "vendor", "third-party", "third_party"})
        or "/external/" in f"/{normalized_path}/"
    )


def _warning(discovered_file: DiscoveredFile, message: str) -> OpenAPIWarning:
    return OpenAPIWarning(
        repository_id=discovered_file.repository_id,
        path=discovered_file.path,
        message=message,
    )


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _operation_contract_id(
    repository_id: str,
    spec_path: str,
    operation: OpenAPIOperation,
) -> str:
    return canonical_http_contract_id(repository_id, operation.method, operation.path)


def _operation_id(
    repository_id: str,
    spec_path: str,
    operation: OpenAPIOperation,
) -> str:
    operation_slug = slugify_identifier(
        operation.operation_id or f"{operation.method} {operation.path}",
        fallback="operation",
    )
    return f"operation:{repository_id}:{spec_path}:{operation_slug}"


def _schema_id_for_fact(repository_id: str, schema: OpenAPISchema) -> str:
    schema_slug = slugify_identifier(schema.name, fallback="schema")
    return f"schema:{repository_id}:{schema_slug}"


def _service_id(repository_id: str, name: str) -> str:
    name_slug = slugify_identifier(name, fallback="service")
    return f"service:{repository_id}:{name_slug}"


def _line_count(path: str) -> int:
    with Path(path).open(encoding="utf-8") as file:
        return sum(1 for _line in file)


@dataclass(frozen=True)
class _YAMLLine:
    number: int
    indent: int
    text: str


class _YAMLError(ValueError):
    pass


def _parse_yaml_subset(config_text: str) -> dict[str, Any]:
    lines = _yaml_lines(config_text)
    if not lines:
        raise _YAMLError("document is empty")

    value, index = _parse_yaml_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise _YAMLError(f"unexpected content near line {lines[index].number}")
    if not isinstance(value, dict):
        raise _YAMLError("top-level document must be a mapping")
    return value


def _yaml_lines(config_text: str) -> list[_YAMLLine]:
    yaml_lines: list[_YAMLLine] = []
    for number, raw_line in enumerate(config_text.splitlines(), start=1):
        content = raw_line.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if "\t" in raw_line[:indent]:
            raise _YAMLError(f"tabs are not supported near line {number}")
        yaml_lines.append(_YAMLLine(number, indent, content.lstrip(" ")))
    return yaml_lines


def _parse_yaml_block(
    lines: list[_YAMLLine],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    line = lines[index]
    if line.indent != indent:
        raise _YAMLError(f"unexpected indentation near line {line.number}")
    if line.text.startswith("- "):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_mapping(lines, index, indent)


def _parse_yaml_mapping(
    lines: list[_YAMLLine],
    index: int,
    indent: int,
) -> tuple[dict[str, Any], int]:
    mapping: dict[str, Any] = {}
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _YAMLError(f"unexpected indentation near line {line.number}")
        if line.text.startswith("- "):
            break

        key, value_text = _split_yaml_key_value(line)
        if key in mapping:
            raise _YAMLError(f"duplicate key `{key}` near line {line.number}")
        if value_text:
            mapping[key] = _parse_yaml_scalar(line.number, value_text)
            index += 1
            continue
        if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
            mapping[key] = {}
            index += 1
            continue
        mapping[key], index = _parse_yaml_block(
            lines, index + 1, lines[index + 1].indent
        )
    return mapping, index


def _parse_yaml_list(
    lines: list[_YAMLLine],
    index: int,
    indent: int,
) -> tuple[list[Any], int]:
    values: list[Any] = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _YAMLError(f"unexpected indentation near line {line.number}")
        if not line.text.startswith("- "):
            break

        item_text = line.text[2:].strip()
        if not item_text:
            if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
                raise _YAMLError(f"empty list item near line {line.number}")
            value, index = _parse_yaml_block(lines, index + 1, lines[index + 1].indent)
            values.append(value)
            continue
        values.append(_parse_yaml_scalar(line.number, item_text))
        index += 1
    return values, index


def _split_yaml_key_value(line: _YAMLLine) -> tuple[str, str]:
    key, separator, value = line.text.partition(":")
    if not separator:
        raise _YAMLError(f"expected `key: value` near line {line.number}")
    key = key.strip()
    if not key:
        raise _YAMLError(f"mapping key is empty near line {line.number}")
    return key.strip("'\""), value.strip()


def _parse_yaml_scalar(line_number: int, value: str) -> str | bool | list[Any]:
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith(("'", '"')):
        quote = value[0]
        if not value.endswith(quote) or len(value) == 1:
            raise _YAMLError(f"quoted string is not closed near line {line_number}")
        return value[1:-1]
    if value.startswith("[") or value.endswith("]"):
        raise _YAMLError(f"flow-style lists are not supported near line {line_number}")
    return value
