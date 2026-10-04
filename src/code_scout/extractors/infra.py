from __future__ import annotations

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
from code_scout.utils.active_facts import merge_active_facts_by_id
from code_scout.utils.identifiers import slugify_identifier

EXTRACTOR_NAME = "infra"
COMPOSE_CANDIDATE_NAMES = frozenset(
    {
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    },
)
RESOURCE_HINTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("database", "postgresql", ("postgres", "postgresql")),
    ("database", "mysql", ("mysql",)),
    ("database", "mariadb", ("mariadb",)),
    ("database", "mongodb", ("mongo", "mongodb")),
    ("database", "redis", ("redis",)),
    ("queue", "rabbitmq", ("rabbitmq",)),
    ("queue", "kafka", ("kafka", "zookeeper")),
    ("queue", "nats", ("nats",)),
    ("queue", "sqs", ("sqs", "localstack")),
    ("dependency", "elasticsearch", ("elasticsearch", "opensearch")),
    ("dependency", "mailhog", ("mailhog", "mailpit")),
    ("dependency", "minio", ("minio",)),
)


@dataclass(frozen=True)
class DockerComposeService:
    name: str
    image: str | None = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class DockerComposeMetadata:
    services: tuple[DockerComposeService, ...] = ()


@dataclass(frozen=True)
class DockerfileBaseImage:
    image: str
    stage: str | None = None
    line_number: int | None = None


@dataclass(frozen=True)
class DockerfileExposedPort:
    port: int
    protocol: str = "tcp"
    line_number: int | None = None


@dataclass(frozen=True)
class DockerfileMetadata:
    base_images: tuple[DockerfileBaseImage, ...] = ()
    exposed_ports: tuple[DockerfileExposedPort, ...] = ()


def parse_docker_compose_file(
    discovered_file: DiscoveredFile,
    *,
    configured_paths: tuple[str, ...] = (),
) -> DockerComposeMetadata | None:
    if not _is_compose_candidate(discovered_file.path, configured_paths):
        return None

    payload = _parse_yaml_subset(
        Path(discovered_file.absolute_path).read_text(encoding="utf-8"),
    )
    services = payload.get("services")
    if not isinstance(services, dict):
        return None

    compose_services: list[DockerComposeService] = []
    for service_name, service_payload in sorted(services.items()):
        if not isinstance(service_name, str) or not service_name:
            continue
        if isinstance(service_payload, dict):
            compose_services.append(
                DockerComposeService(
                    name=service_name,
                    image=_optional_string(service_payload.get("image")),
                    depends_on=_depends_on_names(service_payload.get("depends_on")),
                ),
            )
        else:
            compose_services.append(DockerComposeService(name=service_name))

    return DockerComposeMetadata(services=tuple(compose_services))


def parse_dockerfile_file(discovered_file: DiscoveredFile) -> DockerfileMetadata | None:
    if not _is_dockerfile_candidate(discovered_file.path):
        return None

    base_images: list[DockerfileBaseImage] = []
    exposed_ports: list[DockerfileExposedPort] = []
    source = Path(discovered_file.absolute_path).read_text(encoding="utf-8")

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = _dockerfile_instruction(raw_line)
        if not line:
            continue
        instruction, _, arguments = line.partition(" ")
        instruction = instruction.upper()
        arguments = arguments.strip()
        if instruction == "FROM":
            base_image = _dockerfile_base_image(arguments, line_number)
            if base_image is not None:
                base_images.append(base_image)
        elif instruction == "EXPOSE":
            exposed_ports.extend(_dockerfile_exposed_ports(arguments, line_number))

    if not base_images and not exposed_ports:
        return None
    return DockerfileMetadata(
        base_images=tuple(base_images),
        exposed_ports=tuple(exposed_ports),
    )


