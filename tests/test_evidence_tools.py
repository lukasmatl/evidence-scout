from __future__ import annotations

import json

import pytest

from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.evidence.pack import (
    EvidencePack,
    PackedEvidence,
    PackedRelationship,
    PackedSourceProvenance,
    RepositoryMapEntry,
)
from code_scout.evidence.tools import EvidenceToolLimits, EvidenceTools


def test_evidence_tools_search_read_and_review_are_bounded(tmp_path) -> None:
    tools = EvidenceTools(tmp_path, _pack(tmp_path))
    limits = EvidenceToolLimits(
        max_matches=1, max_lines=1, max_characters=12, max_hops=1
    )

    search = tools.search_evidence("checkout", limits=limits)
    source = tools.read_source_range("evidence:a", limits=limits)
    review = tools.inspect_review_items(limits=limits)

    assert search["items"][0]["text"] == "checkout imp"
    assert search["truncated"] is True
    assert source["items"] == [{"line": 1, "text": "def caller()"}]
    assert source["truncated"] is True
    assert source["provenance"]["source_file"] == "src/app.py"
    assert len(review["items"]) == 1
    assert review["truncated"] is True
    for result in (search, source, review):
        assert result["limits"]["characters_used"] <= 12
        json.dumps(result)


def test_source_read_stays_inside_pack_citation_and_workspace(tmp_path) -> None:
    pack = _pack(tmp_path)
    tools = EvidenceTools(tmp_path, pack)

    with pytest.raises(ValueError, match="inside the cited"):
        tools.read_source_range("evidence:a", start_line=1, end_line=4)

    outside = tmp_path.parent / "secret.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    escaped = _evidence("evidence:escape", "../secret.py", "secret = True", 1, 1)
    unsafe_tools = EvidenceTools(
        tmp_path,
        EvidencePack(
            query=pack.query,
            evidence=(*pack.evidence, escaped),
            relationships=pack.relationships,
            repository_map=pack.repository_map,
            needs_review=pack.needs_review,
            context=pack.context,
            truncated=pack.truncated,
        ),
    )

    result = unsafe_tools.read_source_range("evidence:escape")

    assert result["status"] == "unavailable"
    assert "secret" not in json.dumps(result)


def test_symbol_inspection_preserves_ambiguity_and_missing_status(tmp_path) -> None:
    tools = EvidenceTools(tmp_path, _pack(tmp_path))

    exact = tools.inspect_symbol("symbol:a")
    ambiguous = tools.inspect_symbol("helper")
    missing = tools.inspect_symbol("does_not_exist")

    assert exact["status"] == "confirmed"
    assert exact["items"][0]["provenance"][0]["extractor"] == "lsp"
    assert ambiguous["status"] == "ambiguous"
    assert [item["id"] for item in ambiguous["items"]] == [
        "symbol:b",
        "symbol:c",
    ]
    assert missing["status"] == "unavailable"


def test_callers_and_callees_report_each_confidence_class(tmp_path) -> None:
    tools = EvidenceTools(tmp_path, _pack(tmp_path))
    one_hop = EvidenceToolLimits(max_hops=1)

    callers = tools.list_callers("symbol:a", limits=one_hop)
    callees = tools.list_callees("symbol:a", limits=one_hop)

    assert {
        item["neighbor_id"]: item["confidence_class"] for item in callers["items"]
    } == {"symbol:c": "inferred", "symbol:x": "unavailable"}
    assert [
        (item["neighbor_id"], item["confidence_class"]) for item in callees["items"]
    ] == [("symbol:b", "confirmed")]
    assert callees["items"][0]["provenance"]["sources"] == [
        {
            "assessment": json.loads(
                json.dumps(
                    assess_legacy_evidence({}, extractor_provenance=("lsp",)).to_dict()
                )
            ),
            "source_file": "src/app.py",
            "start_line": 2,
            "end_line": 2,
            "extractor": "lsp",
            "confidence": 0.95,
        }
    ]


