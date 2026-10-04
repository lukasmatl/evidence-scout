from __future__ import annotations

from copy import deepcopy

import pytest

from code_scout.pipeline.snapshot_diff import (
    SnapshotComparisonError,
    compare_snapshot_payloads,
)


def _fact(
    *,
    fact_id: str = "content-derived-id-a",
    repository_id: str = "repo-a",
    entity_id: str = "service:catalog",
    source_file: str = "catalog.py",
    properties: dict[str, object] | None = None,
    confidence: float = 0.8,
    provenance: str = "tree_sitter",
    revision: str = "source-revision:repo-a:catalog.py:a",
    assessment: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": fact_id,
        "extractor": "tree_sitter",
        "kind": "service",
        "source_file": source_file,
        "identity": {"entity_type": "service", "entity_id": entity_id},
        "properties": properties or {"name": "catalog"},
        "confidence": confidence,
        "conflict_metadata": {},
        "evidence": [
            {
                "file_path": source_file,
                "extractor_name": "tree_sitter",
                "confidence": confidence,
                "start_line": 4,
                "end_line": 4,
                "metadata": {
                    "repository_id": repository_id,
                    "source_revision_id": revision,
                    "content_sha256": revision.rsplit(":", 1)[-1],
                    "provenance": provenance,
                    "assessment": assessment
                    or {
                        "origin": "extracted",
                        "validation": "unknown",
                        "conflicted": False,
                        "freshness": "unknown",
                    },
                },
            }
        ],
    }


def _payload(
    *facts: dict[str, object],
    scope: dict[str, object] | None = None,
    coverage: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "version": 1,
        "active_facts": list(facts),
        "resolved_entities": {},
        "resolved_edges": [],
        "raw_evidence": [],
        "revision_references": [],
        "repository_identities": [],
        "extractor_versions": [],
        "scan_scope": scope if scope is not None else {"selection_type": "all"},
        "coverage": coverage if coverage is not None else {"discovered_files": 1},
    }


