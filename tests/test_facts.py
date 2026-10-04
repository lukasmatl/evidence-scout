from __future__ import annotations

import json
from dataclasses import is_dataclass

from code_scout.extractors.package_json import RepositoryPackageFacts
from code_scout.harvest.raw_evidence import raw_evidence_units_from_active_facts
from code_scout.models.facts import (
    DEFAULT_EXTRACTOR_CONFIDENCE,
    ActiveFact,
    EntityFact,
    ExtractorMetadata,
    FactEvidence,
    FactReference,
    RelationFact,
    concept_to_entity_facts,
    contract_to_entity_facts,
    decision_to_entity_facts,
    deterministic_entity_fact_id,
    deterministic_relation_fact_id,
    edge_to_relation_facts,
    entity_fact_to_active_fact,
    file_to_entity_facts,
    relation_fact_to_active_fact,
    repository_package_facts_to_entity_facts,
    repository_to_entity_facts,
    service_to_entity_facts,
    symbol_to_entity_facts,
)
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


def test_fact_models_are_typed_dataclasses() -> None:
    assert is_dataclass(FactReference)
    assert is_dataclass(FactEvidence)
    assert is_dataclass(ExtractorMetadata)
    assert is_dataclass(ActiveFact)
    assert is_dataclass(EntityFact)
    assert is_dataclass(RelationFact)


def test_extractor_confidence_defaults_cover_source_extractors() -> None:
    assert set(DEFAULT_EXTRACTOR_CONFIDENCE) >= {
        "openapi",
        "asyncapi",
        "graphify",
        "lsp",
        "tree_sitter",
        "adr",
        "docs",
        "infra",
        "package_metadata",
    }


def test_valid_entity_fact_serializes_identity_properties_and_evidence() -> None:
    evidence = FactEvidence(
        file_path="apps/api/src/orders.ts",
        extractor_name="graphify",
        confidence=0.97,
        start_line=12,
        end_line=24,
        metadata={"symbol": "OrderService"},
    )
    fact = EntityFact(
        entity_type="service",
        entity_id="service:orders",
        properties={
            "name": "Order Service",
            "repository_id": "repo:api",
            "language": "typescript",
        },
        evidence=(evidence,),
        confidence=0.95,
        extractor_metadata=ExtractorMetadata(
            name="graphify",
            version="1.2.0",
            metadata={"run_id": "scan-1"},
        ),
    )

    assert fact.reference == FactReference(type="service", id="service:orders")
    assert fact.to_dict() == {
        "entity_type": "service",
        "entity_id": "service:orders",
        "properties": {
            "name": "Order Service",
            "repository_id": "repo:api",
            "language": "typescript",
        },
        "evidence": (
            {
                "file_path": "apps/api/src/orders.ts",
                "extractor_name": "graphify",
                "confidence": 0.97,
                "start_line": 12,
                "end_line": 24,
                "metadata": {"symbol": "OrderService"},
            },
        ),
        "confidence": 0.95,
        "extractor_metadata": {
            "name": "graphify",
            "version": "1.2.0",
            "metadata": {"run_id": "scan-1"},
        },
    }


def test_valid_relation_fact_serializes_typed_endpoints_and_relation() -> None:
    evidence = FactEvidence(
        file_path="apps/web/src/order-client.ts",
        extractor_name="frontend_api",
        confidence=0.86,
        start_line=33,
        metadata={"method": "GET", "path": "/orders/{id}"},
    )
    fact = RelationFact(
        subject=FactReference(type="service", id="service:web"),
        relation="calls",
        object=FactReference(type="contract", id="contract:get-orders-id"),
        properties={
            "protocol": "http",
            "method": "GET",
            "path": "/orders/{id}",
        },
        evidence=(evidence,),
        confidence=0.84,
        extractor_metadata=ExtractorMetadata(
            name="frontend_api",
            metadata={"source": "fetch"},
        ),
    )

    assert fact.to_dict() == {
        "subject": {
            "type": "service",
            "id": "service:web",
        },
        "relation": "calls",
        "object": {
            "type": "contract",
            "id": "contract:get-orders-id",
        },
        "properties": {
            "protocol": "http",
            "method": "GET",
            "path": "/orders/{id}",
        },
        "evidence": (
            {
                "file_path": "apps/web/src/order-client.ts",
                "extractor_name": "frontend_api",
                "confidence": 0.86,
                "start_line": 33,
                "end_line": None,
                "metadata": {"method": "GET", "path": "/orders/{id}"},
            },
        ),
        "confidence": 0.84,
        "extractor_metadata": {
            "name": "frontend_api",
            "version": None,
            "metadata": {"source": "fetch"},
        },
    }


