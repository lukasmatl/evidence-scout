from __future__ import annotations

import pytest

from code_scout.utils.entity_paths import (
    entity_markdown_link,
    entity_markdown_path,
    entity_markdown_paths,
)


@pytest.mark.parametrize(
    ("entity_type", "row", "expected_path"),
    (
        (
            "service",
            {"id": "service:payments", "name": "Payments API"},
            "services/payments-api.md",
        ),
        (
            "contract",
            {"id": "contract:payments", "name": "POST /payments"},
            "contracts/post-payments.md",
        ),
        (
            "concept",
            {"id": "concept:contract-memory", "name": "Contract Memory"},
            "concepts/contract-memory.md",
        ),
        ("repository", {"id": "web-app", "name": "Web App"}, "repositories/web-app.md"),
        (
            "decision",
            {"id": "decision:adr-0001", "title": "Use SQLite"},
            "decisions/use-sqlite.md",
        ),
    ),
)
def test_entity_markdown_path_uses_plural_folder_and_display_name(
    entity_type,
    row,
    expected_path,
) -> None:
    assert entity_markdown_path(entity_type, row) == expected_path


def test_entity_markdown_path_falls_back_to_sanitized_id_without_display_name() -> None:
    assert (
        entity_markdown_path(
            "service", {"id": "service:apps/api:HTTP Adapter", "name": "!!!"}
        )
        == "services/service-apps-api-http-adapter.md"
    )


def test_entity_markdown_paths_append_sanitized_id_for_duplicate_display_names() -> (
    None
):
    paths = entity_markdown_paths(
        "service",
        (
            {"id": "service:backend:payments", "name": "Payments"},
            {"id": "service:frontend:payments", "name": "Payments"},
            {"id": "service:billing", "name": "Billing"},
        ),
    )

    assert paths == {
        "service:backend:payments": ("services/payments-service-backend-payments.md"),
        "service:billing": "services/billing.md",
        "service:frontend:payments": ("services/payments-service-frontend-payments.md"),
    }


def test_entity_markdown_paths_resolve_sanitized_id_collisions_deterministically() -> (
    None
):
    paths = entity_markdown_paths(
        "contract",
        (
            {"id": "contract:api/foo", "name": "Checkout"},
            {"id": "contract:api:foo", "name": "Checkout"},
        ),
    )

    assert paths["contract:api/foo"] == "contracts/checkout-contract-api-foo.md"
    assert paths["contract:api:foo"].startswith("contracts/checkout-contract-api-foo-")
    assert paths["contract:api:foo"].endswith(".md")
    assert len(set(paths.values())) == 2


def test_entity_markdown_paths_resolve_missing_display_names_with_punctuation_ids() -> (
    None
):
    paths = entity_markdown_paths(
        "decision",
        (
            {"id": "decision:workspace:docs/adr/0001-use-sqlite.md", "title": ""},
            {"id": "decision:workspace:docs:adr:0001-use-sqlite.md", "title": "!!!"},
        ),
    )

    assert (
        paths["decision:workspace:docs/adr/0001-use-sqlite.md"]
        == "decisions/decision-workspace-docs-adr-0001-use-sqlite-md.md"
    )
    assert paths["decision:workspace:docs:adr:0001-use-sqlite.md"].startswith(
        "decisions/decision-workspace-docs-adr-0001-use-sqlite-md-"
    )
    assert len(set(paths.values())) == 2


def test_entity_markdown_path_requires_entity_id() -> None:
    with pytest.raises(ValueError, match="non-empty string id"):
        entity_markdown_path("concept", {"name": "Payments"})


def test_entity_markdown_link_from_root_doc_to_nested_entity_page() -> None:
    assert (
        entity_markdown_link(
            from_page_path="overview.md",
            target_entity_type="service",
            target_entity_id="service:payments",
            label="Payments API",
            paths_by_type={"service": {"service:payments": "services/payments-api.md"}},
        )
        == "[Payments API](./services/payments-api.md)"
    )


def test_entity_markdown_link_from_index_page_to_nested_entity_page() -> None:
    assert (
        entity_markdown_link(
            from_page_path="services.md",
            target_entity_type="service",
            target_entity_id="service:payments",
            label="Payments API",
            paths_by_type={"service": {"service:payments": "services/payments-api.md"}},
        )
        == "[Payments API](./services/payments-api.md)"
    )


def test_entity_markdown_link_from_nested_page_to_sibling_entity_page() -> None:
    assert (
        entity_markdown_link(
            from_page_path="services/payments-api.md",
            target_entity_type="service",
            target_entity_id="service:billing",
            label="Billing API",
            paths_by_type={"service": {"service:billing": "services/billing-api.md"}},
        )
        == "[Billing API](./billing-api.md)"
    )


def test_entity_markdown_link_from_nested_page_to_parent_folder_entity_page() -> None:
    assert (
        entity_markdown_link(
            from_page_path="services/payments-api.md",
            target_entity_type="contract",
            target_entity_id="contract:get-payments",
            label="GET /payments",
            paths_by_type={
                "contract": {
                    "contract:get-payments": "contracts/get-payments.md",
                }
            },
        )
        == "[GET /payments](../contracts/get-payments.md)"
    )


def test_entity_markdown_link_from_nested_page_to_parent_index_page() -> None:
    assert (
        entity_markdown_link(
            from_page_path="services/payments-api/details.md",
            target_entity_type="service",
            target_entity_id="service:payments",
            label="Payments API",
            paths_by_type={"service": {"service:payments": "services/payments-api.md"}},
        )
        == "[Payments API](../payments-api.md)"
    )


@pytest.mark.parametrize(
    ("target_entity_type", "paths_by_type"),
    (
        ("file", {"file": {"file:workspace:src/app.ts": "files/app.md"}}),
        ("service", {"service": {}}),
    ),
)
def test_entity_markdown_link_falls_back_to_inline_text_for_unknown_targets(
    target_entity_type,
    paths_by_type,
) -> None:
    assert (
        entity_markdown_link(
            from_page_path="overview.md",
            target_entity_type=target_entity_type,
            target_entity_id="file:workspace:src/app.ts",
            label="src/app.ts",
            paths_by_type=paths_by_type,
        )
        == "src/app.ts"
    )