def _diff(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    return compare_snapshot_payloads(
        before,
        after,
        before_snapshot_id="snapshot:before",
        before_content_sha256="a" * 64,
        after_snapshot_id="snapshot:after",
        after_content_sha256="b" * 64,
    )


def test_snapshot_diff_is_noop_for_same_persisted_observation() -> None:
    payload = _payload(_fact())
    later = deepcopy(payload)

    result = _diff(payload, later)

    assert result["counts"] == {"added": 0, "removed": 0, "modified": 0}
    assert result["before_snapshot"]["content_sha256"] == "a" * 64  # type: ignore[index]
    assert result["after_snapshot"]["id"] == "snapshot:after"  # type: ignore[index]


def test_snapshot_diff_preserves_domain_timestamp_properties_and_metadata() -> None:
    before = _payload(
        _fact(
            properties={
                "timestamp": "domain-before",
                "nested": {"created_at": "domain-created-before"},
            }
        )
    )
    after = deepcopy(before)
    before_fact = before["active_facts"][0]  # type: ignore[index]
    after_fact = after["active_facts"][0]  # type: ignore[index]
    before_fact["evidence"][0]["metadata"].update(  # type: ignore[index]
        {
            "timestamp": "evidence-before",
            "nested": {"scanned_at": "evidence-nested-before"},
        }
    )
    after_fact["properties"]["timestamp"] = "domain-after"  # type: ignore[index]
    after_fact["properties"]["nested"]["created_at"] = "domain-created-after"  # type: ignore[index]
    after_fact["evidence"][0]["metadata"].update(  # type: ignore[index]
        {
            "timestamp": "evidence-after",
            "nested": {"scanned_at": "evidence-nested-after"},
        }
    )

    result = _diff(before, after)

    assert result["counts"] == {"added": 0, "removed": 0, "modified": 1}
    changes = result["modified"][0]["changes"]  # type: ignore[index]
    assert changes["properties"]["timestamp"] == {
        "before": "domain-before",
        "after": "domain-after",
    }
    assert changes["properties"]["nested"] == {
        "before": {"created_at": "domain-created-before"},
        "after": {"created_at": "domain-created-after"},
    }
    assert changes["evidence"]["before"][0]["metadata"] == {  # type: ignore[index]
        "repository_id": "repo-a",
        "source_revision_id": "source-revision:repo-a:catalog.py:a",
        "content_sha256": "a",
        "provenance": "tree_sitter",
        "assessment": {
            "origin": "extracted",
            "validation": "unknown",
            "conflicted": False,
            "freshness": "unknown",
        },
        "timestamp": "evidence-before",
        "nested": {"scanned_at": "evidence-nested-before"},
    }
    assert changes["evidence"]["after"][0]["metadata"]["timestamp"] == "evidence-after"  # type: ignore[index]


def test_snapshot_diff_reports_add_remove_properties_and_evidence_only_changes() -> (
    None
):
    before = _payload(_fact(), _fact(fact_id="remove", entity_id="service:old"))
    changed = _fact(
        fact_id="different-content-derived-id",
        properties={"name": "catalog-v2"},
        confidence=0.9,
        provenance="lsp",
        revision="source-revision:repo-a:catalog.py:b",
    )
    after = _payload(changed, _fact(fact_id="add", entity_id="service:new"))

    result = _diff(before, after)

    assert result["counts"] == {"added": 1, "removed": 1, "modified": 1}
    removed = result["removed"][0]  # type: ignore[index]
    assert removed["status"] == "absent_from_later_snapshot"
    assert "not proof" in removed["interpretation"]
    changes = result["modified"][0]["changes"]  # type: ignore[index]
    assert changes["properties"]["name"] == {"before": "catalog", "after": "catalog-v2"}
    assert changes["confidence"] == {"before": 0.8, "after": 0.9}
    assert changes["source_revisions"][0]["before"]["source_revision_id"].endswith(":a")
    assert changes["source_revisions"][0]["after"]["source_revision_id"].endswith(":b")
    assert changes["evidence"]


def test_snapshot_diff_reports_persisted_conflict_assessment_transitions() -> None:
    before = _payload(_fact())
    after = _payload(
        _fact(
            assessment={
                "origin": "extracted",
                "validation": "corroborated",
                "conflicted": True,
                "freshness": "unknown",
            }
        )
    )

    introduced = _diff(before, after)
    resolved = _diff(after, before)

    transition = introduced["modified"][0]["changes"]["assessment_transitions"][0]  # type: ignore[index]
    assert transition["before"]["conflicted"] is False
    assert transition["after"]["conflicted"] is True
    assert resolved["modified"][0]["changes"]["assessment_transitions"]  # type: ignore[index]


def test_snapshot_diff_qualifies_same_path_and_entity_by_repository() -> None:
    before = _payload(
        _fact(repository_id="repo-a", entity_id="service:shared"),
        _fact(repository_id="repo-b", entity_id="service:shared"),
    )
    after = _payload(
        _fact(repository_id="repo-a", entity_id="service:shared", confidence=0.7),
        _fact(repository_id="repo-b", entity_id="service:shared", confidence=0.6),
    )

    result = _diff(before, after)

    assert result["counts"] == {"added": 0, "removed": 0, "modified": 2}
    assert [item["identity"]["repository_id"] for item in result["modified"]] == [  # type: ignore[index]
        "repo-a",
        "repo-b",
    ]


def test_snapshot_diff_adding_first_observation_keeps_existing_evidence_unchanged() -> (
    None
):
    before = _payload(_fact(source_file="b.py"), _fact(source_file="c.py"))
    after = _payload(
        _fact(source_file="a.py"), _fact(source_file="b.py"), _fact(source_file="c.py")
    )

    result = _diff(before, after)

    assert result["counts"] == {"added": 1, "removed": 0, "modified": 0}
    assert result["added"][0]["after"]["source_file"] == "a.py"  # type: ignore[index]


def test_snapshot_diff_removing_first_observation_reports_the_removed_source() -> None:
    before = _payload(
        _fact(source_file="a.py"), _fact(source_file="b.py"), _fact(source_file="c.py")
    )
    after = _payload(_fact(source_file="b.py"), _fact(source_file="c.py"))

    result = _diff(before, after)

    assert result["counts"] == {"added": 0, "removed": 1, "modified": 0}
    assert result["removed"][0]["before"]["source_file"] == "a.py"  # type: ignore[index]


def test_snapshot_diff_matches_duplicate_observations_by_multiplicity() -> None:
    duplicate = _fact(source_file="same.py")
    before = _payload(deepcopy(duplicate), deepcopy(duplicate))
    after = _payload(deepcopy(duplicate), deepcopy(duplicate), deepcopy(duplicate))

    result = _diff(before, after)

    assert result["counts"] == {"added": 1, "removed": 0, "modified": 0}
    assert result["added"][0]["after"]["source_file"] == "same.py"  # type: ignore[index]


def test_snapshot_diff_pairs_real_change_only_with_its_stable_source() -> None:
    before = _payload(_fact(source_file="a.py"), _fact(source_file="b.py"))
    after = _payload(
        _fact(source_file="b.py"),
        _fact(source_file="a.py", properties={"name": "catalog-v2"}),
    )

    result = _diff(before, after)

    assert result["counts"] == {"added": 0, "removed": 0, "modified": 1}
    assert result["modified"][0]["before"]["source_file"] == "a.py"  # type: ignore[index]
    assert result["modified"][0]["after"]["source_file"] == "a.py"  # type: ignore[index]


def test_snapshot_diff_does_not_cross_pair_same_paths_from_repositories() -> None:
    before = _payload(
        _fact(
            repository_id="repo-a", entity_id="service:shared", source_file="same.py"
        ),
        _fact(
            repository_id="repo-b", entity_id="service:shared", source_file="same.py"
        ),
    )
    after = _payload(
        _fact(
            repository_id="repo-a",
            entity_id="service:shared",
            source_file="same.py",
            properties={"name": "a-changed"},
        ),
        _fact(
            repository_id="repo-b", entity_id="service:shared", source_file="same.py"
        ),
    )

    result = _diff(before, after)

    assert result["counts"] == {"added": 0, "removed": 0, "modified": 1}
    assert result["modified"][0]["identity"]["repository_id"] == "repo-a"  # type: ignore[index]


def test_snapshot_diff_leaves_ambiguous_residuals_unpaired() -> None:
    before = _payload(
        _fact(fact_id="one", source_file="same.py", properties={"name": "one"}),
        _fact(fact_id="two", source_file="same.py", properties={"name": "two"}),
    )
    after = _payload(
        _fact(fact_id="three", source_file="same.py", properties={"name": "three"}),
        _fact(fact_id="four", source_file="same.py", properties={"name": "four"}),
    )

    result = _diff(before, after)

    assert result["counts"] == {"added": 2, "removed": 2, "modified": 0}
    assert result["uncertainties"][0]["kind"] == "ambiguous_observation_pairing"  # type: ignore[index]


def test_snapshot_diff_pairs_relations_by_repository_qualified_endpoints() -> None:
    relation = _fact(fact_id="content-a")
    relation["kind"] = "imports"
    relation["identity"] = {
        "subject": {"type": "file", "id": "file:a"},
        "relation": "imports",
        "object": {"type": "file", "id": "file:b"},
    }
    before = _payload(relation)
    before["resolved_entities"] = {
        "files": [
            {"id": "file:a", "repository_id": "repo-a"},
            {"id": "file:b", "repository_id": "repo-b"},
        ]
    }
    changed = deepcopy(relation)
    changed["id"] = "content-b"
    changed["confidence"] = 0.5
    after = _payload(changed)
    after["resolved_entities"] = before["resolved_entities"]

    result = _diff(before, after)

    identity = result["modified"][0]["identity"]  # type: ignore[index]
    assert identity["subject"]["repository_id"] == "repo-a"
    assert identity["object"]["repository_id"] == "repo-b"


def test_snapshot_diff_marks_scope_and_unknown_coverage_as_uncertain() -> None:
    result = _diff(
        _payload(_fact(), scope={"selection_type": "all"}, coverage={}),
        _payload(_fact(), scope={"selection_type": "profile"}, coverage={}),
    )

    assert result["scope_difference"]
    assert result["coverage_difference"] is None
    assert {item["kind"] for item in result["uncertainties"]} == {  # type: ignore[index]
        "coverage_unknown",
        "scope_changed",
    }


def test_snapshot_diff_rejects_legacy_unavailable_payload() -> None:
    with pytest.raises(SnapshotComparisonError, match="historical content unavailable"):
        compare_snapshot_payloads(
            None,
            _payload(_fact()),
            before_snapshot_id="legacy",
            before_content_sha256=None,
            after_snapshot_id="snapshot:after",
            after_content_sha256="b" * 64,
        )
