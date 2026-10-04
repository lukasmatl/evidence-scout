from __future__ import annotations

import sqlite3

from code_scout.domain.resolver import resolve_active_facts
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.storage import initialize_schema, list_active_facts, upsert_active_facts


def test_resolver_converts_single_service_fact_to_graph_node() -> None:
    fact = ActiveFact(
        id="fact:entity:graphify:billing",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/billing.ts",
        identity={"entity_type": "service", "entity_id": "service:billing"},
        properties={
            "name": "Billing",
            "repository_id": "workspace",
            "description": "Handles billing workflows",
        },
        evidence=(
            FactEvidence(
                file_path="apps/api/src/billing.ts",
                extractor_name="graphify",
                confidence=0.89,
                start_line=4,
                end_line=17,
                metadata={"symbol": "BillingService"},
            ),
        ),
        confidence=0.89,
        extractor_version="1.0.0",
    )

    resolved = resolve_active_facts((fact,))

    assert len(resolved.services) == 1
    assert resolved.services[0].id == "service:billing"
    assert resolved.services[0].name == "Billing"
    assert resolved.services[0].repository_id == "workspace"
    assert resolved.services[0].description == "Handles billing workflows"
    assert resolved.services[0].evidence[0].file_path == "apps/api/src/billing.ts"
    assert resolved.services[0].evidence[0].extractor_name == "graphify"
    assert resolved.services[0].evidence[0].metadata == {"symbol": "BillingService"}


def test_resolver_groups_entity_facts_by_identity_and_preserves_evidence() -> None:
    graphify_fact = ActiveFact(
        id="fact:entity:graphify:orders",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/orders.ts",
        identity={"entity_type": "service", "entity_id": "service:orders"},
        properties={
            "name": "Orders",
            "repository_id": "workspace",
        },
        evidence=(
            FactEvidence(
                file_path="apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.91,
                start_line=7,
                end_line=19,
                metadata={"symbol": "OrdersService"},
            ),
        ),
        confidence=0.91,
        extractor_version="1.0.0",
    )
    dotnet_fact = ActiveFact(
        id="fact:entity:dotnet:orders",
        extractor="dotnet",
        kind="service",
        source_file="apps/api/OrdersController.cs",
        identity={"entity_type": "service", "entity_id": "service:orders"},
        properties={
            "name": "Orders API",
            "description": "Handles order workflows",
        },
        evidence=(
            FactEvidence(
                file_path="apps/api/OrdersController.cs",
                extractor_name="dotnet",
                confidence=0.96,
                start_line=3,
                end_line=42,
                metadata={"controller": "OrdersController"},
            ),
        ),
        confidence=0.96,
    )

    resolved = resolve_active_facts((graphify_fact, dotnet_fact))
    reversed_resolved = resolve_active_facts((dotnet_fact, graphify_fact))

    assert len(resolved.services) == 1
    assert reversed_resolved.services == resolved.services
    assert resolved.services[0].id == "service:orders"
    assert resolved.services[0].name == "Orders"
    assert resolved.services[0].repository_id == "workspace"
    assert resolved.services[0].description == "Handles order workflows"
    evidence_by_extractor = {
        evidence.extractor_name: evidence for evidence in resolved.services[0].evidence
    }
    assert set(evidence_by_extractor) == {"dotnet", "graphify"}
    assert evidence_by_extractor["dotnet"].metadata["controller"] == (
        "OrdersController"
    )
    assert evidence_by_extractor["dotnet"].metadata["resolver_conflicts"] == (
        {
            "field": "name",
            "conflicting_value": "Orders API",
            "resolved_value": "Orders",
        },
    )
    assert evidence_by_extractor["graphify"].metadata == {"symbol": "OrdersService"}