def test_repository_to_entity_facts_preserves_repository_fields() -> None:
    repository = Repository(
        id="workspace",
        name="Workspace",
        path=".",
        remote_url="https://example.test/workspace.git",
        default_branch="main",
        language_hints=("TypeScript",),
        framework_hints=("Next.js",),
        module_hints=("frontend",),
        evidence=(
            Evidence(
                file_path="code_scout.toml",
                extractor_name="workspace",
                confidence=1.0,
                metadata={"source": "config"},
            ),
        ),
    )

    facts = repository_to_entity_facts(repository)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "repository",
        "entity_id": "workspace",
        "properties": {
            "name": "Workspace",
            "path": ".",
            "remote_url": "https://example.test/workspace.git",
            "default_branch": "main",
            "language_hints": ("TypeScript",),
            "framework_hints": ("Next.js",),
            "module_hints": ("frontend",),
        },
        "evidence": (
            {
                "file_path": "code_scout.toml",
                "extractor_name": "workspace",
                "confidence": 1.0,
                "start_line": None,
                "end_line": None,
                "metadata": {"source": "config"},
            },
        ),
        "confidence": 1.0,
        "extractor_metadata": {
            "name": "workspace",
            "version": None,
            "metadata": {},
        },
    }


def test_file_to_entity_facts_preserves_file_fields() -> None:
    file = File(
        id="file:workspace:apps/api/src/users.ts",
        repository_id="workspace",
        path="apps/api/src/users.ts",
        kind="source",
        extension=".ts",
        language="TypeScript",
        content_hash="abc123",
        modified_at="2026-06-20T10:00:00Z",
        evidence=(
            Evidence(
                file_path="apps/api/src/users.ts",
                extractor_name="discovery",
                confidence=1.0,
                metadata={"absolute_path": "/workspace/apps/api/src/users.ts"},
            ),
        ),
    )

    facts = file_to_entity_facts(file)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "file",
        "entity_id": "file:workspace:apps/api/src/users.ts",
        "properties": {
            "repository_id": "workspace",
            "path": "apps/api/src/users.ts",
            "kind": "source",
            "extension": ".ts",
            "language": "TypeScript",
            "content_hash": "abc123",
            "modified_at": "2026-06-20T10:00:00Z",
        },
        "evidence": (
            {
                "file_path": "apps/api/src/users.ts",
                "extractor_name": "discovery",
                "confidence": 1.0,
                "start_line": None,
                "end_line": None,
                "metadata": {"absolute_path": "/workspace/apps/api/src/users.ts"},
            },
        ),
        "confidence": 1.0,
        "extractor_metadata": {
            "name": "discovery",
            "version": None,
            "metadata": {},
        },
    }


def test_symbol_to_entity_facts_preserves_symbol_fields_and_evidence_metadata() -> None:
    symbol = Symbol(
        id="symbol:workspace:apps/api/src/users.ts:UsersService",
        file_id="file:workspace:apps/api/src/users.ts",
        name="UsersService",
        kind="class",
        qualified_name="api.users.UsersService",
        start_line=12,
        end_line=58,
        evidence=(
            Evidence(
                file_path="apps/api/src/users.ts",
                extractor_name="tree_sitter",
                confidence=0.94,
                start_line=12,
                end_line=58,
                metadata={
                    "repository_id": "workspace",
                    "node_type": "class_declaration",
                },
            ),
        ),
    )

    facts = symbol_to_entity_facts(symbol)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "symbol",
        "entity_id": "symbol:workspace:apps/api/src/users.ts:UsersService",
        "properties": {
            "file_id": "file:workspace:apps/api/src/users.ts",
            "name": "UsersService",
            "kind": "class",
            "qualified_name": "api.users.UsersService",
            "start_line": 12,
            "end_line": 58,
        },
        "evidence": (
            {
                "file_path": "apps/api/src/users.ts",
                "extractor_name": "tree_sitter",
                "confidence": 0.94,
                "start_line": 12,
                "end_line": 58,
                "metadata": {
                    "repository_id": "workspace",
                    "node_type": "class_declaration",
                },
            },
        ),
        "confidence": 0.94,
        "extractor_metadata": {
            "name": "tree_sitter",
            "version": None,
            "metadata": {},
        },
    }


