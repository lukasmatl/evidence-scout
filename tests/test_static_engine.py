from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_scout.engines import (
    CodeScoutRequest,
    StaticEngine,
    StaticIndexError,
    create_engine_registry,
)
from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.evidence.embeddings import LocalEmbeddingProvider
from code_scout.runtime.config import CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE
from code_scout.storage import (
    RawEvidenceUnit,
    open_storage,
    refresh_evidence_embeddings,
    store_source_revision,
    upsert_raw_evidence_units,
)


def _seed_index(
    workspace: Path,
    units: tuple[RawEvidenceUnit, ...] | None = None,
) -> None:
    source = workspace / "src" / "router.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def route_request(request):\n    return dispatch(request)\n",
        encoding="utf-8",
    )
    if units is None:
        units = (
            RawEvidenceUnit(
                id="evidence-unit:router",
                owner_entity_type="symbol",
                owner_entity_id="symbol:route_request",
                source_file="src/router.py",
                start_line=1,
                end_line=2,
                text="route_request implements request routing and dispatch.",
                provenance="test",
                confidence=1.0,
            ),
        )
    with open_storage(workspace) as connection:
        upsert_raw_evidence_units(connection, units)
        refresh_evidence_embeddings(
            connection,
            units,
            provider=LocalEmbeddingProvider(),
        )
        connection.commit()


def test_static_engine_queries_the_scan_backed_index_deterministically(
    tmp_path: Path,
) -> None:
    _seed_index(tmp_path)
    engine = StaticEngine()
    request = CodeScoutRequest(
        query="Where is request routing implemented?",
        workspace=tmp_path,
        engine="static",
    )

    first = engine.execute(request)
    second = engine.execute(request)

    assert first.answer == second.answer
    assert "src/router.py:1-2" in first.answer
    assert first.citations == second.citations
    assert first.citations[0].path == "src/router.py"
    assert first.engine.name == "static"
    assert first.engine.provider == "local-index"
    assert first.metadata["query_mode"] == "deterministic-hybrid"
    assert first.metadata["query"] == "Where is request routing implemented?"
    assert first.metadata["result_kind"] == "matched"
    assert "gap_reason" not in first.metadata
    assert first.metadata["match_count"] == 1
    assert first.metadata["matches"] == [
        {
            "rank": 1,
            "evidence_id": "evidence-unit:router",
            "assessment": assess_legacy_evidence({}).to_dict(),
            "score": first.metadata["matches"][0]["score"],
            "source_file": "src/router.py",
            "line_range": {"start": 1, "end": 2},
            "owner_entity": {"type": "symbol", "id": "symbol:route_request"},
            "text": "route_request implements request routing and dispatch.",
            "scoring_reasons": ["semantic match"],
        }
    ]
    assert isinstance(first.metadata["matches"][0]["score"], float)
    json.dumps(dict(first.metadata))


def test_static_citation_uses_the_indexed_revision_not_live_hash(
    tmp_path: Path,
) -> None:
    source_bytes = b"def route_request(request):\n    return dispatch(request)\n"
    with open_storage(tmp_path) as connection:
        revision = store_source_revision(
            connection,
            repository_id="workspace",
            path="src/router.py",
            source_bytes=source_bytes,
        )
        connection.commit()
    _seed_index(
        tmp_path,
        (
            RawEvidenceUnit(
                id="evidence-unit:router",
                owner_entity_type="symbol",
                owner_entity_id="symbol:route_request",
                source_file="src/router.py",
                start_line=1,
                end_line=2,
                text="route_request implements request routing and dispatch.",
                provenance="test",
                confidence=1.0,
                metadata={
                    "repository_id": revision.repository_id,
                    "source_revision_id": revision.id,
                    "content_sha256": revision.content_sha256,
                },
            ),
        ),
    )

    result = StaticEngine().execute(
        CodeScoutRequest(query="request routing", workspace=tmp_path, engine="static")
    )

    assert result.citations[0].source_revision_id == revision.id
    assert result.citations[0].content_sha256 == revision.content_sha256


def test_static_engine_returns_ranked_metadata_for_multiple_matches(
    tmp_path: Path,
) -> None:
    _seed_index(
        tmp_path,
        (
            RawEvidenceUnit(
                id="evidence-unit:router",
                owner_entity_type="symbol",
                owner_entity_id="symbol:route_request",
                source_file="src/router.py",
                start_line=1,
                end_line=2,
                text="route_request implements request routing and dispatch.",
                provenance="test",
                confidence=1.0,
            ),
            RawEvidenceUnit(
                id="evidence-unit:api",
                owner_entity_type="file",
                owner_entity_id="file:api",
                source_file="src/api.py",
                start_line=10,
                end_line=10,
                text="api handlers delegate request routing to route_request.",
                provenance="test",
                confidence=1.0,
            ),
        ),
    )

    result = StaticEngine(limit=2).execute(
        CodeScoutRequest(
            query="Where is request routing implemented?",
            workspace=tmp_path,
            engine="static",
        )
    )

    matches = result.metadata["matches"]
    assert result.metadata["match_count"] == 2
    assert [match["rank"] for match in matches] == [1, 2]
    assert [match["source_file"] for match in matches] == [
        citation.path for citation in result.citations
    ]
    assert all(
        set(match)
        == {
            "rank",
            "evidence_id",
            "assessment",
            "score",
            "source_file",
            "line_range",
            "owner_entity",
            "text",
            "scoring_reasons",
        }
        for match in matches
    )
    assert all(isinstance(match["scoring_reasons"], list) for match in matches)
    assert matches == sorted(
        matches,
        key=lambda match: match["rank"],
    )
    json.dumps(dict(result.metadata))


