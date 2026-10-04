from __future__ import annotations

import json
import sqlite3

import pytest

from code_scout.evidence.query import (
    DEFAULT_SEMANTIC_QUERY_LIMIT,
    SEMANTIC_MATCH_REASON,
    SemanticEvidenceMatch,
    search_semantic_evidence,
    semantic_evidence_search,
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
from code_scout.storage import (
    RawEvidenceUnit,
    evidence_text_hash,
    open_storage,
    upsert_concept,
    upsert_contract,
    upsert_decision,
    upsert_edge,
    upsert_file,
    upsert_raw_evidence_units,
    upsert_repository,
    upsert_service,
    upsert_symbol,
    upsert_workspace,
)


class _SeededEmbeddingProvider:
    model_id = "test-semantic-provider"
    dimensions = 3

    def __init__(self, question_vectors: dict[str, tuple[float, ...]] | None = None):
        self.question_vectors = question_vectors or {
            "checkout payment workflow": (1.0, 0.0, 0.0),
            "tied query": (0.0, 1.0, 0.0),
            "BillingService": (0.0, 0.0, 1.0),
            "billing": (0.0, 0.0, 1.0),
            "/billing/checkout": (0.0, 0.0, 1.0),
            "Use hosted checkout for billing": (0.0, 0.0, 1.0),
        }
        self.texts: list[str] = []

    def embed_text(self, text: str) -> tuple[float, ...]:
        self.texts.append(text)
        return self.question_vectors[text]


def test_semantic_evidence_search_returns_ranked_persisted_matches(tmp_path) -> None:
    provider = _SeededEmbeddingProvider()
    payments = _raw_evidence_unit(
        "evidence-unit:semantic:payments",
        owner_entity_type="service",
        owner_entity_id="service:payments",
        source_file="apps/api/src/payments.ts",
        start_line=8,
        end_line=18,
        text="PaymentsService owns checkout payment workflows.",
    )
    checkout = _raw_evidence_unit(
        "evidence-unit:semantic:checkout",
        owner_entity_type="contract",
        owner_entity_id="contract:post-checkout",
        source_file="openapi.yaml",
        start_line=40,
        end_line=52,
        text="POST /checkout creates a checkout session.",
    )
    unrelated = _raw_evidence_unit(
        "evidence-unit:semantic:profiles",
        owner_entity_type="service",
        owner_entity_id="service:profiles",
        source_file="apps/api/src/profiles.ts",
        start_line=3,
        end_line=9,
        text="ProfilesService updates avatar settings.",
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, (unrelated, payments, checkout))
        _insert_embedding(connection, unrelated, vector=(0.0, 1.0, 0.0))
        _insert_embedding(connection, payments, vector=(1.0, 0.0, 0.0))
        _insert_embedding(connection, checkout, vector=(0.8, 0.6, 0.0))

        matches = search_semantic_evidence(
            connection,
            question="checkout payment workflow",
            provider=provider,
            limit=2,
        )

    assert provider.texts == ["checkout payment workflow"]
    assert matches == (
        SemanticEvidenceMatch(
            evidence_unit_id=payments.id,
            score=1.0,
            owner_entity_type="service",
            owner_entity_id="service:payments",
            source_file="apps/api/src/payments.ts",
            start_line=8,
            end_line=18,
            text="PaymentsService owns checkout payment workflows.",
            reason=SEMANTIC_MATCH_REASON,
            related_entity_ids=("service:payments",),
            related_services=("service:payments",),
        ),
        SemanticEvidenceMatch(
            evidence_unit_id=checkout.id,
            score=0.8,
            owner_entity_type="contract",
            owner_entity_id="contract:post-checkout",
            source_file="openapi.yaml",
            start_line=40,
            end_line=52,
            text="POST /checkout creates a checkout session.",
            reason=SEMANTIC_MATCH_REASON,
            related_entity_ids=("contract:post-checkout",),
            related_contracts=("contract:post-checkout",),
        ),
    )


def test_semantic_evidence_search_only_uses_persisted_units_and_embeddings(
    tmp_path,
) -> None:
    provider = _SeededEmbeddingProvider()
    persisted = _raw_evidence_unit(
        "evidence-unit:semantic:persisted",
        text="Persisted evidence with matching embedding.",
    )
    raw_without_embedding = _raw_evidence_unit(
        "evidence-unit:semantic:raw-only",
        text="Raw evidence without a persisted embedding.",
    )
    other_model = _raw_evidence_unit(
        "evidence-unit:semantic:other-model",
        text="Evidence embedded by a different model.",
    )

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(
            connection,
            (persisted, raw_without_embedding, other_model),
        )
        _insert_embedding(connection, persisted, vector=(1.0, 0.0, 0.0))
        _insert_embedding(
            connection,
            other_model,
            model_id="other-provider",
            vector=(1.0, 0.0, 0.0),
        )
        _insert_orphan_embedding(connection, "evidence-unit:semantic:orphan")

    matches = semantic_evidence_search(
        str(tmp_path),
        question="checkout payment workflow",
        provider=provider,
    )

    assert [match.evidence_unit_id for match in matches] == [persisted.id]


def test_semantic_evidence_search_uses_default_limit(tmp_path) -> None:
    provider = _SeededEmbeddingProvider()

    with open_storage(tmp_path) as connection:
        units = tuple(
            _raw_evidence_unit(f"evidence-unit:semantic:{index:02d}")
            for index in range(DEFAULT_SEMANTIC_QUERY_LIMIT + 2)
        )
        upsert_raw_evidence_units(connection, units)
        for unit in units:
            _insert_embedding(connection, unit, vector=(1.0, 0.0, 0.0))

        matches = search_semantic_evidence(
            connection,
            question="checkout payment workflow",
            provider=provider,
        )

    assert len(matches) == DEFAULT_SEMANTIC_QUERY_LIMIT


def test_semantic_evidence_search_breaks_exact_score_ties_by_evidence_id(
    tmp_path,
) -> None:
    provider = _SeededEmbeddingProvider()
    later = _raw_evidence_unit("evidence-unit:semantic:z-later")
    earlier = _raw_evidence_unit("evidence-unit:semantic:a-earlier")

    with open_storage(tmp_path) as connection:
        upsert_raw_evidence_units(connection, (later, earlier))
        _insert_embedding(connection, later, vector=(0.0, 1.0, 0.0))
        _insert_embedding(connection, earlier, vector=(0.0, 1.0, 0.0))

        first_run = search_semantic_evidence(
            connection,
            question="tied query",
            provider=provider,
        )
        second_run = search_semantic_evidence(
            connection,
            question="tied query",
            provider=provider,
        )

    assert [match.evidence_unit_id for match in first_run] == [
        earlier.id,
        later.id,
    ]
    assert first_run == second_run


def test_semantic_evidence_search_rejects_non_positive_limit(tmp_path) -> None:
    with open_storage(tmp_path) as connection:
        with pytest.raises(ValueError, match="limit"):
            search_semantic_evidence(
                connection,
                question="checkout payment workflow",
                limit=0,
                provider=_SeededEmbeddingProvider(),
            )


@pytest.mark.parametrize(
    ("query", "owner_entity_type", "owner_entity_id", "reason"),
    [
        ("BillingService", "service", "service:billing", "matched service name"),
        ("billing", "service", "service:billing", "matched alias"),
        (
            "/billing/checkout",
            "contract",
            "contract:billing-checkout",
            "matched contract path",
        ),
        (
            "Use hosted checkout for billing",
            "decision",
            "decision:billing-checkout",
            "matched decision title",
        ),
    ],
)
def test_semantic_evidence_search_scores_lexical_graph_metadata(
    tmp_path,
    query: str,
    owner_entity_type: str,
    owner_entity_id: str,
    reason: str,
) -> None:
    provider = _SeededEmbeddingProvider()
    weak_vector_match = _raw_evidence_unit(
        "evidence-unit:semantic:weak-billing",
        owner_entity_type="service",
        owner_entity_id="service:billing",
        source_file="apps/api/src/billing.service.ts",
        text="Unhelpful embedded evidence for the billing service.",
    )

    with open_storage(tmp_path) as connection:
        _seed_lexical_graph(connection)
        upsert_raw_evidence_units(connection, (weak_vector_match,))
        _insert_embedding(connection, weak_vector_match, vector=(0.1, 0.0, 0.0))

        matches = search_semantic_evidence(
            connection,
            question=query,
            provider=provider,
            limit=20,
        )

    assert provider.texts == [query]
    lexical_match = next(
        match
        for match in matches
        if match.owner_entity_type == owner_entity_type
        and match.owner_entity_id == owner_entity_id
        and reason in match.scoring_reasons
    )
    assert lexical_match.score > 1.0
    assert any(SEMANTIC_MATCH_REASON in match.scoring_reasons for match in matches)


def test_semantic_evidence_search_matches_inflected_query_terms_to_source_paths(
    tmp_path,
) -> None:
    question = "Locate the implementation that parses and dispatches CLI requests"
    provider = _SeededEmbeddingProvider({question: (0.0, 0.0, 1.0)})

    with open_storage(tmp_path) as connection:
        upsert_workspace(connection, id="workspace", name="Workspace", root_path=".")
        upsert_repository(
            connection,
            "workspace",
            repository=Repository(id="app", name="app", path="."),
        )
        for file_id, path in (
            ("file:parser", "src/code_scout/commands/parser.py"),
            ("file:dispatch", "src/code_scout/commands/dispatch.py"),
            ("file:cli-test", "tests/test_cli.py"),
        ):
            upsert_file(
                connection,
                File(
                    id=file_id,
                    repository_id="app",
                    path=path,
                    kind="source",
                    extension=".py",
                    language="python",
                ),
            )

        matches = search_semantic_evidence(
            connection,
            question=question,
            provider=provider,
            limit=3,
        )

    assert {match.owner_entity_id for match in matches[:2]} == {
        "file:parser",
        "file:dispatch",
    }


def test_semantic_evidence_search_merges_hybrid_scores_by_owner(tmp_path) -> None:
    provider = _SeededEmbeddingProvider()
    billing_evidence = _raw_evidence_unit(
        "evidence-unit:semantic:billing",
        owner_entity_type="service",
        owner_entity_id="service:billing",
        source_file="apps/api/src/billing.service.ts",
        text="Embedded evidence for BillingService.",
    )
    unrelated_evidence = _raw_evidence_unit(
        "evidence-unit:semantic:unrelated",
        owner_entity_type="service",
        owner_entity_id="service:unrelated",
        text="Unrelated semantic evidence.",
    )

    with open_storage(tmp_path) as connection:
        _seed_lexical_graph(connection)
        upsert_raw_evidence_units(connection, (billing_evidence, unrelated_evidence))
        _insert_embedding(connection, billing_evidence, vector=(0.0, 0.0, 0.8))
        _insert_embedding(connection, unrelated_evidence, vector=(1.0, 0.0, 0.0))

        matches = search_semantic_evidence(
            connection,
            question="BillingService",
            provider=provider,
            limit=20,
        )

    billing = next(
        match
        for match in matches
        if match.owner_entity_type == "service"
        and match.owner_entity_id == "service:billing"
    )
    assert [
        match.owner_entity_id
        for match in matches
        if match.owner_entity_id == "service:billing"
    ] == ["service:billing"]
    assert billing.evidence_unit_id == billing_evidence.id
    assert billing.score > 8.0
    assert billing.scoring_reasons == ("matched service name", "semantic match")
    assert billing.reason == "matched service name; semantic match"
    unrelated = next(
        match for match in matches if match.owner_entity_id == "service:unrelated"
    )
    assert billing.score > unrelated.score


def test_semantic_evidence_search_expands_only_direct_related_graph_context(
    tmp_path,
) -> None:
    provider = _SeededEmbeddingProvider()
    billing_evidence = _raw_evidence_unit(
        "evidence-unit:semantic:billing-context",
        owner_entity_type="service",
        owner_entity_id="service:billing",
        source_file="apps/api/src/billing.service.ts",
        text="Embedded evidence for the billing service.",
    )

    with open_storage(tmp_path) as connection:
        _seed_lexical_graph(connection)
        _seed_relationship_graph(connection)
        upsert_raw_evidence_units(connection, (billing_evidence,))
        _insert_embedding(connection, billing_evidence, vector=(0.2, 0.0, 0.0))

        first_run = search_semantic_evidence(
            connection,
            question="billing",
            provider=provider,
            limit=20,
        )
        second_run = search_semantic_evidence(
            connection,
            question="billing",
            provider=provider,
            limit=20,
        )

    billing = next(
        match
        for match in first_run
        if match.owner_entity_type == "service"
        and match.owner_entity_id == "service:billing"
    )
    assert billing.related_services == ("service:billing",)
    assert billing.related_contracts == ("contract:billing-checkout",)
    assert billing.related_concepts == ("concept:billing",)
    assert billing.related_decisions == ("decision:billing-checkout",)
    assert billing.related_symbols == ("symbol:billing-service",)
    assert billing.related_files == ("file:billing-service",)
    assert billing.relationships == (
        "relationship:1",
        "relationship:2",
        "relationship:3",
        "relationship:4",
        "relationship:5",
    )
    assert "service:unrelated" not in billing.related_entity_ids
    assert "contract:unrelated" not in billing.related_entity_ids
    assert first_run == second_run


def test_semantic_evidence_search_handles_large_owner_contexts(tmp_path) -> None:
    provider = _SeededEmbeddingProvider()
    unit_count = 1100

    with open_storage(tmp_path) as connection:
        units = tuple(
            _raw_evidence_unit(
                f"evidence-unit:semantic:large-context:{index:04d}",
                owner_entity_id=f"service:large-context:{index:04d}",
            )
            for index in range(unit_count)
        )
        upsert_raw_evidence_units(connection, units)
        for unit in units:
            _insert_embedding(connection, unit, vector=(1.0, 0.0, 0.0))

        matches = search_semantic_evidence(
            connection,
            question="checkout payment workflow",
            provider=provider,
            limit=5,
        )

    assert len(matches) == 5
    assert matches[0].related_services == ("service:large-context:0000",)


def _raw_evidence_unit(
    id: str,
    *,
    owner_entity_type: str = "service",
    owner_entity_id: str = "service:orders",
    source_file: str = "apps/api/src/orders.ts",
    start_line: int | None = 12,
    end_line: int | None = 18,
    text: str = "OrdersService handles order commands.",
) -> RawEvidenceUnit:
    return RawEvidenceUnit(
        id=id,
        owner_entity_type=owner_entity_type,
        owner_entity_id=owner_entity_id,
        source_file=source_file,
        start_line=start_line,
        end_line=end_line,
        text=text,
        provenance="test",
        confidence=0.9,
    )


def _seed_lexical_graph(connection: sqlite3.Connection) -> None:
    upsert_workspace(connection, id="workspace", name="Workspace", root_path=".")
    upsert_repository(
        connection,
        "workspace",
        repository=Repository(
            id="app",
            name="app",
            path=".",
        ),
    )
    upsert_file(
        connection,
        File(
            id="file:billing-service",
            repository_id="app",
            path="apps/api/src/billing.service.ts",
            kind="source",
            extension=".ts",
            language="typescript",
        ),
    )
    upsert_symbol(
        connection,
        Symbol(
            id="symbol:billing-service",
            file_id="file:billing-service",
            name="BillingService",
            kind="class",
            qualified_name="BillingService",
            start_line=5,
            end_line=28,
        ),
    )
    upsert_service(
        connection,
        Service(
            id="service:billing",
            name="BillingService",
            description="Owns checkout and invoice workflows.",
            aliases=("billing", "Billing domain"),
            evidence=(
                Evidence(
                    file_path="apps/api/src/billing.service.ts",
                    extractor_name="test",
                    start_line=5,
                    end_line=28,
                ),
            ),
        ),
    )
    upsert_contract(
        connection,
        Contract(
            id="contract:billing-checkout",
            name="Create billing checkout",
            kind="http",
            path="/billing/checkout",
            method="POST",
            operation_id="createBillingCheckout",
            producer_hint="BillingService",
            evidence=(
                Evidence(
                    file_path="contracts/billing.yaml",
                    extractor_name="test",
                    start_line=12,
                    end_line=20,
                ),
            ),
        ),
    )
    upsert_concept(
        connection,
        Concept(
            id="concept:billing",
            name="Billing",
            description="Domain concept for billing checkout.",
        ),
    )
    upsert_decision(
        connection,
        Decision(
            id="decision:billing-checkout",
            title="Use hosted checkout for billing",
            status="accepted",
            path="docs/adr/0003-use-hosted-checkout.md",
            context="Billing needs a checkout provider.",
            decision="Use hosted checkout.",
            consequences="Checkout flow stays outside our service boundary.",
            evidence=(
                Evidence(
                    file_path="docs/adr/0003-use-hosted-checkout.md",
                    extractor_name="test",
                    start_line=1,
                    end_line=12,
                ),
            ),
        ),
    )


def _seed_relationship_graph(connection: sqlite3.Connection) -> None:
    for edge in (
        Edge(
            source_type="service",
            source_id="service:billing",
            target_type="contract",
            target_id="contract:billing-checkout",
            relation_type="produces",
            confidence=0.9,
        ),
        Edge(
            source_type="service",
            source_id="service:billing",
            target_type="concept",
            target_id="concept:billing",
            relation_type="owns",
            confidence=0.9,
        ),
        Edge(
            source_type="decision",
            source_id="decision:billing-checkout",
            target_type="service",
            target_id="service:billing",
            relation_type="governs",
            confidence=0.9,
        ),
        Edge(
            source_type="symbol",
            source_id="symbol:billing-service",
            target_type="service",
            target_id="service:billing",
            relation_type="implements",
            confidence=0.9,
        ),
        Edge(
            source_type="file",
            source_id="file:billing-service",
            target_type="service",
            target_id="service:billing",
            relation_type="contains",
            confidence=0.9,
        ),
        Edge(
            source_type="service",
            source_id="service:unrelated",
            target_type="contract",
            target_id="contract:unrelated",
            relation_type="produces",
            confidence=0.9,
        ),
    ):
        upsert_edge(connection, edge)


def _insert_embedding(
    connection: sqlite3.Connection,
    unit: RawEvidenceUnit,
    *,
    vector: tuple[float, ...],
    model_id: str = "test-semantic-provider",
) -> None:
    connection.execute(
        """
        INSERT INTO evidence_embeddings (
            evidence_unit_id,
            model_id,
            vector_json,
            dimensions,
            text_hash
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            unit.id,
            model_id,
            json.dumps(list(vector), separators=(",", ":")),
            len(vector),
            evidence_text_hash(unit.text),
        ),
    )


def _insert_orphan_embedding(
    connection: sqlite3.Connection,
    evidence_unit_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO evidence_embeddings (
            evidence_unit_id,
            model_id,
            vector_json,
            dimensions,
            text_hash
        )
        VALUES (?, ?, '[1.0,0.0,0.0]', 3, 'orphan')
        """,
        (evidence_unit_id, "test-semantic-provider"),
    )