def test_repository_package_facts_to_entity_facts_preserves_package_metadata() -> None:
    package_facts = RepositoryPackageFacts(
        repository_id="workspace",
        language_hints=("TypeScript",),
        framework_hints=("Next.js", "turbo"),
        package_files_analyzed=2,
        evidence=(
            Evidence(
                file_path="package.json",
                extractor_name="package_json",
                confidence=1.0,
                start_line=1,
                end_line=20,
                metadata={
                    "name": "@acme/web",
                    "dependencies": {"next": "^15.0.0"},
                    "dev_dependencies": {"typescript": "^5.8.0"},
                },
            ),
        ),
    )

    facts = repository_package_facts_to_entity_facts(package_facts)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "package_metadata",
        "entity_id": "package-metadata:workspace",
        "properties": {
            "repository_id": "workspace",
            "language_hints": ("TypeScript",),
            "framework_hints": ("Next.js", "turbo"),
            "package_files_analyzed": 2,
        },
        "evidence": (
            {
                "file_path": "package.json",
                "extractor_name": "package_json",
                "confidence": 1.0,
                "start_line": 1,
                "end_line": 20,
                "metadata": {
                    "name": "@acme/web",
                    "dependencies": {"next": "^15.0.0"},
                    "dev_dependencies": {"typescript": "^5.8.0"},
                },
            },
        ),
        "confidence": 1.0,
        "extractor_metadata": {
            "name": "package_json",
            "version": None,
            "metadata": {},
        },
    }


def test_service_to_entity_facts_preserves_service_fields_and_evidence() -> None:
    service = Service(
        id="service:orders",
        name="Orders API",
        repository_id="repo:api",
        description="Handles order workflows",
        evidence=(
            Evidence(
                file_path="apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.93,
                start_line=10,
                end_line=42,
                metadata={"symbol": "OrdersModule"},
            ),
        ),
    )

    facts = service_to_entity_facts(service)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "service",
        "entity_id": "service:orders",
        "properties": {
            "name": "Orders API",
            "repository_id": "repo:api",
            "description": "Handles order workflows",
            "aliases": (),
        },
        "evidence": (
            {
                "file_path": "apps/api/src/orders.ts",
                "extractor_name": "graphify",
                "confidence": 0.93,
                "start_line": 10,
                "end_line": 42,
                "metadata": {"symbol": "OrdersModule"},
            },
        ),
        "confidence": 0.93,
        "extractor_metadata": {
            "name": "graphify",
            "version": None,
            "metadata": {},
        },
    }


def test_service_to_entity_facts_uses_default_confidence_when_evidence_omits_it() -> (
    None
):
    service = Service(
        id="service:orders",
        name="Orders API",
        evidence=(
            Evidence(
                file_path="graphify/services.json",
                extractor_name="graphify",
                metadata={"symbol": "OrdersService"},
            ),
        ),
    )

    facts = service_to_entity_facts(service)

    assert facts[0].confidence == DEFAULT_EXTRACTOR_CONFIDENCE["graphify"]
    assert facts[0].evidence[0].confidence == DEFAULT_EXTRACTOR_CONFIDENCE["graphify"]