def test_static_engine_returns_structured_no_match_metadata(
    tmp_path: Path,
) -> None:
    _seed_index(tmp_path, ())

    result = StaticEngine().execute(
        CodeScoutRequest(
            query="Where is request routing implemented?",
            workspace=tmp_path,
            engine="static",
        )
    )

    assert result.answer == (
        'No static evidence found for: "Where is request routing implemented?"'
    )
    assert "model" not in result.answer.casefold()
    assert "synthesis" not in result.answer.casefold()
    assert result.citations == ()
    assert result.metadata["result_kind"] == "no_match"
    assert result.metadata["gap_reason"] == "no_static_evidence_found"
    assert result.metadata["needs_review"] == [
        {
            "code": "no-static-evidence-found",
            "message": (
                "No static evidence was found for this query in the scanned workspace."
            ),
        }
    ]
    assert result.metadata["match_count"] == 0
    assert result.metadata["matches"] == []
    json.dumps(dict(result.metadata))


def test_static_engine_requires_a_scanned_index(tmp_path: Path) -> None:
    with pytest.raises(StaticIndexError, match="Static index not found") as caught:
        StaticEngine().execute(
            CodeScoutRequest(
                query="Where is request routing implemented?",
                workspace=tmp_path,
                engine="static",
            )
        )

    assert "code-scout scan" in caught.value.hint


def test_static_engine_is_available_without_an_endpoint() -> None:
    availability = StaticEngine().availability(probe_endpoint=True)

    assert availability.ready is True


def test_default_registry_runs_static_without_global_model_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_index(tmp_path)
    monkeypatch.setenv(
        CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE,
        str(tmp_path / "missing-global-config"),
    )

    result = create_engine_registry().execute(
        CodeScoutRequest(
            query="Where is request routing implemented?",
            workspace=tmp_path,
            engine="static",
        )
    )

    assert result.metadata["resolved_engine"] == "static"
    assert result.metadata["resolved_provider"] == "local-index"


@pytest.mark.parametrize(
    "metadata, expected",
    [
        (
            {"unresolved": True, "resolver_conflicts": ["target"]},
            ["extracted", "unresolved", "conflicted", "unknown"],
        ),
        (
            {"assessment": {"validation": "corroborated"}},
            ["extracted", "validated", "unknown"],
        ),
        ({}, ["extracted", "unknown"]),
    ],
)
def test_static_preserves_per_match_assessments(tmp_path, metadata, expected):
    from code_scout.commands.cli import render_scout_result
    from code_scout.engines.citations import validate_result_citations

    _seed_index(
        tmp_path,
        (
            RawEvidenceUnit(
                "raw:route",
                "symbol",
                "route",
                "src/router.py",
                "route request",
                "tree_sitter",
                1.0,
                1,
                2,
                {"evidence_metadata": metadata},
            ),
        ),
    )
    result = StaticEngine().execute(CodeScoutRequest("route", tmp_path, "static"))
    result = validate_result_citations(result, tmp_path)
    assert result.metadata["matches"][0]["assessment"]["labels"] == expected
    assert (
        list(result.citations[0].supporting_evidence[0].assessment.labels) == expected
    )
    payload = json.loads(render_scout_result(result, "json"))
    assert payload["evidence"][0]["assessment"]["labels"] == expected
    assert payload["likely_starting_points"][0]["evidence_id"] == "raw:route"
    assert "assessment" not in payload  # No answer-wide promotion.
    assert ", ".join(expected) in render_scout_result(result)


def test_static_retains_resolver_conflicts_on_raw_observations(tmp_path):
    _seed_index(
        tmp_path,
        (
            RawEvidenceUnit(
                "raw:route",
                "service",
                "service:router",
                "src/router.py",
                "dispatch request",
                "tree_sitter",
                1.0,
                1,
                2,
            ),
        ),
    )
    with open_storage(tmp_path) as connection:
        connection.execute(
            "INSERT INTO services (id, name, evidence_json) VALUES (?, ?, ?)",
            (
                "service:router",
                "router",
                json.dumps(
                    [
                        {
                            "file_path": "src/router.py",
                            "extractor_name": "tree_sitter",
                            "metadata": {"resolver_conflicts": ["description"]},
                        }
                    ]
                ),
            ),
        )
        connection.commit()
    result = StaticEngine().execute(CodeScoutRequest("dispatch", tmp_path, "static"))
    match = result.metadata["matches"][0]
    assert match["evidence_id"] == "raw:route"
    assert match["assessment"]["conflicted"] is True
    assert "conflicted" in result.answer
