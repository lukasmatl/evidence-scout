from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from code_scout.models.graph import Contract
from code_scout.utils.identifiers import slugify_identifier

EXTRACTOR_NAME = "asyncapi"
EXACT_PARSED_CONFIDENCE = 1.0
ASYNCAPI_CANDIDATE_NAMES = frozenset(
    {
        "asyncapi.json",
        "asyncapi.yaml",
        "asyncapi.yml",
    },
)
ASYNCAPI_CHANNEL_OPERATIONS = frozenset({"publish", "subscribe"})


@dataclass(frozen=True)
class AsyncAPIOperation:
    action: str
    channel: str
    operation_id: str | None = None
    summary: str | None = None
    payload_dtos: tuple[str, ...] = ()


@dataclass(frozen=True)
class AsyncAPISchema:
    name: str
    schema_type: str | None = None
    required: tuple[str, ...] = ()
    properties: tuple[str, ...] = ()


@dataclass(frozen=True)
class AsyncAPIMetadata:
    title: str
    version: str
    spec_version: str
    channel_count: int
    operations: tuple[AsyncAPIOperation, ...] = ()
    schemas: tuple[AsyncAPISchema, ...] = ()


@dataclass(frozen=True)
class AsyncAPIWarning:
    repository_id: str
    path: str
    message: str


def parse_asyncapi_file(
    discovered_file: DiscoveredFile,
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[AsyncAPIMetadata | None, AsyncAPIWarning | None]:
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
            return None, _warning(
                discovered_file, f"AsyncAPI file is invalid: {error}."
            )
        return None, None

    metadata, reason = _metadata_from_payload(payload)
    if metadata is not None:
        return metadata, None
    if is_named_candidate or is_configured_candidate or _looks_like_asyncapi(payload):
        return None, _warning(discovered_file, reason)
    return None, None


def asyncapi_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[tuple[ActiveFact, ...], tuple[AsyncAPIWarning, ...]]:
    active_facts: list[ActiveFact] = []
    warnings: list[AsyncAPIWarning] = []

    for discovered_file in discovered_files:
        metadata, warning = parse_asyncapi_file(
            discovered_file,
            configured_paths=configured_paths,
        )
        if warning is not None:
            warnings.append(warning)
        if metadata is None:
            continue
        active_facts.extend(_active_facts_for_metadata(discovered_file, metadata))

    return tuple(active_facts), tuple(warnings)


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
) -> tuple[AsyncAPIMetadata | None, str]:
    spec_version = payload.get("asyncapi")
    if not isinstance(spec_version, str) or not spec_version:
        return None, "AsyncAPI file is missing an `asyncapi` version."

    info = payload.get("info")
    if not isinstance(info, dict):
        return None, "AsyncAPI file is missing an `info` mapping."

    title = info.get("title")
    if not isinstance(title, str) or not title:
        return None, "AsyncAPI file is missing a non-empty `info.title` string."

    version = info.get("version")
    if not isinstance(version, str) or not version:
        return None, "AsyncAPI file is missing a non-empty `info.version` string."

    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return None, "AsyncAPI file is missing a `channels` mapping."

    operations = _operations_from_channels(channels, payload)
    schemas = _schemas_from_payload(payload, operations)
    return (
        AsyncAPIMetadata(
            title=title,
            version=version,
            spec_version=spec_version,
            channel_count=len(channels),
            operations=operations,
            schemas=schemas,
        ),
        "",
    )