def test_contract_to_entity_facts_preserves_explicit_source_confidence() -> None:
    contract = Contract(
        id="contract:workspace:contracts/openapi.yaml:get:orders",
        name="listOrders",
        kind="http",
        path="/orders",
        method="GET",
        evidence=(
            Evidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=0.72,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )

    facts = contract_to_entity_facts(contract)

    assert facts[0].confidence == 0.72
    assert facts[0].evidence[0].confidence == 0.72


def test_service_to_entity_facts_emits_one_fact_per_evidence_source() -> None:
    service = Service(
        id="service:payments",
        name="Payments API",
        repository_id="repo:billing",
        description="Processes payments",
        evidence=(
            Evidence(
                file_path="backend/src/Payments.Api/Payments.Api.csproj",
                extractor_name="dotnet",
                confidence=0.91,
                start_line=1,
                end_line=1,
                metadata={"project_type": "aspnet_core_web"},
            ),
            Evidence(
                file_path="graphify/services.json",
                extractor_name="graphify",
                confidence=0.87,
                metadata={"source": "workspace scan"},
            ),
        ),
    )

    facts = service_to_entity_facts(service)

    assert len(facts) == 2
    assert [fact.entity_id for fact in facts] == [
        "service:payments",
        "service:payments",
    ]
    assert [fact.properties for fact in facts] == [
        {
            "name": "Payments API",
            "repository_id": "repo:billing",
            "description": "Processes payments",
            "aliases": (),
        },
        {
            "name": "Payments API",
            "repository_id": "repo:billing",
            "description": "Processes payments",
            "aliases": (),
        },
    ]
    assert [fact.confidence for fact in facts] == [0.91, 0.87]
    assert [fact.extractor_metadata.name for fact in facts] == ["dotnet", "graphify"]
    assert [fact.evidence[0].file_path for fact in facts] == [
        "backend/src/Payments.Api/Payments.Api.csproj",
        "graphify/services.json",
    ]
    assert [fact.evidence[0].metadata for fact in facts] == [
        {"project_type": "aspnet_core_web"},
        {"source": "workspace scan"},
    ]


def test_contract_to_entity_facts_preserves_openapi_contract_fields() -> None:
    contract = Contract(
        id="contract:workspace:contracts/openapi.yaml:post:payments",
        name="createPayment",
        kind="http",
        path="/payments",
        version="2026-06-20",
        method="POST",
        producer_hint="Payments API",
        operation_id="createPayment",
        request_dtos=("CreatePaymentRequest",),
        response_dtos=("PaymentResponse",),
        evidence=(
            Evidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
                start_line=1,
                end_line=40,
                metadata={
                    "repository_id": "workspace",
                    "title": "Payments API",
                    "operation_id": "createPayment",
                },
            ),
        ),
    )

    facts = contract_to_entity_facts(contract)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "contract",
        "entity_id": "contract:workspace:http:post:payments",
        "properties": {
            "name": "createPayment",
            "kind": "http",
            "path": "/payments",
            "version": "2026-06-20",
            "method": "POST",
            "producer_hint": "Payments API",
            "operation_id": "createPayment",
            "request_dtos": ("CreatePaymentRequest",),
            "response_dtos": ("PaymentResponse",),
        },
        "evidence": (
            {
                "file_path": "contracts/openapi.yaml",
                "extractor_name": "openapi",
                "confidence": 1.0,
                "start_line": 1,
                "end_line": 40,
                "metadata": {
                    "repository_id": "workspace",
                    "title": "Payments API",
                    "operation_id": "createPayment",
                },
            },
        ),
        "confidence": 1.0,
        "extractor_metadata": {
            "name": "openapi",
            "version": None,
            "metadata": {},
        },
    }


def test_contract_to_entity_facts_normalizes_inferred_frontend_contract_identity() -> (
    None
):
    contract = Contract(
        id="contract:workspace:frontend_api:get:api-users",
        name="GET /api/users",
        kind="http",
        path="/api/users",
        method="GET",
        evidence=(
            Evidence(
                file_path="apps/web/src/users.ts",
                extractor_name="frontend_api",
                confidence=0.82,
                start_line=12,
                end_line=12,
                metadata={
                    "repository_id": "workspace",
                    "call_kind": "fetch",
                    "method": "GET",
                    "route": "/api/users",
                },
            ),
        ),
    )

    facts = contract_to_entity_facts(contract)

    assert len(facts) == 1
    assert facts[0].entity_id == "contract:workspace:http:get:api-users"
    assert facts[0].properties == {
        "name": "GET /api/users",
        "kind": "http",
        "path": "/api/users",
        "version": None,
        "method": "GET",
        "producer_hint": None,
        "operation_id": None,
        "request_dtos": (),
        "response_dtos": (),
    }
    assert facts[0].confidence == 0.82
    assert facts[0].extractor_metadata.name == "frontend_api"
    assert facts[0].evidence[0] == FactEvidence(
        file_path="apps/web/src/users.ts",
        extractor_name="frontend_api",
        confidence=0.82,
        start_line=12,
        end_line=12,
        metadata={
            "repository_id": "workspace",
            "call_kind": "fetch",
            "method": "GET",
            "route": "/api/users",
        },
    )


