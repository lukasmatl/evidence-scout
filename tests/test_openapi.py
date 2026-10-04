from __future__ import annotations

from hashlib import sha256

from code_scout.discovery import DiscoveredFile
from code_scout.extractors.openapi import (
    OpenAPIWarning,
    openapi_active_facts,
    openapi_concept_links,
    openapi_contract_edges,
    openapi_contracts,
    parse_openapi_file,
)
from code_scout.utils.language_map import language_for_extension


def test_parse_openapi_json_extracts_contract_metadata(tmp_path) -> None:
    spec_file = tmp_path / "api.json"
    spec_file.write_text(
        """
        {
          "openapi": "3.1.0",
          "info": {
            "title": "Payments API",
            "version": "2026-06-20"
          },
          "paths": {
            "/payments": {
              "post": {
                "operationId": "createPayment",
                "tags": ["Payments", "Checkout"]
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )

    metadata, warning = parse_openapi_file(_discovered_file(spec_file))

    assert warning is None
    assert metadata is not None
    assert metadata.title == "Payments API"
    assert metadata.version == "2026-06-20"
    assert metadata.spec_version == "3.1.0"
    assert metadata.path_count == 1
    assert metadata.operations[0].method == "POST"
    assert metadata.operations[0].path == "/payments"
    assert metadata.operations[0].operation_id == "createPayment"
    assert metadata.operations[0].tags == ("Payments", "Checkout")
    assert metadata.operations[0].request_dtos == ()
    assert metadata.operations[0].response_dtos == ()


def test_parse_openapi_yaml_extracts_contract_metadata(tmp_path) -> None:
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
      tags:
        - Orders
      summary: List orders
""",
        encoding="utf-8",
    )

    metadata, warning = parse_openapi_file(
        _discovered_file(spec_file, relative_path="openapi.yaml")
    )

    assert warning is None
    assert metadata is not None
    assert metadata.title == "Orders API"
    assert metadata.version == "v1"
    assert metadata.path_count == 1
    assert metadata.operations[0].method == "GET"
    assert metadata.operations[0].path == "/orders"
    assert metadata.operations[0].operation_id == "listOrders"
    assert metadata.operations[0].tags == ("Orders",)
    assert metadata.operations[0].request_dtos == ()
    assert metadata.operations[0].response_dtos == ()


def test_parse_openapi_extracts_referenced_request_and_response_schemas(
    tmp_path,
) -> None:
    spec_file = tmp_path / "api.json"
    spec_file.write_text(
        """
        {
          "openapi": "3.1.0",
          "info": {"title": "Payments API", "version": "v1"},
          "paths": {
            "/payments": {
              "post": {
                "operationId": "createPayment",
                "requestBody": {
                  "content": {
                    "application/json": {
                      "schema": {"$ref": "#/components/schemas/CreatePaymentRequest"}
                    }
                  }
                },
                "responses": {
                  "201": {
                    "description": "Created",
                    "content": {
                      "application/json": {
                        "schema": {"$ref": "#/components/schemas/PaymentResponse"}
                      }
                    }
                  },
                  "400": {
                    "description": "Bad request",
                    "content": {
                      "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )

    metadata, warning = parse_openapi_file(_discovered_file(spec_file))

    assert warning is None
    assert metadata is not None
    assert metadata.operations[0].request_dtos == ("CreatePaymentRequest",)
    assert metadata.operations[0].response_dtos == ("PaymentResponse",)


def test_parse_openapi_extracts_inline_request_and_response_schema_ids(
    tmp_path,
) -> None:
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    post:
      operationId: createOrder
      requestBody:
        content:
          application/json:
            schema:
              type: object
              properties:
                itemId:
                  type: string
      responses:
        200:
          description: Created
          content:
            application/json:
              schema:
                title: OrderDto
                type: object
        204:
          description: Empty success response
        500:
          description: Server error
          content:
            application/json:
              schema:
                title: ErrorDto
                type: object
""",
        encoding="utf-8",
    )

    metadata, warning = parse_openapi_file(
        _discovered_file(spec_file, relative_path="openapi.yaml")
    )

    assert warning is None
    assert metadata is not None
    assert metadata.operations[0].request_dtos == ("inline:request:application-json",)
    assert metadata.operations[0].response_dtos == ("OrderDto",)


def test_parse_openapi_rejects_unrelated_json_without_warning(tmp_path) -> None:
    data_file = tmp_path / "settings.json"
    data_file.write_text('{"name":"not an api","paths":{}}\n', encoding="utf-8")

    metadata, warning = parse_openapi_file(
        _discovered_file(data_file, relative_path="config/settings.json")
    )

    assert metadata is None
    assert warning is None


def test_named_invalid_openapi_file_reports_warning(tmp_path) -> None:
    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(
        '{"openapi":"3.0.0","info":{"title":"Broken"}}\n', encoding="utf-8"
    )

    metadata, warning = parse_openapi_file(
        _discovered_file(spec_file, relative_path="openapi.json")
    )

    assert metadata is None
    assert warning == OpenAPIWarning(
        repository_id="workspace",
        path="openapi.json",
        message="OpenAPI file is missing a non-empty `info.version` string.",
    )


def test_openapi_contracts_and_edges_use_stable_operation_ids(tmp_path) -> None:
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
      tags:
        - Orders
      summary: List orders