def _operations_from_channels(
    channels: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[AsyncAPIOperation, ...]:
    operations: list[AsyncAPIOperation] = []
    for channel, channel_payload in channels.items():
        if not isinstance(channel, str) or not isinstance(channel_payload, dict):
            continue
        for action in sorted(ASYNCAPI_CHANNEL_OPERATIONS):
            operation_payload = channel_payload.get(action)
            if not isinstance(operation_payload, dict):
                continue
            operation_id = operation_payload.get("operationId")
            summary = operation_payload.get("summary")
            operations.append(
                AsyncAPIOperation(
                    action=action,
                    channel=channel,
                    operation_id=operation_id
                    if isinstance(operation_id, str) and operation_id
                    else None,
                    summary=summary if isinstance(summary, str) and summary else None,
                    payload_dtos=_message_payload_dtos(operation_payload, payload),
                ),
            )
    return tuple(operations)


def _schemas_from_payload(
    payload: dict[str, Any],
    operations: tuple[AsyncAPIOperation, ...],
) -> tuple[AsyncAPISchema, ...]:
    parsed: list[AsyncAPISchema] = []
    components = payload.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            parsed.extend(
                _schema_from_payload(name, schema_payload)
                for name, schema_payload in schemas.items()
                if isinstance(name, str) and name and isinstance(schema_payload, dict)
            )

    component_schema_names = {schema.name for schema in parsed}
    parsed.extend(
        schema
        for schema in _inline_message_schemas(payload)
        if schema.name not in component_schema_names
    )
    for operation in operations:
        for payload_dto in operation.payload_dtos:
            if payload_dto in component_schema_names or not payload_dto.startswith(
                "inline:"
            ):
                continue
            parsed.append(
                AsyncAPISchema(
                    name=payload_dto,
                    schema_type="object",
                ),
            )
    return tuple(parsed)


def _inline_message_schemas(payload: dict[str, Any]) -> tuple[AsyncAPISchema, ...]:
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return ()

    schemas: list[AsyncAPISchema] = []
    for channel_payload in channels.values():
        if not isinstance(channel_payload, dict):
            continue
        for action in sorted(ASYNCAPI_CHANNEL_OPERATIONS):
            operation_payload = channel_payload.get(action)
            if not isinstance(operation_payload, dict):
                continue
            for payload_schema in _message_payloads(
                operation_payload.get("message"),
                payload,
            ):
                if "$ref" in payload_schema:
                    continue
                name = _schema_id(
                    payload_schema,
                    "message",
                    operation_payload.get("operationId"),
                )
                if name is not None:
                    schemas.append(_schema_from_payload(name, payload_schema))
    return tuple(schemas)


def _schema_from_payload(name: str, schema_payload: dict[str, Any]) -> AsyncAPISchema:
    schema_type = schema_payload.get("type")
    properties = schema_payload.get("properties", {})
    return AsyncAPISchema(
        name=name,
        schema_type=schema_type if isinstance(schema_type, str) else None,
        required=_string_tuple(schema_payload.get("required", ())),
        properties=tuple(
            property_name
            for property_name in properties
            if isinstance(property_name, str)
        )
        if isinstance(properties, dict)
        else (),
    )


def _message_payload_dtos(
    operation_payload: dict[str, Any],
    root_payload: dict[str, Any],
) -> tuple[str, ...]:
    message = operation_payload.get("message")
    message_payloads = _message_payloads(message, root_payload)
    return _dedupe(
        [
            dto
            for index, payload in enumerate(message_payloads, start=1)
            for dto in (
                _schema_id(
                    payload,
                    f"message:{index}",
                    operation_payload.get("operationId"),
                ),
            )
            if dto is not None
        ],
    )


def _message_payloads(
    message: Any, root_payload: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    if isinstance(message, dict):
        one_of = message.get("oneOf")
        if isinstance(one_of, list):
            return tuple(
                payload
                for item in one_of
                for payload in _message_payloads(item, root_payload)
            )
        resolved_message = _resolve_ref(message, root_payload)
        if resolved_message is not None and resolved_message is not message:
            return _message_payloads(resolved_message, root_payload)
        payload = message.get("payload")
        if isinstance(payload, dict):
            return (payload,)
    return ()


def _resolve_ref(
    value: dict[str, Any], root_payload: dict[str, Any]
) -> dict[str, Any] | None:
    ref = value.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    current: Any = root_payload
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, dict) else None


def _schema_id(
    schema: dict[str, Any],
    inline_prefix: str,
    operation_id: Any,
) -> str | None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref:
        return ref.rsplit("/", 1)[-1]

    title = schema.get("title")
    if isinstance(title, str) and title:
        return title

    if schema:
        prefix = (
            operation_id
            if isinstance(operation_id, str) and operation_id
            else inline_prefix
        )
        prefix_slug = slugify_identifier(prefix, fallback="message")
        return f"inline:{prefix_slug}"
    return None


def _active_facts_for_metadata(
    discovered_file: DiscoveredFile,
    metadata: AsyncAPIMetadata,
) -> tuple[ActiveFact, ...]:
    evidence = _metadata_fact_evidence(discovered_file, metadata)
    extractor_metadata = ExtractorMetadata(name=EXTRACTOR_NAME)
    contracts = _operation_contracts(discovered_file, metadata)
    contract_facts = tuple(
        entity_fact_to_active_fact(
            EntityFact(
                entity_type="contract",
                entity_id=contract.id,
                properties={
                    "name": contract.name,
                    "kind": contract.kind,
                    "path": contract.path,
                    "version": contract.version,
                    "method": contract.method,
                    "producer_hint": contract.producer_hint,
                    "operation_id": contract.operation_id,
                    "request_dtos": contract.request_dtos,
                    "response_dtos": contract.response_dtos,
                    "channel": operation.channel,
                    "action": operation.action,
                },
                evidence=(evidence,),
                confidence=EXACT_PARSED_CONFIDENCE,
                extractor_metadata=extractor_metadata,
            ),
        )
        for operation, contract in zip(metadata.operations, contracts, strict=True)
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
                    "contract_id": contract.id,
                    "channel": operation.channel,
                    "action": operation.action,
                    "operation_id": operation.operation_id,
                    "summary": operation.summary,
                    "payload_dtos": operation.payload_dtos,
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
    relation_facts = tuple(
        relation_fact_to_active_fact(
            RelationFact(
                subject=FactReference(
                    type="file",
                    id=_file_id(discovered_file.repository_id, discovered_file.path),
                ),
                relation="declares_contract",
                object=FactReference(type="contract", id=contract.id),
                properties={
                    "repository_id": discovered_file.repository_id,
                    "spec_path": discovered_file.path,
                    "channel": operation.channel,
                    "action": operation.action,
                },
                evidence=(evidence,),
                confidence=EXACT_PARSED_CONFIDENCE,
                extractor_metadata=extractor_metadata,
            ),
        )
        for operation, contract in zip(metadata.operations, contracts, strict=True)
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
                    object=FactReference(type="contract", id=contract.id),
                    properties={
                        "repository_id": discovered_file.repository_id,
                        "spec_path": discovered_file.path,
                        "service_name": metadata.title,
                        "channel": operation.channel,
                        "action": operation.action,
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
        *contract_facts,
        *operation_facts,
        *schema_facts,
        *relation_facts,
        *service_contract_relation_facts,
    )


def _operation_contracts(
    discovered_file: DiscoveredFile,
    metadata: AsyncAPIMetadata,
) -> tuple[Contract, ...]:
    return tuple(
        Contract(
            id=_operation_contract_id(
                discovered_file.repository_id,
                discovered_file.path,
                operation,
            ),
            name=operation.operation_id or f"{operation.action} {operation.channel}",
            kind="event",
            path=operation.channel,
            version=metadata.version,
            method=operation.action.upper(),
            producer_hint=metadata.title,
            operation_id=operation.operation_id,
            response_dtos=operation.payload_dtos,
        )
        for operation in metadata.operations
    )


def _metadata_fact_evidence(
    discovered_file: DiscoveredFile,
    metadata: AsyncAPIMetadata,
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
            "channel_count": metadata.channel_count,
        },
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return tuple(deduped)


def _looks_like_asyncapi(payload: dict[str, Any]) -> bool:
    return "asyncapi" in payload


def _is_named_candidate(path: str) -> bool:
    return Path(path).name.lower() in ASYNCAPI_CANDIDATE_NAMES


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


def _warning(discovered_file: DiscoveredFile, message: str) -> AsyncAPIWarning:
    return AsyncAPIWarning(
        repository_id=discovered_file.repository_id,
        path=discovered_file.path,
        message=message,
    )


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _operation_contract_id(
    repository_id: str,
    spec_path: str,
    operation: AsyncAPIOperation,
) -> str:
    channel_slug = slugify_identifier(operation.channel, fallback="channel")
    action = operation.action.lower()
    return f"contract:{repository_id}:{spec_path}:event:{action}:{channel_slug}"


def _operation_id(
    repository_id: str,
    spec_path: str,
    operation: AsyncAPIOperation,
) -> str:
    operation_slug = slugify_identifier(
        operation.operation_id or f"{operation.action} {operation.channel}",
        fallback="operation",
    )
    return f"operation:{repository_id}:{spec_path}:{operation_slug}"


def _schema_id_for_fact(repository_id: str, schema: AsyncAPISchema) -> str:
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
        content = _strip_yaml_comment(raw_line).rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip(" "))
        if "\t" in raw_line[:indent]:
            raise _YAMLError(f"tabs are not supported near line {number}")
        yaml_lines.append(_YAMLLine(number, indent, content.lstrip(" ")))
    return yaml_lines


def _strip_yaml_comment(raw_line: str) -> str:
    quote: str | None = None
    previous = ""
    for index, character in enumerate(raw_line):
        if quote is not None:
            if character == quote and previous != "\\":
                quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "#" and (index == 0 or raw_line[index - 1].isspace()):
            return raw_line[:index]
        previous = character
    return raw_line


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