def test_concept_to_entity_facts_preserves_documentation_concept_fields() -> None:
    concept = Concept(
        id="concept:workspace:architecture-memory",
        name="Architecture Memory",
        description="Documentation concept for reusable architecture knowledge.",
        evidence=(
            Evidence(
                file_path="docs/concepts.md",
                extractor_name="docs",
                confidence=0.88,
                start_line=14,
                end_line=18,
                metadata={"heading": "Architecture Memory"},
            ),
        ),
    )

    facts = concept_to_entity_facts(concept)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "concept",
        "entity_id": "concept:workspace:architecture-memory",
        "properties": {
            "name": "Architecture Memory",
            "description": "Documentation concept for reusable architecture knowledge.",
        },
        "evidence": (
            {
                "file_path": "docs/concepts.md",
                "extractor_name": "docs",
                "confidence": 0.88,
                "start_line": 14,
                "end_line": 18,
                "metadata": {"heading": "Architecture Memory"},
            },
        ),
        "confidence": 0.88,
        "extractor_metadata": {
            "name": "docs",
            "version": None,
            "metadata": {},
        },
    }


def test_decision_to_entity_facts_preserves_adr_decision_fields() -> None:
    decision = Decision(
        id="decision:workspace:docs/adr/0001-use-sqlite.md",
        adr_id="0001",
        title="Use SQLite",
        status="accepted",
        path="docs/adr/0001-use-sqlite.md",
        decided_at="2026-06-20",
        context="code-scout needs portable local storage.",
        decision="Use SQLite through the stdlib sqlite3 module.",
        consequences="The database remains easy to inspect and migrate.",
        evidence=(
            Evidence(
                file_path="docs/adr/0001-use-sqlite.md",
                extractor_name="adr",
                confidence=0.96,
                start_line=1,
                end_line=26,
                metadata={"repository_id": "workspace", "field": "decision"},
            ),
        ),
    )

    facts = decision_to_entity_facts(decision)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "entity_type": "decision",
        "entity_id": "decision:workspace:docs/adr/0001-use-sqlite.md",
        "properties": {
            "adr_id": "0001",
            "title": "Use SQLite",
            "status": "accepted",
            "path": "docs/adr/0001-use-sqlite.md",
            "decided_at": "2026-06-20",
            "context": "code-scout needs portable local storage.",
            "decision": "Use SQLite through the stdlib sqlite3 module.",
            "consequences": "The database remains easy to inspect and migrate.",
        },
        "evidence": (
            {
                "file_path": "docs/adr/0001-use-sqlite.md",
                "extractor_name": "adr",
                "confidence": 0.96,
                "start_line": 1,
                "end_line": 26,
                "metadata": {"repository_id": "workspace", "field": "decision"},
            },
        ),
        "confidence": 0.96,
        "extractor_metadata": {
            "name": "adr",
            "version": None,
            "metadata": {},
        },
    }