def infra_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[ActiveFact, ...]:
    active_facts: list[ActiveFact] = []
    extractor_metadata = ExtractorMetadata(name=EXTRACTOR_NAME)

    for discovered_file in discovered_files:
        dockerfile_metadata = parse_dockerfile_file(discovered_file)
        if dockerfile_metadata is not None:
            active_facts.extend(
                _dockerfile_active_facts(
                    discovered_file,
                    dockerfile_metadata,
                    extractor_metadata,
                ),
            )

        metadata = parse_docker_compose_file(
            discovered_file,
            configured_paths=configured_paths,
        )
        if metadata is None:
            continue

        evidence = _fact_evidence(discovered_file, metadata)
        resource_references = {
            service.name: _resource_reference(discovered_file.repository_id, service)
            for service in metadata.services
        }

        for service in metadata.services:
            service_reference = FactReference(
                type="service",
                id=_runtime_service_id(discovered_file.repository_id, service.name),
            )
            active_facts.append(
                entity_fact_to_active_fact(
                    EntityFact(
                        entity_type="service",
                        entity_id=service_reference.id,
                        properties={
                            "name": service.name,
                            "repository_id": discovered_file.repository_id,
                            "description": "Docker Compose runtime service",
                            "aliases": (service.name,),
                            "runtime": "docker_compose",
                            "compose_file": discovered_file.path,
                            "image": service.image,
                            "depends_on": service.depends_on,
                        },
                        evidence=(evidence,),
                        confidence=0.8,
                        extractor_metadata=extractor_metadata,
                    ),
                ),
            )

            resource_reference = resource_references[service.name]
            if resource_reference is not None:
                category, provider = _resource_category_and_provider(service)
                active_facts.append(
                    entity_fact_to_active_fact(
                        EntityFact(
                            entity_type=category,
                            entity_id=resource_reference.id,
                            properties={
                                "name": service.name,
                                "repository_id": discovered_file.repository_id,
                                "provider": provider,
                                "image": service.image,
                                "runtime": "docker_compose",
                                "compose_file": discovered_file.path,
                            },
                            evidence=(evidence,),
                            confidence=0.8,
                            extractor_metadata=extractor_metadata,
                        ),
                    ),
                )

            for dependency_name in service.depends_on:
                dependency_reference = resource_references.get(dependency_name)
                if dependency_reference is None:
                    dependency_reference = FactReference(
                        type="dependency",
                        id=_dependency_id(
                            discovered_file.repository_id,
                            dependency_name,
                        ),
                    )
                    active_facts.append(
                        entity_fact_to_active_fact(
                            EntityFact(
                                entity_type="dependency",
                                entity_id=dependency_reference.id,
                                properties={
                                    "name": dependency_name,
                                    "repository_id": discovered_file.repository_id,
                                    "runtime": "docker_compose",
                                    "compose_file": discovered_file.path,
                                },
                                evidence=(evidence,),
                                confidence=0.65,
                                extractor_metadata=extractor_metadata,
                            ),
                        ),
                    )

                active_facts.append(
                    relation_fact_to_active_fact(
                        RelationFact(
                            subject=service_reference,
                            relation="depends_on",
                            object=dependency_reference,
                            properties={
                                "repository_id": discovered_file.repository_id,
                                "runtime": "docker_compose",
                                "compose_file": discovered_file.path,
                                "dependency_name": dependency_name,
                            },
                            evidence=(evidence,),
                            confidence=0.8,
                            extractor_metadata=extractor_metadata,
                        ),
                    ),
                )

    return merge_active_facts_by_id(*active_facts)


def docker_compose_active_facts(
    discovered_files: tuple[DiscoveredFile, ...],
    *,
    configured_paths: tuple[str, ...] = (),
) -> tuple[ActiveFact, ...]:
    return infra_active_facts(discovered_files, configured_paths=configured_paths)