def test_resolver_merges_service_aliases_and_preserves_evidence() -> None:
    graphify_fact = ActiveFact(
        id="fact:entity:graphify:billing-service",
        extractor="graphify",
        kind="service",
        source_file="graphify/services.json",
        identity={
            "entity_type": "service",
            "entity_id": "service:billing-service",
        },
        properties={
            "name": "BillingService",
            "repository_id": "workspace",
            "aliases": ("Billing",),
        },
        evidence=(
            FactEvidence(
                file_path="graphify/services.json",
                extractor_name="graphify",
                confidence=0.91,
                metadata={"symbol": "BillingService"},
            ),
        ),
        confidence=0.91,
    )
    openapi_fact = ActiveFact(
        id="fact:entity:openapi:billing",
        extractor="openapi",
        kind="service",
        source_file="contracts/billing.yaml",
        identity={"entity_type": "service", "entity_id": "service:billing"},
        properties={"name": "billing", "repository_id": "workspace"},
        evidence=(
            FactEvidence(
                file_path="contracts/billing.yaml",
                extractor_name="openapi",
                confidence=1.0,
            ),
        ),
        confidence=1.0,
    )
    docs_fact = ActiveFact(
        id="fact:entity:docs:billing-domain",
        extractor="docs",
        kind="service",
        source_file="docs/billing.md",
        identity={"entity_type": "service", "entity_id": "service:billing-domain"},
        properties={
            "name": "Billing domain",
            "description": "Owns invoicing workflows.",
            "aliases": ("Billing",),
        },
        evidence=(
            FactEvidence(
                file_path="docs/billing.md",
                extractor_name="docs",
                confidence=0.72,
                metadata={"domain": "Billing"},
            ),
        ),
        confidence=0.72,
    )

    resolved = resolve_active_facts((docs_fact, openapi_fact, graphify_fact))
    reversed_resolved = resolve_active_facts((graphify_fact, openapi_fact, docs_fact))

    assert resolved.services == reversed_resolved.services
    assert len(resolved.services) == 1
    service = resolved.services[0]
    assert service.id == "service:billing-service"
    assert service.name == "BillingService"
    assert service.repository_id == "workspace"
    assert service.description == "Owns invoicing workflows."
    assert service.aliases == ("Billing", "Billing domain", "BillingService", "billing")
    evidence_by_extractor = {
        evidence.extractor_name: evidence for evidence in service.evidence
    }
    assert set(evidence_by_extractor) == {"docs", "graphify", "openapi"}
    assert evidence_by_extractor["graphify"].metadata["symbol"] == "BillingService"
    assert evidence_by_extractor["docs"].metadata["domain"] == "Billing"


def test_resolver_groups_relation_facts_by_identity_and_preserves_evidence() -> None:
    subject = {"type": "service", "id": "service:orders"}
    object_ = {"type": "contract", "id": "contract:workspace:http:get:orders"}
    openapi_fact = ActiveFact(
        id="fact:relation:openapi:orders",
        extractor="openapi",
        kind="exposes",
        source_file="contracts/openapi.yaml",
        identity={
            "subject": subject,
            "relation": "exposes",
            "object": object_,
        },
        properties={},
        evidence=(
            FactEvidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
                metadata={"operation_id": "listOrders"},
            ),
        ),
        confidence=0.88,
    )
    dotnet_fact = ActiveFact(
        id="fact:relation:dotnet:orders",
        extractor="dotnet",
        kind="exposes",
        source_file="apps/api/OrdersController.cs",
        identity={
            "subject": subject,
            "relation": "exposes",
            "object": object_,
        },
        properties={},
        evidence=(
            FactEvidence(
                file_path="apps/api/OrdersController.cs",
                extractor_name="dotnet",
                confidence=0.94,
                start_line=12,
                end_line=18,
                metadata={"route": "GET /orders"},
            ),
        ),
        confidence=0.94,
    )

    resolved = resolve_active_facts((openapi_fact, dotnet_fact))

    assert len(resolved.edges) == 1
    assert resolved.edges[0].source_type == "service"
    assert resolved.edges[0].source_id == "service:orders"
    assert resolved.edges[0].target_type == "contract"
    assert resolved.edges[0].target_id == "contract:workspace:http:get:orders"
    assert resolved.edges[0].relation_type == "produces"
    assert resolved.edges[0].confidence == 0.94
    assert [evidence.extractor_name for evidence in resolved.edges[0].evidence] == [
        "dotnet",
        "openapi",
    ]
    assert resolved.edges[0].evidence[0].metadata == {"route": "GET /orders"}
    assert resolved.edges[0].evidence[1].metadata == {"operation_id": "listOrders"}


def test_resolver_converts_openapi_contract_fact_to_graph_node() -> None:
    fact = _contract_fact(
        id="fact:entity:openapi:list-orders",
        extractor="openapi",
        source_file="contracts/openapi.yaml",
        entity_id="contract:workspace:contracts/openapi.yaml:get:orders",
        properties={
            "name": "listOrders",
            "kind": "http",
            "path": "/orders",
            "version": "v1",
            "method": "GET",
            "producer_hint": "Orders API",
            "operation_id": "listOrders",
            "request_dtos": (),
            "response_dtos": ("OrderListResponse",),
        },
        metadata={
            "repository_id": "workspace",
            "operation_id": "listOrders",
        },
    )

    resolved = resolve_active_facts((fact,))

    assert len(resolved.contracts) == 1
    assert resolved.contracts[0].id == "contract:workspace:http:get:orders"
    assert resolved.contracts[0].name == "listOrders"
    assert resolved.contracts[0].kind == "http"
    assert resolved.contracts[0].path == "/orders"
    assert resolved.contracts[0].version == "v1"
    assert resolved.contracts[0].method == "GET"
    assert resolved.contracts[0].producer_hint == "Orders API"
    assert resolved.contracts[0].operation_id == "listOrders"
    assert resolved.contracts[0].request_dtos == ()
    assert resolved.contracts[0].response_dtos == ("OrderListResponse",)
    assert resolved.contracts[0].evidence[0].file_path == "contracts/openapi.yaml"
    assert resolved.contracts[0].evidence[0].extractor_name == "openapi"
    assert resolved.contracts[0].evidence[0].metadata == {
        "repository_id": "workspace",
        "operation_id": "listOrders",
    }