""",
        encoding="utf-8",
    )
    discovered_file = _discovered_file(
        spec_file,
        relative_path="contracts/openapi.yaml",
    )

    contracts, warnings = openapi_contracts((discovered_file,))
    edges = openapi_contract_edges((discovered_file,))

    assert warnings == ()
    assert contracts[0].id == "contract:workspace:http:get:orders"
    assert contracts[0].name == "listOrders"
    assert contracts[0].kind == "http"
    assert contracts[0].path == "/orders"
    assert contracts[0].version == "v1"
    assert contracts[0].method == "GET"
    assert contracts[0].producer_hint == "Orders API"
    assert contracts[0].operation_id == "listOrders"
    assert contracts[0].request_dtos == ()
    assert contracts[0].response_dtos == ()
    assert contracts[0].evidence[0].confidence == 1.0
    assert edges[0].source_type == "file"
    assert edges[0].source_id == "file:workspace:contracts/openapi.yaml"
    assert edges[0].target_type == "contract"
    assert edges[0].target_id == "contract:workspace:http:get:orders"
    assert edges[0].relation_type == "declares_contract"
    assert edges[0].evidence[0].metadata == {
        "repository_id": "workspace",
        "title": "Orders API",
        "version": "v1",
        "spec_version": "3.0.3",
        "path_count": 1,
        "method": "GET",
        "path": "/orders",
        "operation_id": "listOrders",
        "tags": ["Orders"],
        "request_dtos": [],
        "response_dtos": [],
    }


def test_openapi_concept_links_use_tags_and_operation_ids(tmp_path) -> None:
    spec_file = tmp_path / "openapi.yaml"
    spec_file.write_text(
        """
openapi: 3.0.3
info:
  title: Orders API
  version: v1
paths:
  /orders:
    get:
      operationId: listOrders
      tags:
        - Orders
""",
        encoding="utf-8",
    )
    discovered_file = _discovered_file(
        spec_file,
        relative_path="contracts/openapi.yaml",
    )

    links = openapi_concept_links((discovered_file,))

    assert [link.concept.id for link in links] == [
        "concept:workspace:orders",
        "concept:workspace:listorders",
    ]
    assert [link.concept.name for link in links] == ["Orders", "listOrders"]
    assert [link.edge.target_id for link in links] == [
        "contract:workspace:http:get:orders",
        "contract:workspace:http:get:orders",
    ]
    assert [link.edge.confidence for link in links] == [0.85, 0.85]
    assert [link.edge.evidence[0].confidence for link in links] == [0.85, 0.85]
    assert [link.edge.evidence[0].metadata["source"] for link in links] == [
        "tag",
        "operation_id",
    ]


def test_openapi_active_facts_include_endpoint_operation_schema_and_relation(
    tmp_path,
) -> None:
    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(
        """
{
  "openapi": "3.0.3",
  "info": {"title": "Orders API", "version": "v1"},
  "paths": {
    "/orders": {
      "get": {
        "operationId": "listOrders",
        "tags": ["Orders"],
        "responses": {
          "200": {
            "description": "OK",
            "content": {
              "application/json": {
                "schema": {"$ref": "#/components/schemas/OrderListResponse"}
              }
            }
          }
        }
      }
    }
  },
  "components": {
    "schemas": {
      "OrderListResponse": {
        "type": "object",
        "required": ["orders"],
        "properties": {
          "orders": {"type": "array"}
        }
      }
    }
  }
}
""",
        encoding="utf-8",
    )
    discovered_file = _discovered_file(
        spec_file,
        relative_path="contracts/openapi.json",
    )

    facts, warnings = openapi_active_facts((discovered_file,))
    facts_by_kind = {fact.kind: fact for fact in facts}

    assert warnings == ()
    assert facts_by_kind["contract"].identity == {
        "entity_type": "contract",
        "entity_id": "contract:workspace:http:get:orders",
    }
    assert facts_by_kind["contract"].properties["operation_id"] == "listOrders"
    assert facts_by_kind["contract"].confidence == 1.0
    assert facts_by_kind["operation"].identity == {
        "entity_type": "operation",
        "entity_id": "operation:workspace:contracts/openapi.json:listorders",
    }
    assert facts_by_kind["operation"].properties["endpoint_id"] == (
        "contract:workspace:http:get:orders"
    )
    assert facts_by_kind["schema"].identity == {
        "entity_type": "schema",
        "entity_id": "schema:workspace:orderlistresponse",
    }
    assert facts_by_kind["schema"].properties == {
        "repository_id": "workspace",
        "spec_path": "contracts/openapi.json",
        "name": "OrderListResponse",
        "type": "object",
        "required": ("orders",),
        "properties": ("orders",),
        "title": "Orders API",
        "version": "v1",
    }
    assert facts_by_kind["produces"].identity == {
        "subject": {"type": "service", "id": "service:workspace:orders-api"},
        "relation": "produces",
        "object": {"type": "contract", "id": "contract:workspace:http:get:orders"},
    }
    assert facts_by_kind["produces"].confidence == 1.0


def test_openapi_active_facts_return_warning_for_invalid_openapi_file(tmp_path) -> None:
    spec_file = tmp_path / "openapi.json"
    spec_file.write_text('{"openapi":"3.0.0","paths":{}}\n', encoding="utf-8")

    facts, warnings = openapi_active_facts(
        (_discovered_file(spec_file, relative_path="openapi.json"),),
    )

    assert facts == ()
    assert warnings == (
        OpenAPIWarning(
            repository_id="workspace",
            path="openapi.json",
            message="OpenAPI file is missing an `info` mapping.",
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
        modified_at="2026-06-20T10:00:00Z",
        language=language_for_extension(file_path.suffix),
    )