def _dockerfile_active_facts(
    discovered_file: DiscoveredFile,
    metadata: DockerfileMetadata,
    extractor_metadata: ExtractorMetadata,
) -> tuple[ActiveFact, ...]:
    active_facts: list[ActiveFact] = []
    evidence = _dockerfile_evidence(discovered_file, metadata)
    file_reference = FactReference(
        type="file",
        id=_file_id(discovered_file.repository_id, discovered_file.path),
    )
    repository_reference = FactReference(
        type="repository",
        id=discovered_file.repository_id,
    )

    image_references: list[FactReference] = []
    for base_image in metadata.base_images:
        image_reference = FactReference(
            type="runtime_image",
            id=_runtime_image_id(discovered_file.repository_id, base_image.image),
        )
        image_references.append(image_reference)
        active_facts.append(
            entity_fact_to_active_fact(
                EntityFact(
                    entity_type="runtime_image",
                    entity_id=image_reference.id,
                    properties={
                        "name": base_image.image,
                        "repository_id": discovered_file.repository_id,
                        "image": base_image.image,
                        "stage": base_image.stage,
                        "runtime": "dockerfile",
                        "dockerfile": discovered_file.path,
                        "source_file_id": file_reference.id,
                    },
                    evidence=(evidence,),
                    confidence=0.8,
                    extractor_metadata=extractor_metadata,
                ),
            ),
        )
        active_facts.append(
            relation_fact_to_active_fact(
                RelationFact(
                    subject=file_reference,
                    relation="declares_runtime_image",
                    object=image_reference,
                    properties={
                        "repository_id": discovered_file.repository_id,
                        "runtime": "dockerfile",
                        "dockerfile": discovered_file.path,
                        "line_number": base_image.line_number,
                    },
                    evidence=(evidence,),
                    confidence=0.8,
                    extractor_metadata=extractor_metadata,
                ),
            ),
        )

    for exposed_port in metadata.exposed_ports:
        deploy_target_reference = FactReference(
            type="deploy_target",
            id=_deploy_target_id(
                discovered_file.repository_id,
                discovered_file.path,
                exposed_port,
            ),
        )
        active_facts.append(
            entity_fact_to_active_fact(
                EntityFact(
                    entity_type="deploy_target",
                    entity_id=deploy_target_reference.id,
                    properties={
                        "name": f"{exposed_port.port}/{exposed_port.protocol}",
                        "repository_id": discovered_file.repository_id,
                        "port": exposed_port.port,
                        "protocol": exposed_port.protocol,
                        "runtime": "dockerfile",
                        "dockerfile": discovered_file.path,
                        "source_file_id": file_reference.id,
                    },
                    evidence=(evidence,),
                    confidence=0.75,
                    extractor_metadata=extractor_metadata,
                ),
            ),
        )
        active_facts.append(
            relation_fact_to_active_fact(
                RelationFact(
                    subject=file_reference,
                    relation="declares_deploy_target",
                    object=deploy_target_reference,
                    properties={
                        "repository_id": discovered_file.repository_id,
                        "runtime": "dockerfile",
                        "dockerfile": discovered_file.path,
                        "line_number": exposed_port.line_number,
                    },
                    evidence=(evidence,),
                    confidence=0.75,
                    extractor_metadata=extractor_metadata,
                ),
            ),
        )
        active_facts.append(
            relation_fact_to_active_fact(
                RelationFact(
                    subject=repository_reference,
                    relation="defines_deploy_target",
                    object=deploy_target_reference,
                    properties={
                        "repository_id": discovered_file.repository_id,
                        "runtime": "dockerfile",
                        "dockerfile": discovered_file.path,
                    },
                    evidence=(evidence,),
                    confidence=0.75,
                    extractor_metadata=extractor_metadata,
                ),
            ),
        )
        for image_reference in image_references:
            active_facts.append(
                relation_fact_to_active_fact(
                    RelationFact(
                        subject=image_reference,
                        relation="exposes_port",
                        object=deploy_target_reference,
                        properties={
                            "repository_id": discovered_file.repository_id,
                            "runtime": "dockerfile",
                            "dockerfile": discovered_file.path,
                        },
                        evidence=(evidence,),
                        confidence=0.75,
                        extractor_metadata=extractor_metadata,
                    ),
                ),
            )

    return tuple(active_facts)


def _depends_on_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(sorted(str(item) for item in value if isinstance(item, str)))
    if isinstance(value, dict):
        return tuple(sorted(str(name) for name in value if isinstance(name, str)))
    return ()


def _resource_reference(
    repository_id: str,
    service: DockerComposeService,
) -> FactReference | None:
    category, _provider = _resource_category_and_provider(service)
    if category == "":
        return None
    return FactReference(type=category, id=_resource_id(repository_id, service.name))


def _resource_category_and_provider(service: DockerComposeService) -> tuple[str, str]:
    haystack = " ".join(value for value in (service.name, service.image) if value)
    normalized = slugify_identifier(haystack, fallback="service")
    for category, provider, hints in RESOURCE_HINTS:
        if any(hint in normalized for hint in hints):
            return category, provider
    return "", ""


def _runtime_service_id(repository_id: str, service_name: str) -> str:
    service_slug = slugify_identifier(service_name, fallback="service")
    return f"service:{repository_id}:{service_slug}"


def _resource_id(repository_id: str, resource_name: str) -> str:
    return (
        f"runtime-resource:{repository_id}:"
        f"{slugify_identifier(resource_name, fallback='resource')}"
    )


def _dependency_id(repository_id: str, dependency_name: str) -> str:
    return (
        f"runtime-dependency:{repository_id}:"
        f"{slugify_identifier(dependency_name, fallback='dependency')}"
    )


def _runtime_image_id(repository_id: str, image: str) -> str:
    return (
        f"runtime-image:{repository_id}:{slugify_identifier(image, fallback='image')}"
    )


