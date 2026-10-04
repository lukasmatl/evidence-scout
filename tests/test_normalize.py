from __future__ import annotations

from code_scout.domain.normalize import normalize_graph_facts, normalized_contract_id
from code_scout.models.graph import Concept, Contract, Decision, Edge, Evidence, Service
from code_scout.utils.identifiers import (
    canonical_http_contract_id,
    canonical_http_contract_identity,
    normalize_http_path,
)


def test_normalized_http_contract_ids_are_stable_across_extractors() -> None:
    contracts = (
        _contract("contract:workspace:openapi.yaml:get:users-id", "openapi"),
        _contract("contract:workspace:nestjs:get:users-id", "nestjs"),
        _contract("contract:workspace:nextjs:get:users-id", "nextjs"),
    )

    normalized = normalize_graph_facts(contracts=contracts)

    assert [contract.id for contract in normalized.contracts] == [
        "contract:workspace:http:get:users-id"
    ]
    assert (
        normalized_contract_id(contracts[0]) == "contract:workspace:http:get:users-id"
    )
    assert [
        evidence.extractor_name for evidence in normalized.contracts[0].evidence
    ] == ["nestjs", "nextjs", "openapi"]


def test_canonical_http_contract_identity_includes_method_and_normalized_path() -> None:
    assert (
        canonical_http_contract_id("workspace", "GET", "users/{id}/")
        == "contract:workspace:http:get:users-id"
    )
    assert (
        canonical_http_contract_id("workspace", "POST", "users/{id}/")
        == "contract:workspace:http:post:users-id"
    )


def test_unknown_http_methods_use_distinct_contract_marker() -> None:
    assert (
        canonical_http_contract_id("workspace", None, "/users/{id}")
        == "contract:workspace:http:unknown:users-id"
    )
    assert (
        canonical_http_contract_id("workspace", "BOGUS", "/users/{id}")
        == "contract:workspace:http:unknown:users-id"
    )
    assert (
        canonical_http_contract_id("workspace", "GET", "/users/{id}")
        == "contract:workspace:http:get:users-id"
    )


def test_normalize_http_path_handles_route_syntaxes_deterministically() -> None:
    assert normalize_http_path("users//{id}/") == "/users/{id}"
    assert normalize_http_path("/API/Users/:userId/") == "/api/users/{userId}"
    assert normalize_http_path("/api/users/[id]") == "/api/users/{id}"
    assert normalize_http_path("/api/files/[...path]") == "/api/files/{path}"
    assert normalize_http_path("/api/files/[[...path]]") == "/api/files/{path}"
    assert normalize_http_path("/api/users/<int:userId>/") == "/api/users/{userId}"


def test_canonical_http_contract_identity_preserves_parameter_names() -> None:
    assert canonical_http_contract_identity("GET", "/users/{id}") == (
        "GET",
        "/users/{id}",
    )
    assert canonical_http_contract_identity("GET", "/users/{userId}") == (
        "GET",
        "/users/{userId}",
    )
    assert canonical_http_contract_id(
        "workspace", "GET", "/users/{id}"
    ) != canonical_http_contract_id("workspace", "GET", "/users/{userId}")


def test_normalization_rewrites_edges_and_merges_repeated_evidence() -> None:
    openapi_contract = _contract(
        "contract:workspace:openapi.yaml:get:users-id",
        "openapi",
    )
    nestjs_contract = _contract("contract:workspace:nestjs:get:users-id", "nestjs")
    concept = Concept(id="concept:workspace:users", name="Users")
    duplicate_concept = Concept(id="concept:workspace:users", name="Users API")
    decision = Decision(
        id="decision:workspace:docs/adr/0001-users.md",
        title="Users",
        status="accepted",
        evidence=(_evidence("docs/adr/0001-users.md", "adr_markdown"),),
    )
    duplicate_decision = Decision(
        id="decision:workspace:docs/adr/0001-users.md",
        title="Users",
        status="accepted",
        evidence=(_evidence("docs/adr/0001-users.md", "adr_markdown"),),
    )

    normalized = normalize_graph_facts(
        contracts=(openapi_contract, nestjs_contract),
        concepts=(concept, duplicate_concept),
        decisions=(decision, duplicate_decision),
        edges=(
            _describes_edge(openapi_contract.id, "openapi"),
            _describes_edge(nestjs_contract.id, "nestjs"),
            Edge(
                source_type="repository",
                source_id="workspace",
                target_type="file",
                target_id="file:workspace:package.json",
                relation_type="declares_package_metadata",
                confidence=1.0,
                evidence=(_evidence("package.json", "package_json"),),
            ),
        ),
    )

    assert [concept.id for concept in normalized.concepts] == [
        "concept:workspace:users"
    ]
    assert [decision.id for decision in normalized.decisions] == [
        "decision:workspace:docs/adr/0001-users.md"
    ]
    assert len(normalized.decisions[0].evidence) == 1
    assert [
        (
            edge.source_type,
            edge.source_id,
            edge.target_type,
            edge.target_id,
            edge.relation_type,
        )
        for edge in normalized.edges
    ] == [
        (
            "concept",
            "concept:workspace:users",
            "contract",
            "contract:workspace:http:get:users-id",
            "describes_contract",
        ),
        (
            "repository",
            "workspace",
            "file",
            "file:workspace:package.json",
            "declares_package_metadata",
        ),
    ]
    assert [evidence.extractor_name for evidence in normalized.edges[0].evidence] == [
        "nestjs",
        "openapi",
    ]


