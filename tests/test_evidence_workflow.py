"""Small end-to-end evidence-history workflow fixture.

The providers in this test are deliberately scripted: the assertions exercise
local revision binding and retained-history semantics, not a model endpoint.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from code_scout.commands import CommandOptions, command, execute_command
from code_scout.engines import (
    Citation,
    CodeScoutEngine,
    CodeScoutRequest,
    OpenAIChatChoice,
    OpenAIChatMessage,
    OpenAIChatResponse,
    OpenAIModelTarget,
    OpenAIToolCall,
    RLMClaim,
    RLMResponse,
    StaticEngine,
    VerifyEngine,
)
from code_scout.engines.citations import validate_citations, validate_result_citations
from code_scout.evidence.pack import EvidencePack, compile_evidence_pack
from code_scout.pipeline.scan import scan_workspace
from code_scout.runtime.config import VerifyModelConfig
from code_scout.storage import list_snapshots, open_storage
from code_scout.workspace.config import create_workspace_config


class _FastProvider:
    """A provider response that localizes the one-line package manifest."""

    def __call__(self, *_args: object, **_kwargs: object) -> OpenAIChatResponse:
        return OpenAIChatResponse(
            id="fast-fixture",
            model="mock-fast",
            choices=(
                OpenAIChatChoice(
                    0,
                    OpenAIChatMessage(
                        "assistant",
                        None,
                        (
                            OpenAIToolCall(
                                "finish",
                                "localization_finish",
                                json.dumps(
                                    {
                                        "locations": [
                                            {
                                                "file": "package.json",
                                            }
                                        ]
                                    }
                                ),
                            ),
                        ),
                    ),
                    "tool_calls",
                ),
            ),
        )


class _VerifyProvider:
    def __init__(self, evidence_id: str) -> None:
        self.evidence_id = evidence_id

    def run(self, _request: object) -> RLMResponse:
        return RLMResponse(
            "scripted verification",
            claims=(
                RLMClaim(
                    "The scanned package manifest is present.",
                    (self.evidence_id,),
                    (Citation("package.json", 1),),
                    0.95,
                ),
            ),
        )


def _fast_engine() -> CodeScoutEngine:
    return CodeScoutEngine(
        OpenAIModelTarget("https://models.example.test/v1", "mock-fast"),
        model_client=_FastProvider(),
    )


def _verify_engine(evidence_id: str, pack: EvidencePack) -> VerifyEngine:
    target = OpenAIModelTarget("https://models.example.test/v1", "mock-verify")
    return VerifyEngine(
        VerifyModelConfig(target, target, isolation="local"),
        boundary=_VerifyProvider(evidence_id),
        pack_factory=lambda _workspace, *, question: pack,
        environment={},
    )


def _latest_snapshot(workspace: Path):
    with open_storage(workspace) as connection:
        return list_snapshots(connection)[-1]


def test_three_feature_workflow_retains_revisions_and_compares_snapshots(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    manifest = tmp_path / "package.json"
    version_a = '{"name":"version-a"}\n'
    version_b = '{"name":"version-b"}\n'  # Deliberately the same line count.
    manifest.write_text(version_a, encoding="utf-8")

    scan_workspace(str(tmp_path), extractor="package_metadata")
    snapshot_a = _latest_snapshot(tmp_path)
    static_a = StaticEngine().execute(
        CodeScoutRequest("package manifest", tmp_path, "static")
    )
    fast_a = _fast_engine().execute(
        CodeScoutRequest("locate package", tmp_path, "fast")
    )
    revision_a = static_a.citations[0].source_revision_id
    assert revision_a is not None
    # A file-only finish did not observe a source identity during this request.
    assert fast_a.citations[0].source_revision_id is None

    manifest.write_text(version_b, encoding="utf-8")
    scan_workspace(str(tmp_path), extractor="package_metadata")
    snapshot_b = _latest_snapshot(tmp_path)
    static_b = StaticEngine().execute(
        CodeScoutRequest("version-b package manifest", tmp_path, "static")
    )
    fast_b = _fast_engine().execute(
        CodeScoutRequest("locate package", tmp_path, "fast")
    )
    revision_b = static_b.citations[0].source_revision_id

    # A's stored byte identity never changes, even though live B has identical lines.
    stale_a = validate_result_citations(static_a, tmp_path).citations[0]
    assert stale_a.source_revision_id == revision_a
    assert stale_a.source_assessment.historical == "verified"
    assert stale_a.source_assessment.freshness == "stale"
    assert revision_b is not None and revision_b != revision_a
    assert static_b.citations[0].content_sha256 != static_a.citations[0].content_sha256
    # File-only fast localization has no uniquely indexed observation after A/B.
    assert fast_b.citations[0].source_revision_id is None
    assert fast_b.citations[0].source_assessment.freshness == "unknown"

    # The mock verify provider can bind only the exact revision in its frozen B pack.
    pack_b = compile_evidence_pack(tmp_path, question="version-b package manifest")
    assert pack_b.evidence[0].source_revision_id == revision_b
    # package_metadata has no source range; the scripted provider receives the
    # same stored B identity with its explicit one-line citation coordinate.
    pack_b = replace(
        pack_b,
        evidence=(replace(pack_b.evidence[0], start_line=1, end_line=1),),
    )
    verify_b = _verify_engine(pack_b.evidence[0].id, pack_b).execute(
        CodeScoutRequest("verify package", tmp_path, "verify")
    )
    assert verify_b.citations[0].source_revision_id == revision_b
    assert verify_b.citations[0].source_assessment.freshness == "fresh"

    # A path/line citation without the complete stored tuple remains honestly unknown.
    unbound = validate_citations(tmp_path, (Citation("package.json", 1),)).citations[0]
    assert unbound.source_assessment.historical == "unknown"
    assert unbound.source_assessment.freshness == "unknown"

    retained_a = json.loads(
        execute_command(
            command(("evidence", "show")),
            CommandOptions(
                workspace=str(tmp_path), revision_id=revision_a, output_format="json"
            ),
        )
    )
    assert retained_a["text"] == version_a

    comparison = json.loads(
        execute_command(
            command(("changes",)),
            CommandOptions(
                workspace=str(tmp_path),
                from_snapshot_id=snapshot_a.id,
                to_snapshot_id=snapshot_b.id,
                output_format="json",
            ),
        )
    )
    assert comparison["counts"]["modified"] >= 1
    assert any(
        {"evidence", "source_revision"} & set(change["changes"])
        for change in comparison["modified"]
    )


def test_file_only_fast_finish_cannot_borrow_a_stale_index_revision(
    tmp_path: Path,
) -> None:
    create_workspace_config(tmp_path)
    manifest = tmp_path / "package.json"
    manifest.write_text('{"name":"version-a"}\n', encoding="utf-8")
    scan_workspace(str(tmp_path), extractor="package_metadata")
    indexed = StaticEngine().execute(CodeScoutRequest("package", tmp_path, "static"))

    # Same coordinate, different bytes, and intentionally no intervening scan.
    manifest.write_text('{"name":"version-b"}\n', encoding="utf-8")
    fast = _fast_engine().execute(CodeScoutRequest("locate package", tmp_path, "fast"))

    assert indexed.citations[0].source_revision_id is not None
    assert fast.citations[0].source_revision_id is None
    validated_fast = validate_result_citations(fast, tmp_path)
    assessment = validated_fast.citations[0].source_assessment
    assert assessment.freshness == "unknown"
