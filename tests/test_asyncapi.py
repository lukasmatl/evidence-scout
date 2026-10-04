from __future__ import annotations

from hashlib import sha256

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.asyncapi import (
    AsyncAPIWarning,
    asyncapi_active_facts,
    parse_asyncapi_file,
)
from code_scout.utils.language_map import language_for_extension


def test_parse_asyncapi_yaml_extracts_channel_operation_and_payload_schema(
    tmp_path,
) -> None:
    spec_file = tmp_path / "asyncapi.yaml"
    spec_file.write_text(
        """
asyncapi: 2.6.0
info:
  title: Orders Events
  version: v1
channels:
  orders.created:
    publish:
      operationId: publishOrderCreated
      summary: Publish order creation
      message:
        payload:
          $ref: '#/components/schemas/OrderCreated'
components:
  schemas:
    OrderCreated:
      type: object
      required:
        - orderId
      properties:
        orderId:
          type: string
""",
        encoding="utf-8",
    )

    metadata, warning = parse_asyncapi_file(
        _discovered_file(spec_file, relative_path="asyncapi.yaml")
    )

    assert warning is None
    assert metadata is not None
    assert metadata.title == "Orders Events"
    assert metadata.version == "v1"
    assert metadata.spec_version == "2.6.0"
    assert metadata.channel_count == 1
    assert metadata.operations[0].action == "publish"
    assert metadata.operations[0].channel == "orders.created"
    assert metadata.operations[0].operation_id == "publishOrderCreated"
    assert metadata.operations[0].payload_dtos == ("OrderCreated",)
    assert metadata.schemas[0].name == "OrderCreated"
    assert metadata.schemas[0].required == ("orderId",)
    assert metadata.schemas[0].properties == ("orderId",)


def test_asyncapi_active_facts_include_event_contract_operation_schema_and_relations(
    tmp_path,
) -> None:
    spec_file = tmp_path / "asyncapi.json"
    spec_file.write_text(
        """
{
  "asyncapi": "2.6.0",
  "info": {"title": "Orders Events", "version": "v1"},
  "channels": {
    "orders.created": {
      "publish": {
        "operationId": "publishOrderCreated",
        "message": {
          "payload": {"$ref": "#/components/schemas/OrderCreated"}
        }
      }
    }
  },
  "components": {
    "schemas": {
      "OrderCreated": {
        "type": "object",
        "required": ["orderId"],
        "properties": {"orderId": {"type": "string"}}
      }
    }
  }
}
""",
        encoding="utf-8",
    )
    discovered_file = _discovered_file(
        spec_file,
        relative_path="contracts/asyncapi.json",
    )

    facts, warnings = asyncapi_active_facts((discovered_file,))
    facts_by_kind = {fact.kind: fact for fact in facts}

    assert warnings == ()
    assert facts_by_kind["contract"].identity == {
        "entity_type": "contract",
        "entity_id": (
            "contract:workspace:contracts/asyncapi.json:event:publish:orders-created"
        ),
    }
    assert facts_by_kind["contract"].properties["kind"] == "event"
    assert facts_by_kind["contract"].properties["path"] == "orders.created"
    assert facts_by_kind["contract"].properties["method"] == "PUBLISH"
    assert facts_by_kind["contract"].properties["response_dtos"] == ("OrderCreated",)
    assert facts_by_kind["operation"].identity == {
        "entity_type": "operation",
        "entity_id": (
            "operation:workspace:contracts/asyncapi.json:publishordercreated"
        ),
    }
    assert facts_by_kind["operation"].properties["channel"] == "orders.created"
    assert facts_by_kind["schema"].identity == {
        "entity_type": "schema",
        "entity_id": "schema:workspace:ordercreated",
    }
    assert facts_by_kind["schema"].properties["properties"] == ("orderId",)
    assert facts_by_kind["declares_contract"].identity["subject"] == {
        "type": "file",
        "id": "file:workspace:contracts/asyncapi.json",
    }
    assert facts_by_kind["produces"].identity["subject"] == {
        "type": "service",
        "id": "service:workspace:orders-events",
    }


def test_asyncapi_active_facts_return_warning_for_invalid_asyncapi_file(
    tmp_path,
) -> None:
    spec_file = tmp_path / "asyncapi.json"
    spec_file.write_text('{"asyncapi":"2.6.0","channels":{}}\n', encoding="utf-8")

    facts, warnings = asyncapi_active_facts(
        (_discovered_file(spec_file, relative_path="asyncapi.json"),),
    )

    assert facts == ()
    assert warnings == (
        AsyncAPIWarning(
            repository_id="workspace",
            path="asyncapi.json",
            message="AsyncAPI file is missing an `info` mapping.",
        ),
    )


def _discovered_file(
    file_path,
    *,
    repository_id: str = "workspace",
    relative_path: str | None = None,
) -> DiscoveredFile:
    content = file_path.read_bytes()
    return DiscoveredFile(
        repository_id=repository_id,
        path=relative_path if relative_path is not None else file_path.name,
        absolute_path=str(file_path),
        kind="metadata",
        extension=file_path.suffix,
        content_hash=sha256(content).hexdigest(),
        modified_at="2026-06-21T10:00:00Z",
        language=language_for_extension(file_path.suffix),
    )