def test_dotnet_route_producer_links_to_matching_openapi_contract() -> None:
    dotnet_contract = Contract(
        id="contract:workspace:http:get:api-orders-orderid",
        name="GET /api/Orders/{orderId}",
        kind="http",
        path="/api/Orders/{orderId}",
        method="GET",
        producer_hint="Orders.Api",
        operation_id="Orders.Read",
        response_dtos=("OrderResponse",),
        evidence=(_evidence("src/Orders.Api/OrdersController.cs", "dotnet"),),
    )
    openapi_contract = Contract(
        id="contract:workspace:contracts/openapi.yaml:get:api-orders-orderid",
        name="readOrder",
        kind="http",
        path="/api/orders/{orderId}",
        method="get",
        producer_hint="Orders API",
        operation_id="readOrder",
        response_dtos=("OrderDto",),
        evidence=(_evidence("contracts/openapi.yaml", "openapi"),),
    )
    service = Service(
        id="service:workspace:orders-api",
        name="Orders.Api",
        repository_id="workspace",
        evidence=(_evidence("src/Orders.Api/Orders.Api.csproj", "dotnet"),),
    )

    normalized = normalize_graph_facts(
        services=(service,),
        contracts=(dotnet_contract, openapi_contract),
        edges=(
            Edge(
                source_type="service",
                source_id=service.id,
                target_type="contract",
                target_id=dotnet_contract.id,
                relation_type="produces",
                confidence=1.0,
                evidence=dotnet_contract.evidence,
            ),
            Edge(
                source_type="service",
                source_id=service.id,
                target_type="contract",
                target_id=openapi_contract.id,
                relation_type="exposes",
                confidence=0.95,
                evidence=openapi_contract.evidence,
            ),
        ),
    )

    assert [contract.id for contract in normalized.contracts] == [
        "contract:workspace:http:get:api-orders-orderid",
    ]
    assert normalized.contracts[0].name == "readOrder"
    assert normalized.contracts[0].operation_id == "readOrder"
    assert normalized.contracts[0].response_dtos == ("OrderDto", "OrderResponse")
    assert [
        (
            edge.source_type,
            edge.source_id,
            edge.relation_type,
            edge.target_type,
            edge.target_id,
            edge.confidence,
        )
        for edge in normalized.edges
    ] == [
        (
            "service",
            "service:workspace:orders-api",
            "produces",
            "contract",
            "contract:workspace:http:get:api-orders-orderid",
            1.0,
        ),
    ]
    assert [evidence.extractor_name for evidence in normalized.edges[0].evidence] == [
        "dotnet",
        "openapi",
    ]


def test_frontend_consume_edge_links_to_matching_openapi_contract() -> None:
    frontend_contract = Contract(
        id="contract:workspace:http:get:api-users",
        name="GET /API//Users",
        kind="http",
        path="/API//Users",
        method="GET",
        evidence=(
            Evidence(
                file_path="apps/web/src/api/users.ts",
                extractor_name="frontend_api",
                confidence=0.82,
                start_line=7,
                end_line=7,
                metadata={
                    "repository_id": "workspace",
                    "call_kind": "fetch",
                    "method": "GET",
                    "route": "/API//Users",
                    "url": "https://api.example.test/API//Users?active=true#list",
                },
            ),
        ),
    )
    openapi_contract = Contract(
        id="contract:workspace:contracts/openapi.yaml:get:api-users",
        name="listUsers",
        kind="http",
        path="/api/users",
        method="get",
        operation_id="listUsers",
        response_dtos=("UserListResponse",),
        evidence=(_evidence("contracts/openapi.yaml", "openapi"),),
    )
    consume_edge = Edge(
        source_type="file",
        source_id="file:workspace:apps/web/src/api/users.ts",
        target_type="contract",
        target_id=frontend_contract.id,
        relation_type="consumes",
        confidence=0.82,
        evidence=frontend_contract.evidence,
    )

    normalized = normalize_graph_facts(
        contracts=(frontend_contract, openapi_contract),
        edges=(consume_edge,),
    )

    assert [contract.id for contract in normalized.contracts] == [
        "contract:workspace:http:get:api-users"
    ]
    assert normalized.contracts[0].operation_id == "listUsers"
    assert normalized.edges == (
        Edge(
            source_type="file",
            source_id="file:workspace:apps/web/src/api/users.ts",
            target_type="contract",
            target_id="contract:workspace:http:get:api-users",
            relation_type="consumes",
            confidence=0.82,
            evidence=frontend_contract.evidence,
        ),
    )
    assert normalized.edges[0].evidence[0].metadata["url"] == (
        "https://api.example.test/API//Users?active=true#list"
    )


def test_normalization_preserves_non_contract_exposes_relations() -> None:
    edge = Edge(
        source_type="service",
        source_id="service:workspace:api",
        target_type="file",
        target_id="file:workspace:docker-compose.yml",
        relation_type="exposes",
        confidence=0.8,
        evidence=(_evidence("docker-compose.yml", "infra"),),
    )

    normalized = normalize_graph_facts(edges=(edge,))

    assert normalized.edges[0].relation_type == "exposes"


def _contract(raw_id: str, extractor_name: str) -> Contract:
    return Contract(
        id=raw_id,
        name="GET /users/:id",
        kind="http",
        path="/users/:id",
        method="GET",
        evidence=(_evidence(f"{extractor_name}.ts", extractor_name),),
    )


def _describes_edge(contract_id: str, extractor_name: str) -> Edge:
    return Edge(
        source_type="concept",
        source_id="concept:workspace:users",
        target_type="contract",
        target_id=contract_id,
        relation_type="describes_contract",
        confidence=1.0,
        evidence=(_evidence(f"{extractor_name}.ts", extractor_name),),
    )


def _evidence(file_path: str, extractor_name: str) -> Evidence:
    return Evidence(
        file_path=file_path,
        extractor_name=extractor_name,
        confidence=1.0,
        start_line=1,
        end_line=1,
        metadata={"repository_id": "workspace"},
    )
