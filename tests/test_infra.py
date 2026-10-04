from __future__ import annotations

from hashlib import sha256

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.infra import (
    DockerComposeMetadata,
    DockerComposeService,
    DockerfileBaseImage,
    DockerfileExposedPort,
    DockerfileMetadata,
    docker_compose_active_facts,
    infra_active_facts,
    parse_docker_compose_file,
    parse_dockerfile_file,
)


def test_parse_docker_compose_file_extracts_services_images_and_dependencies(
    tmp_path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  api:
    image: ghcr.io/acme/api:latest
    depends_on:
      - db
      - redis
  db:
    image: postgres:16
  redis:
    image: redis:7
""",
        encoding="utf-8",
    )

    metadata = parse_docker_compose_file(_discovered_file(compose_file))

    assert metadata == DockerComposeMetadata(
        services=(
            DockerComposeService(
                name="api",
                image="ghcr.io/acme/api:latest",
                depends_on=("db", "redis"),
            ),
            DockerComposeService(name="db", image="postgres:16"),
            DockerComposeService(name="redis", image="redis:7"),
        ),
    )


def test_docker_compose_active_facts_emit_services_resources_and_relations(
    tmp_path,
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  api:
    image: ghcr.io/acme/api:latest
    depends_on:
      - db
      - broker
  broker:
    image: rabbitmq:3
  db:
    image: postgres:16
""",
        encoding="utf-8",
    )

    facts = docker_compose_active_facts((_discovered_file(compose_file),))

    service_facts = [fact for fact in facts if fact.kind == "service"]
    resource_facts = [fact for fact in facts if fact.kind in {"database", "queue"}]
    relation_facts = [fact for fact in facts if fact.kind == "depends_on"]

    assert [fact.identity for fact in service_facts] == [
        {
            "entity_type": "service",
            "entity_id": "service:workspace:api",
        },
        {
            "entity_type": "service",
            "entity_id": "service:workspace:broker",
        },
        {
            "entity_type": "service",
            "entity_id": "service:workspace:db",
        },
    ]
    assert service_facts[0].properties == {
        "name": "api",
        "repository_id": "workspace",
        "description": "Docker Compose runtime service",
        "aliases": ("api",),
        "runtime": "docker_compose",
        "compose_file": "docker-compose.yml",
        "image": "ghcr.io/acme/api:latest",
        "depends_on": ("broker", "db"),
    }
    assert [fact.identity for fact in resource_facts] == [
        {
            "entity_type": "queue",
            "entity_id": "runtime-resource:workspace:broker",
        },
        {
            "entity_type": "database",
            "entity_id": "runtime-resource:workspace:db",
        },
    ]
    assert [fact.properties["provider"] for fact in resource_facts] == [
        "rabbitmq",
        "postgresql",
    ]
    assert [fact.identity for fact in relation_facts] == [
        {
            "subject": {"type": "service", "id": "service:workspace:api"},
            "relation": "depends_on",
            "object": {"type": "queue", "id": "runtime-resource:workspace:broker"},
        },
        {
            "subject": {"type": "service", "id": "service:workspace:api"},
            "relation": "depends_on",
            "object": {
                "type": "database",
                "id": "runtime-resource:workspace:db",
            },
        },
    ]
    assert relation_facts[0].evidence[0].metadata == {
        "repository_id": "workspace",
        "compose_services": ("api", "broker", "db"),
    }


def test_parse_dockerfile_file_extracts_base_images_and_exposed_ports(tmp_path) -> None:
    dockerfile = tmp_path / "Dockerfile.dev"
    dockerfile.write_text(
        """
FROM --platform=$BUILDPLATFORM node:22-alpine AS build
WORKDIR /app
FROM nginx:1.27
EXPOSE 8080 8443/tcp 9000/udp
""",
        encoding="utf-8",
    )

    metadata = parse_dockerfile_file(
        _discovered_file(dockerfile, relative_path="Dockerfile.dev"),
    )

    assert metadata == DockerfileMetadata(
        base_images=(
            DockerfileBaseImage(
                image="node:22-alpine",
                stage="build",
                line_number=2,
            ),
            DockerfileBaseImage(image="nginx:1.27", line_number=4),
        ),
        exposed_ports=(
            DockerfileExposedPort(port=8080, line_number=5),
            DockerfileExposedPort(port=8443, protocol="tcp", line_number=5),
            DockerfileExposedPort(port=9000, protocol="udp", line_number=5),
        ),
    )


def test_infra_active_facts_emit_dockerfile_images_ports_and_links(tmp_path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        """
FROM python:3.13-slim AS runtime
EXPOSE 8000
""",
        encoding="utf-8",
    )

    facts = infra_active_facts(
        (_discovered_file(dockerfile, relative_path="Dockerfile"),),
    )

    image_fact = next(fact for fact in facts if fact.kind == "runtime_image")
    deploy_target_fact = next(fact for fact in facts if fact.kind == "deploy_target")
    relation_facts = {fact.kind: fact for fact in facts if "subject" in fact.identity}

    assert image_fact.identity == {
        "entity_type": "runtime_image",
        "entity_id": "runtime-image:workspace:python-3-13-slim",
    }
    assert image_fact.properties == {
        "name": "python:3.13-slim",
        "repository_id": "workspace",
        "image": "python:3.13-slim",
        "stage": "runtime",
        "runtime": "dockerfile",
        "dockerfile": "Dockerfile",
        "source_file_id": "file:workspace:Dockerfile",
    }
    assert deploy_target_fact.identity == {
        "entity_type": "deploy_target",
        "entity_id": "deploy-target:workspace:dockerfile:8000-tcp",
    }
    assert deploy_target_fact.properties == {
        "name": "8000/tcp",
        "repository_id": "workspace",
        "port": 8000,
        "protocol": "tcp",
        "runtime": "dockerfile",
        "dockerfile": "Dockerfile",
        "source_file_id": "file:workspace:Dockerfile",
    }
    assert relation_facts["declares_runtime_image"].identity == {
        "subject": {"type": "file", "id": "file:workspace:Dockerfile"},
        "relation": "declares_runtime_image",
        "object": {
            "type": "runtime_image",
            "id": "runtime-image:workspace:python-3-13-slim",
        },
    }
    assert relation_facts["defines_deploy_target"].identity == {
        "subject": {"type": "repository", "id": "workspace"},
        "relation": "defines_deploy_target",
        "object": {
            "type": "deploy_target",
            "id": "deploy-target:workspace:dockerfile:8000-tcp",
        },
    }
    assert relation_facts["exposes_port"].identity == {
        "subject": {
            "type": "runtime_image",
            "id": "runtime-image:workspace:python-3-13-slim",
        },
        "relation": "exposes_port",
        "object": {
            "type": "deploy_target",
            "id": "deploy-target:workspace:dockerfile:8000-tcp",
        },
    }
    assert image_fact.evidence[0].metadata == {
        "repository_id": "workspace",
        "base_images": ("python:3.13-slim",),
        "exposed_ports": ("8000/tcp",),
    }


def _discovered_file(path, *, relative_path: str = "docker-compose.yml"):
    return DiscoveredFile(
        repository_id="workspace",
        path=relative_path,
        absolute_path=str(path),
        kind="metadata",
        extension=path.suffix,
        content_hash=sha256(path.read_bytes()).hexdigest(),
        modified_at="2026-06-21T00:00:00Z",
        language="YAML" if path.suffix in {".yaml", ".yml"} else None,
    )