def test_resolver_converts_inferred_frontend_contract_fact_to_graph_node() -> None:
    fact = _contract_fact(
        id="fact:entity:frontend-api:get-orders",
        extractor="frontend_api",
        source_file="apps/web/src/api/orders.ts",
        entity_id="contract:workspace:frontend_api:get:orders",
        properties={
            "name": "GET /orders",
            "kind": "http",
            "path": "/orders",
            "method": "GET",
        },
        metadata={
            "repository_id": "workspace",
            "call_kind": "fetch",
            "route": "/orders",
        },
        confidence=0.74,
    )

    resolved = resolve_active_facts((fact,))

    assert len(resolved.contracts) == 1
    assert resolved.contracts[0].id == "contract:workspace:http:get:orders"
    assert resolved.contracts[0].name == "GET /orders"
    assert resolved.contracts[0].kind == "http"
    assert resolved.contracts[0].path == "/orders"
    assert resolved.contracts[0].method == "GET"
    assert resolved.contracts[0].version is None
    assert resolved.contracts[0].producer_hint is None
    assert resolved.contracts[0].operation_id is None
    assert resolved.contracts[0].evidence[0].extractor_name == "frontend_api"
    assert resolved.contracts[0].evidence[0].metadata == {
        "repository_id": "workspace",
        "call_kind": "fetch",
        "route": "/orders",
    }


def test_resolver_merges_duplicate_http_contract_facts_deterministically() -> None:
    openapi_fact = _contract_fact(
        id="fact:entity:openapi:create-order",
        extractor="openapi",
        source_file="contracts/openapi.yaml",
        entity_id="contract:workspace:contracts/openapi.yaml:post:orders",
        properties={
            "name": "createOrder",
            "kind": "http",
            "path": "/orders",
            "version": "v1",
            "method": "POST",
            "producer_hint": "Orders API",
            "operation_id": "createOrder",
            "request_dtos": ("CreateOrderRequest",),
            "response_dtos": ("OrderResponse",),
        },
        metadata={
            "repository_id": "workspace",
            "operation_id": "createOrder",
        },
    )
    inferred_fact = _contract_fact(
        id="fact:entity:frontend-api:create-order",
        extractor="frontend_api",
        source_file="apps/web/src/api/orders.ts",
        entity_id="contract:workspace:frontend_api:post:orders",
        properties={
            "name": "POST /orders",
            "kind": "http",
            "path": "/orders",
            "method": "POST",
            "request_dtos": ("OrderForm",),
            "response_dtos": ("CreateOrderResult",),
        },
        metadata={
            "repository_id": "workspace",
            "call_kind": "fetch",
            "route": "/orders",
        },
        confidence=0.74,
    )

    resolved = resolve_active_facts((openapi_fact, inferred_fact))
    reversed_resolved = resolve_active_facts((inferred_fact, openapi_fact))

    assert len(resolved.contracts) == 1
    assert reversed_resolved.contracts == resolved.contracts
    assert resolved.contracts[0].id == "contract:workspace:http:post:orders"
    assert resolved.contracts[0].name == "createOrder"
    assert resolved.contracts[0].kind == "http"
    assert resolved.contracts[0].path == "/orders"
    assert resolved.contracts[0].version == "v1"
    assert resolved.contracts[0].method == "POST"
    assert resolved.contracts[0].producer_hint == "Orders API"
    assert resolved.contracts[0].operation_id == "createOrder"
    assert resolved.contracts[0].request_dtos == (
        "CreateOrderRequest",
        "OrderForm",
    )
    assert resolved.contracts[0].response_dtos == (
        "CreateOrderResult",
        "OrderResponse",
    )
    assert [evidence.extractor_name for evidence in resolved.contracts[0].evidence] == [
        "frontend_api",
        "openapi",
    ]