def test_edge_to_relation_facts_preserves_file_declares_symbol_relation() -> None:
    edge = Edge(
        source_type="file",
        source_id="file:workspace:src/app.py",
        target_type="symbol",
        target_id="symbol:workspace:src/app.py:App",
        relation_type="declares_symbol",
        confidence=0.89,
        evidence=(
            Evidence(
                file_path="src/app.py",
                extractor_name="tree_sitter",
                confidence=0.94,
                start_line=3,
                end_line=18,
                metadata={"node_type": "class_definition"},
            ),
        ),
    )

    facts = edge_to_relation_facts(edge)

    assert len(facts) == 1
    assert facts[0].to_dict() == {
        "subject": {
            "type": "file",
            "id": "file:workspace:src/app.py",
        },
        "relation": "declares_symbol",
        "object": {
            "type": "symbol",
            "id": "symbol:workspace:src/app.py:App",
        },
        "properties": {},
        "evidence": (
            {
                "file_path": "src/app.py",
                "extractor_name": "tree_sitter",
                "confidence": 0.94,
                "start_line": 3,
                "end_line": 18,
                "metadata": {"node_type": "class_definition"},
            },
        ),
        "confidence": 0.89,
        "extractor_metadata": {
            "name": "tree_sitter",
            "version": None,
            "metadata": {},
        },
    }


def test_edge_to_relation_facts_preserves_service_exposes_contract_relation() -> None:
    edge = Edge(
        source_type="service",
        source_id="service:orders",
        target_type="contract",
        target_id="contract:workspace:http:get:orders",
        relation_type="exposes",
        confidence=0.91,
        evidence=(
            Evidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
                start_line=12,
                end_line=34,
                metadata={"operation_id": "listOrders"},
            ),
        ),
    )

    facts = edge_to_relation_facts(edge)

    assert len(facts) == 1
    assert facts[0].subject == FactReference(type="service", id="service:orders")
    assert facts[0].relation == "exposes"
    assert facts[0].object == FactReference(
        type="contract",
        id="contract:workspace:http:get:orders",
    )
    assert facts[0].confidence == 0.91
    assert facts[0].evidence[0] == FactEvidence(
        file_path="contracts/openapi.yaml",
        extractor_name="openapi",
        confidence=1.0,
        start_line=12,
        end_line=34,
        metadata={"operation_id": "listOrders"},
    )
    assert facts[0].extractor_metadata.name == "openapi"


def test_edge_to_relation_facts_preserves_decision_affects_concept_relation() -> None:
    edge = Edge(
        source_type="decision",
        source_id="decision:workspace:docs/adr/0007-use-fastcontext.md",
        target_type="concept",
        target_id="concept:workspace:context-assembly",
        relation_type="affects",
        confidence=0.86,
        evidence=(
            Evidence(
                file_path="docs/adr/0007-use-fastcontext.md",
                extractor_name="adr",
                confidence=0.92,
                start_line=20,
                end_line=24,
                metadata={"section": "Consequences"},
            ),
        ),
    )

    facts = edge_to_relation_facts(edge)

    assert len(facts) == 1
    assert facts[0].subject == FactReference(
        type="decision",
        id="decision:workspace:docs/adr/0007-use-fastcontext.md",
    )
    assert facts[0].relation == "affects"
    assert facts[0].object == FactReference(
        type="concept",
        id="concept:workspace:context-assembly",
    )
    assert facts[0].confidence == 0.86
    assert facts[0].evidence[0].metadata == {"section": "Consequences"}
    assert facts[0].extractor_metadata.name == "adr"


def test_edge_to_relation_facts_supports_deterministic_ids_for_equivalent_edges() -> (
    None
):
    edge = Edge(
        source_type="service",
        source_id="service:orders",
        target_type="contract",
        target_id="contract:workspace:http:get:orders",
        relation_type="exposes",
        confidence=0.91,
        evidence=(
            Evidence(
                file_path="./contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
            ),
        ),
    )
    equivalent_edge = Edge(
        source_type="service",
        source_id="service:orders",
        target_type="contract",
        target_id="contract:workspace:http:get:orders",
        relation_type="exposes",
        confidence=0.91,
        evidence=(
            Evidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
            ),
        ),
    )

    fact = edge_to_relation_facts(edge)[0]
    equivalent_fact = edge_to_relation_facts(equivalent_edge)[0]

    fact_id = deterministic_relation_fact_id(
        extractor_name=fact.extractor_metadata.name,
        source_file=fact.evidence[0].file_path,
        relation=fact.relation,
        subject=fact.subject,
        object=fact.object,
    )
    equivalent_fact_id = deterministic_relation_fact_id(
        extractor_name=equivalent_fact.extractor_metadata.name,
        source_file=equivalent_fact.evidence[0].file_path,
        relation=equivalent_fact.relation,
        subject=equivalent_fact.subject,
        object=equivalent_fact.object,
    )

    assert fact_id == equivalent_fact_id
    assert fact_id.startswith(
        "fact:relation:openapi:contracts-openapi-yaml:"
        "exposes:service-service-orders-exposes-contract-"
        "contract-workspace-http-get-orders:",
    )