def test_multi_hop_traversal_terminates_on_cycles_and_caps_results(tmp_path) -> None:
    tools = EvidenceTools(tmp_path, _pack(tmp_path))

    traversal = tools.traverse_neighbors(
        "symbol:a",
        direction="outgoing",
        limits=EvidenceToolLimits(max_matches=10, max_hops=5),
    )
    truncated = tools.traverse_neighbors(
        "symbol:a",
        direction="outgoing",
        limits=EvidenceToolLimits(max_matches=2, max_hops=5),
    )

    assert [(item["neighbor_id"], item["hop"]) for item in traversal["items"]] == [
        ("symbol:b", 1),
        ("symbol:c", 2),
        ("symbol:a", 3),
    ]
    assert len({item["id"] for item in traversal["items"]}) == 3
    assert truncated["truncated"] is True
    assert len(truncated["items"]) == 2


def test_tool_limits_reject_unbounded_requests() -> None:
    with pytest.raises(ValueError, match="max_hops"):
        EvidenceToolLimits(max_hops=6)
    with pytest.raises(ValueError, match="max_characters"):
        EvidenceToolLimits(max_characters=20_001)


def _pack(tmp_path) -> EvidencePack:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text(
        "def caller():\n    return helper()\ndef helper():\n    return caller()\n",
        encoding="utf-8",
    )
    evidence = (
        _evidence("evidence:a", "src/app.py", "checkout implementation", 1, 2),
        _evidence("evidence:b", "src/app.py", "checkout helper", 3, 4),
    )
    source_provenance = (PackedSourceProvenance("src/app.py", 2, 2, "lsp", 0.95),)
    relationships = (
        PackedRelationship(
            "relationship:1",
            "symbol",
            "symbol:a",
            "calls",
            "symbol",
            "symbol:b",
            0.95,
            ("lsp",),
            source_provenance,
        ),
        PackedRelationship(
            "relationship:2",
            "symbol",
            "symbol:c",
            "calls",
            "symbol",
            "symbol:a",
            0.7,
            ("tree-sitter",),
            (PackedSourceProvenance("src/app.py", 4, 4, "tree_sitter", 0.7),),
        ),
        PackedRelationship(
            "relationship:3",
            "symbol",
            "symbol:b",
            "calls",
            "symbol",
            "symbol:c",
            0.3,
            ("lexical",),
        ),
        PackedRelationship(
            "relationship:4",
            "symbol",
            "symbol:x",
            "calls",
            "symbol",
            "symbol:a",
            0.99,
            (),
            (PackedSourceProvenance("src/app.py", 1, 1, "", 0.99),),
        ),
    )
    repository_map = (
        RepositoryMapEntry(
            "symbol", "symbol:a", "src/app.py", "caller", "def caller():", 1, 2
        ),
        RepositoryMapEntry(
            "symbol", "symbol:b", "src/app.py", "app.helper", "def helper():", 3, 4
        ),
        RepositoryMapEntry(
            "symbol", "symbol:c", "src/app.py", "other.helper", None, 3, 4
        ),
    )
    return EvidencePack(
        query="checkout",
        evidence=evidence,
        relationships=relationships,
        repository_map=repository_map,
        needs_review=(
            {"code": "resolver_conflict", "detail": "first"},
            {"code": "resolver_conflict", "detail": "second"},
        ),
        context="{}",
        truncated=False,
    )


def _evidence(
    evidence_id: str,
    path: str,
    text: str,
    start_line: int,
    end_line: int,
) -> PackedEvidence:
    return PackedEvidence(
        id=evidence_id,
        rank=1 if evidence_id == "evidence:a" else 2,
        score=1.0,
        owner_type="symbol",
        owner_id="symbol:a",
        source_file=path,
        start_line=start_line,
        end_line=end_line,
        text=text,
        confidence=0.95,
        provenance="lsp",
        scoring_reasons=("lexical",),
        related_symbols=("symbol:a",),
        related_files=("file:app",),
        relationship_ids=("relationship:1",),
    )