def test_resolver_prefers_asyncapi_contract_fields_over_inferred_contracts() -> None:
    asyncapi_fact = _contract_fact(
        id="fact:entity:asyncapi:user-created",
        extractor="asyncapi",
        source_file="contracts/asyncapi.yaml",
        entity_id="contract:workspace:asyncapi:post:events-users-created",
        properties={
            "name": "UserCreated",
            "kind": "http",
            "path": "/events/users/created",
            "version": "v2",
            "method": "POST",
            "producer_hint": "Events API",
            "operation_id": "publishUserCreated",
            "request_dtos": ("UserCreatedEvent",),
        },
        metadata={"repository_id": "workspace"},
    )
    inferred_fact = _contract_fact(
        id="fact:entity:frontend-api:user-created",
        extractor="frontend_api",
        source_file="apps/web/src/events.ts",
        entity_id="contract:workspace:frontend_api:post:events-users-created",
        properties={
            "name": "POST /events/users/created",
            "kind": "http",
            "path": "/events/users/created",
            "method": "POST",
            "producer_hint": "Web Events",
            "operation_id": "emitUserCreated",
        },
        metadata={"repository_id": "workspace"},
        confidence=0.71,
    )

    resolved = resolve_active_facts((inferred_fact, asyncapi_fact))
    reversed_resolved = resolve_active_facts((asyncapi_fact, inferred_fact))

    assert reversed_resolved.contracts == resolved.contracts
    assert resolved.contracts[0].name == "UserCreated"
    assert resolved.contracts[0].version == "v2"
    assert resolved.contracts[0].producer_hint == "Events API"
    assert resolved.contracts[0].operation_id == "publishUserCreated"


def test_resolver_prefers_lsp_reference_confidence_over_tree_sitter() -> None:
    subject = {"type": "file", "id": "file:workspace:src/orders.ts"}
    object_ = {"type": "symbol", "id": "symbol:workspace:src/orders.ts:Orders"}
    tree_sitter_fact = _relation_fact(
        id="fact:relation:tree-sitter:orders",
        extractor="tree_sitter",
        source_file="src/orders.ts",
        subject=subject,
        relation="declares_symbol",
        object=object_,
        confidence=1.0,
    )
    lsp_fact = _relation_fact(
        id="fact:relation:lsp:orders",
        extractor="lsp",
        source_file="src/orders.ts",
        subject=subject,
        relation="declares_symbol",
        object=object_,
        confidence=0.82,
    )

    resolved = resolve_active_facts((tree_sitter_fact, lsp_fact))
    reversed_resolved = resolve_active_facts((lsp_fact, tree_sitter_fact))

    assert reversed_resolved.edges == resolved.edges
    assert len(resolved.edges) == 1
    assert resolved.edges[0].confidence == 0.82
    assert [evidence.extractor_name for evidence in resolved.edges[0].evidence] == [
        "lsp",
        "tree_sitter",
    ]


def test_resolver_keeps_adr_rationale_without_overriding_contract_fields() -> None:
    openapi_fact = _contract_fact(
        id="fact:entity:openapi:list-payments",
        extractor="openapi",
        source_file="contracts/openapi.yaml",
        entity_id="contract:workspace:contracts/openapi.yaml:get:payments",
        properties={
            "name": "listPayments",
            "kind": "http",
            "path": "/payments",
            "version": "v1",
            "method": "GET",
            "operation_id": "listPayments",
        },
        metadata={"repository_id": "workspace"},
    )
    adr_contract_fact = _contract_fact(
        id="fact:entity:adr:list-payments",
        extractor="adr",
        source_file="docs/adr/0003-payments.md",
        entity_id="contract:workspace:docs:payments:get",
        properties={
            "name": "Payments ADR route",
            "kind": "http",
            "path": "/payments",
            "version": "adr-draft",
            "method": "GET",
            "operation_id": "documentedPayments",
        },
        metadata={"repository_id": "workspace", "adr_id": "0003"},
        confidence=0.96,
    )
    rationale_fact = _relation_fact(
        id="fact:relation:adr:payments-rationale",
        extractor="adr",
        source_file="docs/adr/0003-payments.md",
        subject={
            "type": "decision",
            "id": "decision:workspace:docs/adr/0003-payments.md",
        },
        relation="affects",
        object={"type": "contract", "id": "contract:workspace:http:get:payments"},
        confidence=0.9,
    )

    resolved = resolve_active_facts((adr_contract_fact, rationale_fact, openapi_fact))

    assert len(resolved.contracts) == 1
    assert resolved.contracts[0].name == "listPayments"
    assert resolved.contracts[0].version == "v1"
    assert resolved.contracts[0].operation_id == "listPayments"
    assert [evidence.extractor_name for evidence in resolved.contracts[0].evidence] == [
        "adr",
        "openapi",
    ]
    assert len(resolved.edges) == 1
    assert resolved.edges[0].source_type == "decision"
    assert resolved.edges[0].relation_type == "affects"
    assert resolved.edges[0].target_id == "contract:workspace:http:get:payments"