def _deploy_target_id(
    repository_id: str,
    dockerfile_path: str,
    exposed_port: DockerfileExposedPort,
) -> str:
    path_slug = slugify_identifier(dockerfile_path, fallback="dockerfile")
    return (
        f"deploy-target:{repository_id}:{path_slug}:"
        f"{exposed_port.port}-{exposed_port.protocol}"
    )


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _fact_evidence(
    discovered_file: DiscoveredFile,
    metadata: DockerComposeMetadata,
) -> FactEvidence:
    return FactEvidence(
        file_path=discovered_file.path,
        extractor_name=EXTRACTOR_NAME,
        confidence=0.8,
        start_line=1,
        end_line=_line_count(discovered_file.absolute_path),
        metadata={
            "repository_id": discovered_file.repository_id,
            "compose_services": tuple(service.name for service in metadata.services),
        },
    )


def _dockerfile_evidence(
    discovered_file: DiscoveredFile,
    metadata: DockerfileMetadata,
) -> FactEvidence:
    return FactEvidence(
        file_path=discovered_file.path,
        extractor_name=EXTRACTOR_NAME,
        confidence=0.8,
        start_line=1,
        end_line=_line_count(discovered_file.absolute_path),
        metadata={
            "repository_id": discovered_file.repository_id,
            "base_images": tuple(image.image for image in metadata.base_images),
            "exposed_ports": tuple(
                f"{port.port}/{port.protocol}" for port in metadata.exposed_ports
            ),
        },
    )


def _is_compose_candidate(path: str, configured_paths: tuple[str, ...]) -> bool:
    normalized_path = _normalize_path(path)
    if Path(normalized_path).name.lower() in COMPOSE_CANDIDATE_NAMES:
        return True
    for configured_path in configured_paths:
        normalized_candidate = _normalize_path(configured_path)
        if normalized_path == normalized_candidate or normalized_path.startswith(
            f"{normalized_candidate}/",
        ):
            return normalized_path.endswith((".yaml", ".yml"))
    return False


def _is_dockerfile_candidate(path: str) -> bool:
    name = Path(_normalize_path(path).lower()).name
    return name == "dockerfile" or name.startswith("dockerfile.")


def _normalize_path(path: str) -> str:
    return Path(path).as_posix().removeprefix("./").strip("/")


def _dockerfile_instruction(raw_line: str) -> str:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return ""
    return line.split("#", 1)[0].strip()


def _dockerfile_base_image(
    arguments: str,
    line_number: int,
) -> DockerfileBaseImage | None:
    parts = tuple(part for part in arguments.split() if part)
    if not parts:
        return None

    image: str | None = None
    stage: str | None = None
    index = 0
    while index < len(parts):
        part = parts[index]
        if part.startswith("--"):
            index += 1
            continue
        image = part
        index += 1
        break

    if image is None:
        return None
    if index + 1 < len(parts) and parts[index].upper() == "AS":
        stage = parts[index + 1]
    return DockerfileBaseImage(image=image, stage=stage, line_number=line_number)


def _dockerfile_exposed_ports(
    arguments: str,
    line_number: int,
) -> tuple[DockerfileExposedPort, ...]:
    ports: list[DockerfileExposedPort] = []
    for raw_port in arguments.split():
        port, _, protocol = raw_port.partition("/")
        if not port.isdigit():
            continue
        normalized_protocol = protocol.lower() if protocol else "tcp"
        if normalized_protocol not in {"tcp", "udp"}:
            normalized_protocol = "tcp"
        ports.append(
            DockerfileExposedPort(
                port=int(port),
                protocol=normalized_protocol,
                line_number=line_number,
            ),
        )
    return tuple(ports)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


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
        if value_text:
            mapping[key] = _parse_yaml_scalar(value_text)
            index += 1
            continue

        if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
            mapping[key] = {}
            index += 1
            continue

        mapping[key], index = _parse_yaml_block(
            lines,
            index + 1,
            lines[index + 1].indent,
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
        values.append(_parse_yaml_scalar(item_text))
        index += 1

    return values, index


def _split_yaml_key_value(line: _YAMLLine) -> tuple[str, str]:
    key, separator, value = line.text.partition(":")
    if not separator:
        raise _YAMLError(f"expected `key: value` near line {line.number}")
    key = key.strip()
    if not key:
        raise _YAMLError(f"mapping key is empty near line {line.number}")
    return key, value.strip()


def _parse_yaml_scalar(value: str) -> str | bool | tuple[str, ...]:
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value.startswith(("'", '"')):
        quote = value[0]
        if value.endswith(quote) and len(value) > 1:
            return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        return tuple(
            item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()
        )
    return value