def test_deterministic_entity_fact_id_includes_source_extractor_kind_and_identity() -> (
    None
):
    fact_id = deterministic_entity_fact_id(
        extractor_name="graphify",
        source_file="apps/api/src/orders.ts",
        entity_type="service",
        entity_id="service:orders",
    )

    assert fact_id.startswith(
        "fact:entity:graphify:apps-api-src-orders-ts:service:service-orders:",
    )
    assert fact_id == deterministic_entity_fact_id(
        extractor_name="graphify",
        source_file="apps/api/src/orders.ts",
        entity_type="service",
        entity_id="service:orders",
    )


def test_deterministic_relation_fact_id_includes_relation_identity() -> None:
    subject = FactReference(type="service", id="service:web")
    object = FactReference(type="contract", id="contract:get-orders-id")

    fact_id = deterministic_relation_fact_id(
        extractor_name="frontend_api",
        source_file="./apps/web/src/order-client.ts",
        relation="calls",
        subject=subject,
        object=object,
    )

    assert fact_id.startswith(
        "fact:relation:frontend-api:apps-web-src-order-client-ts:"
        "calls:service-service-web-calls-contract-contract-get-orders-id:",
    )
    assert fact_id == deterministic_relation_fact_id(
        extractor_name="frontend_api",
        source_file="apps/web/src/order-client.ts",
        relation="calls",
        subject=subject,
        object=object,
    )


def test_deterministic_relation_fact_id_changes_with_relation_identity() -> None:
    subject = FactReference(type="service", id="service:web")

    list_orders_id = deterministic_relation_fact_id(
        extractor_name="frontend_api",
        source_file="apps/web/src/order-client.ts",
        relation="calls",
        subject=subject,
        object=FactReference(type="contract", id="contract:list-orders"),
    )
    get_order_id = deterministic_relation_fact_id(
        extractor_name="frontend_api",
        source_file="apps/web/src/order-client.ts",
        relation="calls",
        subject=subject,
        object=FactReference(type="contract", id="contract:get-order"),
    )
    describes_id = deterministic_relation_fact_id(
        extractor_name="frontend_api",
        source_file="apps/web/src/order-client.ts",
        relation="describes",
        subject=subject,
        object=FactReference(type="contract", id="contract:list-orders"),
    )

    assert list_orders_id != get_order_id
    assert list_orders_id != describes_id


def test_entity_fact_to_active_fact_uses_deterministic_storage_shape() -> None:
    entity_fact = EntityFact(
        entity_type="service",
        entity_id="service:orders",
        properties={"name": "Orders", "aliases": ("OrdersService",)},
        evidence=(
            FactEvidence(
                file_path="./apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.91,
                metadata={"repository_id": "workspace"},
            ),
        ),
        confidence=0.91,
        extractor_metadata=ExtractorMetadata(name="graphify", version="1.2.0"),
    )

    active_fact = entity_fact_to_active_fact(entity_fact)

    assert active_fact.id == deterministic_entity_fact_id(
        extractor_name="graphify",
        source_file="apps/api/src/orders.ts",
        entity_type="service",
        entity_id="service:orders",
    )
    assert active_fact.extractor == "graphify"
    assert active_fact.kind == "service"
    assert active_fact.source_file == "apps/api/src/orders.ts"
    assert active_fact.identity == {
        "entity_type": "service",
        "entity_id": "service:orders",
    }
    assert active_fact.properties == {
        "name": "Orders",
        "aliases": ("OrdersService",),
    }
    assert active_fact.evidence == entity_fact.evidence
    assert active_fact.extractor_version == "1.2.0"