def test_resolver_keeps_docs_relation_confidence_below_code_facts() -> None:
    subject = {"type": "concept", "id": "concept:workspace:payments"}
    object_ = {"type": "contract", "id": "contract:workspace:http:get:payments"}
    docs_fact = _relation_fact(
        id="fact:relation:docs:payments",
        extractor="docs",
        source_file="docs/contracts.md",
        subject=subject,
        relation="describes_contract",
        object=object_,
        confidence=0.99,
    )
    graphify_fact = _relation_fact(
        id="fact:relation:graphify:payments",
        extractor="graphify",
        source_file="src/payments.ts",
        subject=subject,
        relation="describes_contract",
        object=object_,
        confidence=0.77,
    )

    resolved = resolve_active_facts((docs_fact, graphify_fact))
    reversed_resolved = resolve_active_facts((graphify_fact, docs_fact))

    assert reversed_resolved.edges == resolved.edges
    assert len(resolved.edges) == 1
    assert resolved.edges[0].confidence == 0.77
    assert [evidence.extractor_name for evidence in resolved.edges[0].evidence] == [
        "graphify",
        "docs",
    ]


def test_resolver_marks_contract_conflicts_without_hiding_active_facts() -> None:
    openapi_fact = _contract_fact(
        id="fact:entity:openapi:update-payment",
        extractor="openapi",
        source_file="contracts/openapi.yaml",
        entity_id="contract:workspace:contracts/openapi.yaml:patch:payments-id",
        properties={
            "name": "updatePayment",
            "kind": "http",
            "path": "/payments/{id}",
            "method": "PATCH",
            "operation_id": "updatePayment",
        },
        metadata={"repository_id": "workspace"},
    )
    inferred_fact = _contract_fact(
        id="fact:entity:frontend-api:update-payment",
        extractor="frontend_api",
        source_file="apps/web/src/payments.ts",
        entity_id="contract:workspace:frontend_api:patch:payments-id",
        properties={
            "name": "PATCH /payments/:id",
            "kind": "http",
            "path": "/payments/{id}",
            "method": "PATCH",
            "operation_id": "patchPaymentFromUi",
        },
        metadata={"repository_id": "workspace"},
        confidence=0.72,
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    upsert_active_facts(connection, (openapi_fact, inferred_fact))

    active_facts = list_active_facts(connection)
    resolved = resolve_active_facts(active_facts)

    assert {fact.id for fact in active_facts} == {
        "fact:entity:frontend-api:update-payment",
        "fact:entity:openapi:update-payment",
    }
    assert resolved.contracts[0].name == "updatePayment"
    assert resolved.contracts[0].operation_id == "updatePayment"
    frontend_evidence = _evidence_by_extractor(
        resolved.contracts[0].evidence,
        "frontend_api",
    )
    assert frontend_evidence.metadata["resolver_conflicts"] == (
        {
            "field": "name",
            "conflicting_value": "PATCH /payments/:id",
            "resolved_value": "updatePayment",
        },
        {
            "field": "operation_id",
            "conflicting_value": "patchPaymentFromUi",
            "resolved_value": "updatePayment",
        },
    )


def test_resolver_marks_code_symbol_conflicts() -> None:
    tree_sitter_fact = _symbol_fact(
        id="fact:entity:tree-sitter:payments-symbol",
        extractor="tree_sitter",
        source_file="src/payments.ts",
        entity_id="symbol:workspace:src/payments.ts:PaymentClient",
        properties={
            "file_id": "file:workspace:src/payments.ts",
            "name": "PaymentClient",
            "kind": "class",
            "qualified_name": "PaymentClient",
            "start_line": 10,
            "end_line": 45,
        },
        confidence=1.0,
    )
    lsp_fact = _symbol_fact(
        id="fact:entity:lsp:payments-symbol",
        extractor="lsp",
        source_file="src/payments.ts",
        entity_id="symbol:workspace:src/payments.ts:PaymentClient",
        properties={
            "file_id": "file:workspace:src/payments.ts",
            "name": "PaymentClient",
            "kind": "class",
            "qualified_name": "PaymentsClient",
            "start_line": 12,
            "end_line": 48,
        },
        confidence=0.86,
    )

    resolved = resolve_active_facts((tree_sitter_fact, lsp_fact))

    assert len(resolved.symbols) == 1
    assert resolved.symbols[0].qualified_name == "PaymentsClient"
    assert resolved.symbols[0].start_line == 12
    tree_sitter_evidence = _evidence_by_extractor(
        resolved.symbols[0].evidence,
        "tree_sitter",
    )
    assert tree_sitter_evidence.metadata["resolver_conflicts"] == (
        {
            "field": "qualified_name",
            "conflicting_value": "PaymentClient",
            "resolved_value": "PaymentsClient",
        },
        {"field": "start_line", "conflicting_value": 10, "resolved_value": 12},
        {"field": "end_line", "conflicting_value": 45, "resolved_value": 48},
    )


def test_resolver_marks_adr_decision_conflicts() -> None:
    docs_fact = _decision_fact(
        id="fact:entity:docs:adr-state",
        extractor="docs",
        source_file="docs/architecture.md",
        entity_id="decision:workspace:docs/adr/0007-payments.md",
        properties={
            "title": "Payments routing is proposed",
            "status": "proposed",
            "path": "docs/adr/0007-payments.md",
            "decision": "Route payments through the legacy service.",
        },
        metadata={"repository_id": "workspace"},
        confidence=0.88,
    )
    adr_fact = _decision_fact(
        id="fact:entity:adr:adr-state",
        extractor="adr",
        source_file="docs/adr/0007-payments.md",
        entity_id="decision:workspace:docs/adr/0007-payments.md",
        properties={
            "title": "Route payments through Payments API",
            "status": "accepted",
            "path": "docs/adr/0007-payments.md",
            "decision": "Route payments through the Payments API.",
        },
        metadata={"repository_id": "workspace"},
        confidence=0.96,
    )

    resolved = resolve_active_facts((docs_fact, adr_fact))

    assert resolved.decisions[0].title == "Route payments through Payments API"
    assert resolved.decisions[0].status == "accepted"
    docs_evidence = _evidence_by_extractor(resolved.decisions[0].evidence, "docs")
    assert docs_evidence.metadata["resolver_conflicts"] == (
        {
            "field": "title",
            "conflicting_value": "Payments routing is proposed",
            "resolved_value": "Route payments through Payments API",
        },
        {
            "field": "status",
            "conflicting_value": "proposed",
            "resolved_value": "accepted",
        },
        {
            "field": "decision",
            "conflicting_value": "Route payments through the legacy service.",
            "resolved_value": "Route payments through the Payments API.",
        },
    )


def test_resolver_marks_docs_concept_conflicts() -> None:
    docs_fact = _concept_fact(
        id="fact:entity:docs:payment-boundary",
        extractor="docs",
        source_file="docs/concepts.md",
        entity_id="concept:workspace:payment-boundary",
        properties={
            "name": "Payment Boundary",
            "description": "Payments are owned by the legacy monolith.",
        },
        metadata={"heading": "Payment Boundary"},
        confidence=0.85,
    )
    adr_fact = _concept_fact(
        id="fact:entity:adr:payment-boundary",
        extractor="adr",
        source_file="docs/adr/0008-payments.md",
        entity_id="concept:workspace:payment-boundary",
        properties={
            "name": "Payments Boundary",
            "description": "Payments are owned by the Payments API.",
        },
        metadata={"adr_id": "0008"},
        confidence=0.96,
    )

    resolved = resolve_active_facts((docs_fact, adr_fact))

    assert resolved.concepts[0].name == "Payment Boundary"
    assert resolved.concepts[0].description == (
        "Payments are owned by the Payments API."
    )
    docs_evidence = _evidence_by_extractor(resolved.concepts[0].evidence, "docs")
    assert docs_evidence.metadata["resolver_conflicts"] == (
        {
            "field": "description",
            "conflicting_value": "Payments are owned by the legacy monolith.",
            "resolved_value": "Payments are owned by the Payments API.",
        },
    )


def test_resolver_merges_concept_facts_deterministically() -> None:
    docs_fact = _concept_fact(
        id="fact:entity:docs:architecture-memory",
        extractor="docs",
        source_file="docs/concepts.md",
        entity_id="concept:workspace:architecture-memory",
        properties={
            "name": "Architecture Memory",
            "description": "Reusable knowledge captured from project docs.",
        },
        metadata={"heading": "Architecture Memory"},
        confidence=0.88,
    )
    adr_fact = _concept_fact(
        id="fact:entity:adr:architecture-memory",
        extractor="adr",
        source_file="docs/adr/0001-memory.md",
        entity_id="concept:workspace:architecture-memory",
        properties={
            "name": "Memory Architecture",
            "description": "ADR-linked memory architecture concept.",
        },
        metadata={"adr_id": "0001", "source": "title"},
        confidence=0.92,
    )

    resolved = resolve_active_facts((docs_fact, adr_fact))
    reversed_resolved = resolve_active_facts((adr_fact, docs_fact))

    assert len(resolved.concepts) == 1
    assert reversed_resolved.concepts == resolved.concepts
    assert resolved.concepts[0].id == "concept:workspace:architecture-memory"
    assert resolved.concepts[0].name == "Architecture Memory"
    assert resolved.concepts[0].description == "ADR-linked memory architecture concept."
    evidence_by_extractor = {
        evidence.extractor_name: evidence for evidence in resolved.concepts[0].evidence
    }
    assert set(evidence_by_extractor) == {"adr", "docs"}
    assert evidence_by_extractor["adr"].metadata["adr_id"] == "0001"
    assert evidence_by_extractor["adr"].metadata["source"] == "title"
    assert evidence_by_extractor["adr"].metadata["resolver_conflicts"] == (
        {
            "field": "name",
            "conflicting_value": "Memory Architecture",
            "resolved_value": "Architecture Memory",
        },
    )
    assert evidence_by_extractor["docs"].metadata["heading"] == "Architecture Memory"
    assert evidence_by_extractor["docs"].metadata["resolver_conflicts"] == (
        {
            "field": "description",
            "conflicting_value": "Reusable knowledge captured from project docs.",
            "resolved_value": "ADR-linked memory architecture concept.",
        },
    )


def test_resolver_converts_adr_decision_fact_to_graph_node() -> None:
    fact = _decision_fact(
        id="fact:entity:adr:use-sqlite",
        extractor="adr",
        source_file="docs/adr/0001-use-sqlite.md",
        entity_id="decision:workspace:docs/adr/0001-use-sqlite.md",
        properties={
            "adr_id": "0001",
            "title": "Use SQLite",
            "status": "accepted",
            "path": "docs/adr/0001-use-sqlite.md",
            "decided_at": "2026-06-20",
            "context": "code-scout needs portable local storage.",
            "decision": "Use SQLite through the stdlib sqlite3 module.",
            "consequences": "The database remains easy to inspect and migrate.",
        },
        metadata={"repository_id": "workspace", "field": "decision"},
        confidence=0.96,
    )

    resolved = resolve_active_facts((fact,))

    assert len(resolved.decisions) == 1
    assert resolved.decisions[0].id == "decision:workspace:docs/adr/0001-use-sqlite.md"
    assert resolved.decisions[0].adr_id == "0001"
    assert resolved.decisions[0].title == "Use SQLite"
    assert resolved.decisions[0].status == "accepted"
    assert resolved.decisions[0].path == "docs/adr/0001-use-sqlite.md"
    assert resolved.decisions[0].decided_at == "2026-06-20"
    assert resolved.decisions[0].context == "code-scout needs portable local storage."
    assert (
        resolved.decisions[0].decision
        == "Use SQLite through the stdlib sqlite3 module."
    )
    assert (
        resolved.decisions[0].consequences
        == "The database remains easy to inspect and migrate."
    )
    assert resolved.decisions[0].evidence[0].file_path == (
        "docs/adr/0001-use-sqlite.md"
    )
    assert resolved.decisions[0].evidence[0].extractor_name == "adr"
    assert resolved.decisions[0].evidence[0].confidence == 0.96
    assert resolved.decisions[0].evidence[0].metadata == {
        "repository_id": "workspace",
        "field": "decision",
    }


def _contract_fact(
    *,
    id: str,
    extractor: str,
    source_file: str,
    entity_id: str,
    properties: dict[str, object],
    metadata: dict[str, object],
    confidence: float = 1.0,
) -> ActiveFact:
    return ActiveFact(
        id=id,
        extractor=extractor,
        kind="contract",
        source_file=source_file,
        identity={"entity_type": "contract", "entity_id": entity_id},
        properties=properties,
        evidence=(
            FactEvidence(
                file_path=source_file,
                extractor_name=extractor,
                confidence=confidence,
                metadata=metadata,
            ),
        ),
        confidence=confidence,
    )


def _concept_fact(
    *,
    id: str,
    extractor: str,
    source_file: str,
    entity_id: str,
    properties: dict[str, object],
    metadata: dict[str, object],
    confidence: float,
) -> ActiveFact:
    return _entity_fact(
        id=id,
        extractor=extractor,
        source_file=source_file,
        entity_type="concept",
        entity_id=entity_id,
        properties=properties,
        metadata=metadata,
        confidence=confidence,
    )


def _decision_fact(
    *,
    id: str,
    extractor: str,
    source_file: str,
    entity_id: str,
    properties: dict[str, object],
    metadata: dict[str, object],
    confidence: float,
) -> ActiveFact:
    return _entity_fact(
        id=id,
        extractor=extractor,
        source_file=source_file,
        entity_type="decision",
        entity_id=entity_id,
        properties=properties,
        metadata=metadata,
        confidence=confidence,
    )


def _symbol_fact(
    *,
    id: str,
    extractor: str,
    source_file: str,
    entity_id: str,
    properties: dict[str, object],
    confidence: float,
) -> ActiveFact:
    return _entity_fact(
        id=id,
        extractor=extractor,
        source_file=source_file,
        entity_type="symbol",
        entity_id=entity_id,
        properties=properties,
        metadata={"repository_id": "workspace"},
        confidence=confidence,
    )


def _relation_fact(
    *,
    id: str,
    extractor: str,
    source_file: str,
    subject: dict[str, str],
    relation: str,
    object: dict[str, str],
    confidence: float,
) -> ActiveFact:
    return ActiveFact(
        id=id,
        extractor=extractor,
        kind=relation,
        source_file=source_file,
        identity={"subject": subject, "relation": relation, "object": object},
        properties={},
        evidence=(
            FactEvidence(
                file_path=source_file,
                extractor_name=extractor,
                confidence=confidence,
            ),
        ),
        confidence=confidence,
    )


def _evidence_by_extractor(evidence: tuple[object, ...], extractor_name: str) -> object:
    return next(item for item in evidence if item.extractor_name == extractor_name)


def _entity_fact(
    *,
    id: str,
    extractor: str,
    source_file: str,
    entity_type: str,
    entity_id: str,
    properties: dict[str, object],
    metadata: dict[str, object],
    confidence: float,
) -> ActiveFact:
    return ActiveFact(
        id=id,
        extractor=extractor,
        kind=entity_type,
        source_file=source_file,
        identity={"entity_type": entity_type, "entity_id": entity_id},
        properties=properties,
        evidence=(
            FactEvidence(
                file_path=source_file,
                extractor_name=extractor,
                confidence=confidence,
                metadata=metadata,
            ),
        ),
        confidence=confidence,
    )


def test_assessments_survive_agreement_conflicts_and_scalar_precedence() -> None:
    import json
    from dataclasses import asdict, replace

    high = _contract_fact(
        id="fact:high",
        extractor="openapi",
        source_file="api.yaml",
        entity_id="contract:shared",
        properties={"name": "API", "kind": "event", "version": "v1"},
        metadata={"validation": "corroborated"},
    )
    low = replace(
        high,
        id="fact:low",
        extractor="graphify",
        confidence=0.2,
        evidence=(
            FactEvidence(
                "api.yaml",
                "graphify",
                0.2,
                metadata={"unresolved": True, "assessment": {"conflicted": False}},
            ),
        ),
    )
    agreed = resolve_active_facts((high, low)).contracts[0]
    assert len(agreed.evidence) == 2
    assert {e.assessment["validation"] for e in agreed.evidence} == {
        "corroborated",
        "unresolved",
    }
    assert not any(e.assessment["conflicted"] for e in agreed.evidence)

    low = replace(low, properties={**low.properties, "version": "v2"})
    resolved = resolve_active_facts((high, low))
    contract = resolved.contracts[0]
    assert contract.version == "v1"
    inferred = next(e for e in contract.evidence if e.extractor_name == "graphify")
    assert inferred.assessment["origin"] == "inferred"
    assert inferred.assessment["validation"] == "unresolved"
    assert inferred.assessment["conflicted"] is True
    assert inferred.metadata["resolver_conflicts"]
    assert json.dumps(asdict(resolved), sort_keys=True) == json.dumps(
        asdict(resolve_active_facts((low, high))),
        sort_keys=True,
    )


def test_fact_level_uncertainty_survives_provider_metadata_and_missing_evidence() -> (
    None
):
    from dataclasses import replace

    fact = _relation_fact(
        id="fact:derived",
        extractor="graphify",
        source_file="app.py",
        subject={"type": "symbol", "id": "a"},
        relation="calls",
        object={"type": "symbol", "id": "b"},
        confidence=0.9,
    )
    fact = replace(
        fact,
        properties={"unresolved": True},
        conflict_metadata={"resolver_conflicts": [{"field": "target"}]},
        evidence=(
            replace(
                fact.evidence[0],
                metadata={
                    "unresolved": False,
                    "assessment": {"validation": "corroborated", "conflicted": False},
                },
            ),
        ),
    )
    evidence = resolve_active_facts((fact,)).edges[0].evidence[0]
    assert evidence.assessment["validation"] == "unresolved"
    assert evidence.assessment["conflicted"] is True
    assert evidence.assessment["origin"] == "inferred"
    unknown = (
        resolve_active_facts(
            (
                replace(
                    fact, extractor="", evidence=(), properties={}, conflict_metadata={}
                ),
            )
        )
        .edges[0]
        .evidence[0]
    )
    assert unknown.assessment["origin"] == "unknown"
    assert unknown.assessment["validation"] == "unknown"