def test_relation_fact_to_active_fact_uses_typed_relation_identity() -> None:
    relation_fact = RelationFact(
        subject=FactReference(type="service", id="service:orders"),
        relation="exposes",
        object=FactReference(type="contract", id="contract:orders"),
        properties={},
        evidence=(
            FactEvidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
            ),
        ),
        confidence=0.95,
        extractor_metadata=ExtractorMetadata(name="openapi"),
    )

    active_fact = relation_fact_to_active_fact(relation_fact)

    assert active_fact.id == deterministic_relation_fact_id(
        extractor_name="openapi",
        source_file="contracts/openapi.yaml",
        relation="exposes",
        subject=relation_fact.subject,
        object=relation_fact.object,
    )
    assert active_fact.kind == "exposes"
    assert active_fact.identity == {
        "subject": {"type": "service", "id": "service:orders"},
        "relation": "exposes",
        "object": {"type": "contract", "id": "contract:orders"},
    }
    assert active_fact.confidence == 0.95


def test_raw_evidence_units_from_active_facts_attach_entity_owners() -> None:
    active_fact = ActiveFact(
        id="fact:entity:graphify:orders",
        extractor="graphify",
        kind="service",
        source_file="apps/api/src/orders.ts",
        identity={
            "entity_type": "service",
            "entity_id": "service:orders",
        },
        properties={
            "name": "Orders API",
            "repository_id": "workspace",
            "description": "Handles order workflows",
        },
        evidence=(
            FactEvidence(
                file_path="./apps/api/src/orders.ts",
                extractor_name="graphify",
                confidence=0.93,
                start_line=10,
                end_line=42,
                metadata={"symbol": "OrdersModule"},
            ),
        ),
        confidence=0.93,
    )

    units = raw_evidence_units_from_active_facts((active_fact,))

    assert len(units) == 1
    unit = units[0]
    assert unit.owner_entity_type == "service"
    assert unit.owner_entity_id == "service:orders"
    assert unit.source_file == "apps/api/src/orders.ts"
    assert unit.start_line == 10
    assert unit.end_line == 42
    assert unit.provenance == "graphify"
    assert unit.confidence == 0.93
    assert unit.metadata == {
        "fact_id": "fact:entity:graphify:orders",
        "fact_kind": "service",
        "fact_extractor": "graphify",
        "fact_source_file": "apps/api/src/orders.ts",
        "evidence_metadata": {"symbol": "OrdersModule"},
    }
    assert json.loads(unit.text) == {
        "kind": "service",
        "identity": {
            "entity_type": "service",
            "entity_id": "service:orders",
        },
        "properties": {
            "name": "Orders API",
            "repository_id": "workspace",
            "description": "Handles order workflows",
        },
        "evidence": {
            "file_path": "./apps/api/src/orders.ts",
            "extractor_name": "graphify",
            "confidence": 0.93,
            "start_line": 10,
            "end_line": 42,
            "metadata": {"symbol": "OrdersModule"},
        },
    }
    assert "summary" not in unit.text.lower()


def test_raw_evidence_units_from_active_facts_attach_relationship_owners() -> None:
    active_fact = ActiveFact(
        id="fact:relation:openapi:orders",
        extractor="openapi",
        kind="exposes",
        source_file="contracts/openapi.yaml",
        identity={
            "subject": {"type": "service", "id": "service:orders"},
            "relation": "exposes",
            "object": {
                "type": "contract",
                "id": "contract:workspace:http:get:orders",
            },
        },
        properties={},
        evidence=(
            FactEvidence(
                file_path="contracts/openapi.yaml",
                extractor_name="openapi",
                confidence=1.0,
                start_line=12,
                end_line=34,
                metadata={"operation_id": "listOrders"},
            ),
        ),
        confidence=0.95,
    )

    units = raw_evidence_units_from_active_facts((active_fact,))

    assert len(units) == 1
    unit = units[0]
    assert unit.owner_entity_type == "relationship"
    assert unit.owner_entity_id == (
        "service:service:orders:exposes:contract:contract:workspace:http:get:orders"
    )
    assert unit.source_file == "contracts/openapi.yaml"
    assert unit.provenance == "openapi"
    assert unit.metadata["fact_kind"] == "exposes"
    assert json.loads(unit.text)["identity"] == active_fact.identity
