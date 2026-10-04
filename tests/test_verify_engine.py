from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_scout.engines import (
    APPROVED_EVIDENCE_TOOLS,
    Citation,
    CodeScoutRequest,
    OpenAIModelTarget,
    RLMClaim,
    RLMCLIAdapter,
    RLMEndpointError,
    RLMIsolation,
    RLMModelRoute,
    RLMRequest,
    RLMResponse,
    RuntimeOptions,
    VerifyBudgets,
    VerifyEngine,
    VerifyError,
)
from code_scout.evidence.pack import (
    EvidencePack,
    PackedEvidence,
    PackedRelationship,
    PackedSourceProvenance,
)
from code_scout.runtime.config import VerifyModelConfig


class FakeTools:
    def __getattr__(self, name: str):
        if name not in APPROVED_EVIDENCE_TOOLS:
            raise AttributeError(name)

        def call(**arguments: object) -> dict[str, object]:
            return {"tool": name, "arguments": arguments, "status": "ok"}

        return call


class FakeRLM:
    def __init__(self, response: RLMResponse | Exception) -> None:
        self.response = response
        self.requests: list[RLMRequest] = []

    def run(self, request: RLMRequest) -> RLMResponse:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _config(**changes: object) -> VerifyModelConfig:
    config = VerifyModelConfig(
        root_model=OpenAIModelTarget(
            "https://root.example.test/v1",
            "root-model",
            api_key_env="ROOT_KEY",
            timeout_seconds=12,
            max_tokens=1_200,
            parameters={"temperature": 0.1},
        ),
        sub_model=OpenAIModelTarget(
            "https://sub.example.test/v1",
            "sub-model",
            api_key_env="SUB_KEY",
            timeout_seconds=9,
            max_tokens=300,
            parameters={"seed": 7},
        ),
        isolation="docker",
        isolation_image="python:3.12-slim",
        max_iterations=7,
        max_sub_calls=11,
        max_output_characters=2_000,
        timeout_seconds=20,
        max_total_tokens=5_000,
    )
    return replace(config, **changes)


def _pack(question: str) -> EvidencePack:
    context = json.dumps({"query": question, "evidence": [{"id": "evidence:1"}]})
    return EvidencePack(
        query=question,
        evidence=(),
        relationships=(),
        repository_map=(),
        needs_review=(),
        context=context,
        truncated=False,
    )


def _claim_pack(question: str, *, truncated: bool = False) -> EvidencePack:
    strong = PackedEvidence(
        "evidence:strong",
        1,
        1.0,
        "symbol",
        "symbol:service",
        "src/service.py",
        1,
        2,
        "service definition",
        0.98,
        "lsp",
        (),
        ("symbol:service",),
        ("src/service.py",),
        ("relationship:caller",),
    )
    heuristic = replace(
        strong,
        id="evidence:heuristic",
        rank=2,
        confidence=0.7,
        provenance="heuristic",
    )
    contradiction = replace(strong, id="evidence:contradiction", rank=3)
    caller = PackedRelationship(
        "relationship:caller",
        "symbol",
        "symbol:caller",
        "calls",
        "symbol",
        "symbol:service",
        0.98,
        ("lsp",),
        (PackedSourceProvenance("src/service.py", 1, 1, "lsp", 0.98),),
    )
    context = json.dumps(
        {
            "query": question,
            "evidence": [
                {"id": strong.id},
                {"id": heuristic.id},
                {"id": contradiction.id},
            ],
        }
    )
    return EvidencePack(
        question,
        (strong, heuristic, contradiction),
        (caller,),
        (),
        (),
        context,
        truncated,
    )


def _engine(
    tmp_path: Path,
    boundary: FakeRLM,
    config: VerifyModelConfig | None = None,
) -> VerifyEngine:
    return VerifyEngine(
        config or _config(),
        boundary=boundary,
        pack_factory=lambda workspace, *, question: _pack(question),
        tools_factory=lambda workspace, pack: FakeTools(),
        environment={
            "ROOT_KEY": "root-secret",
            "SUB_KEY": "sub-secret",
            "PATH": "",
        },
    )


def _claim_engine(
    boundary: FakeRLM,
    *,
    truncated: bool = False,
) -> VerifyEngine:
    return VerifyEngine(
        _config(),
        boundary=boundary,
        pack_factory=lambda workspace, *, question: _claim_pack(
            question, truncated=truncated
        ),
        tools_factory=lambda workspace, pack: FakeTools(),
        environment={"ROOT_KEY": "root-secret", "SUB_KEY": "sub-secret"},
    )


def _request(tmp_path: Path, **runtime: object) -> CodeScoutRequest:
    return CodeScoutRequest(
        "Trace the recursive behavior",
        tmp_path,
        "verify",
        RuntimeOptions(**runtime),
    )


def test_fake_rlm_receives_frozen_context_tools_routes_and_recursive_budgets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def service():\n    return True\n", encoding="utf-8")
    boundary = FakeRLM(
        RLMResponse(
            "The service is recursive through its verifier.",
            (Citation("src/service.py", 1, 2),),
            iterations=3,
            sub_calls=5,
            total_tokens=900,
            events=({"kind": "sub_call", "api_key": "must-not-leak"},),
        )
    )

    result = _engine(tmp_path, boundary).execute(
        _request(tmp_path, timeout_seconds=15, max_turns=4, max_tokens=2_000)
    )

    sent = boundary.requests[0]
    assert sent.external_context == _pack(sent.question).render()
    assert str(tmp_path) not in sent.external_context
    assert tuple(sent.tools) == APPROVED_EVIDENCE_TOOLS
    assert "supporting evidence_ids" in sent.response_instructions
    assert sent.tools["search_evidence"](query="service")["status"] == "ok"
    assert sent.root_model.target.base_url == "https://root.example.test/v1"
    assert sent.sub_model.target.base_url == "https://sub.example.test/v1"
    assert sent.root_model.api_key == "root-secret"
    assert sent.sub_model.api_key == "sub-secret"
    assert sent.budgets.max_iterations == 4
    assert sent.budgets.timeout_seconds == 9
    assert result.metadata["sub_model"] == "sub-model"
    trajectory = Path(str(result.metadata["trajectory_path"]))
    assert trajectory.parent == tmp_path / ".code-scout/trajectories/verify"
    text = trajectory.read_text(encoding="utf-8")
    assert "root-secret" not in text
    assert "sub-secret" not in text
    assert "must-not-leak" not in text
    assert "<redacted>" in text


@pytest.mark.parametrize(
    ("response", "message"),
    (
        (RLMResponse("answer", iterations=8), "iteration budget"),
        (RLMResponse("answer", sub_calls=12), "sub-call budget"),
        (RLMResponse("x" * 2_001), "output-character budget"),
        (RLMResponse("answer", total_tokens=5_001), "total-token budget"),
    ),
)
def test_controller_rejects_every_reported_budget_overrun(
    tmp_path: Path, response: RLMResponse, message: str
) -> None:
    with pytest.raises(VerifyError, match=message):
        _engine(tmp_path, FakeRLM(response)).execute(_request(tmp_path, max_turns=7))


def test_controller_accepts_local_rlm_environment_without_docker(
    tmp_path: Path,
) -> None:
    boundary = FakeRLM(RLMResponse("no claims"))
    engine = _engine(
        tmp_path,
        boundary,
        _config(isolation="local", isolation_image=None),
    )

    result = engine.execute(_request(tmp_path))

    assert boundary.requests[0].isolation == RLMIsolation("local")
    assert result.engine.provider == "rawwerks/rlm-cli adapter"
    assert Path(str(result.metadata["trajectory_path"])).exists()


def test_rlm_cli_adapter_maps_native_constructor_completion_and_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class NativeRLM:
        def __init__(
            self,
            *,
            backend: str,
            backend_kwargs: dict[str, object],
            environment: str,
            environment_kwargs: dict[str, object],
            max_iterations: int,
            max_depth: int,
            other_backends: list[str],
            other_backend_kwargs: list[dict[str, object]],
            custom_system_prompt: str,
        ) -> None:
            captured["init"] = locals()

        def completion(self, *, prompt: object, root_prompt: str) -> object:
            captured["prompt"] = prompt
            captured["root_prompt"] = root_prompt
            answer = json.dumps(
                {
                    "answer": "diagnostic",
                    "claims": [
                        {
                            "text": "Service exists.",
                            "evidence_ids": ["evidence:1"],
                            "citations": [
                                {
                                    "path": "src/service.py",
                                    "start_line": 1,
                                    "end_line": 2,
                                }
                            ],
                            "confidence": 0.95,
                            "contradicting_evidence_ids": [],
                            "unresolved_questions": [],
                        }
                    ],
                    "unresolved_questions": [],
                    "unresolved_question": "One additional question.",
                }
            )
            usage = SimpleNamespace(
                model_usage_summaries={
                    "root": SimpleNamespace(
                        total_calls=2,
                        total_input_tokens=100,
                        total_output_tokens=20,
                    ),
                    "sub": SimpleNamespace(
                        total_calls=3,
                        total_input_tokens=60,
                        total_output_tokens=15,
                    ),
                }
            )
            return SimpleNamespace(
                response=f"Structured result follows:\n```json\n{answer}\n```",
                usage_summary=usage,
            )

    monkeypatch.setitem(sys.modules, "rlm", SimpleNamespace(RLM=NativeRLM))
    request = RLMRequest(
        question="Verify the service",
        external_context='{"evidence":[{"id":"evidence:1"}]}',
        tools={},
        root_model=RLMModelRoute(
            OpenAIModelTarget(
                "https://root.test/v1",
                "root",
                parameters={"temperature": 0.1},
            ),
            "root-key",
        ),
        sub_model=RLMModelRoute(OpenAIModelTarget("https://sub.test/v1", "sub"), None),
        isolation=RLMIsolation("local"),
        budgets=VerifyBudgets(max_iterations=5),
    )

    response = RLMCLIAdapter().run(request)

    init = captured["init"]
    assert isinstance(init, dict)
    assert init["backend"] == "openai"
    assert init["environment"] == "local"
    assert init["max_iterations"] == 5
    assert init["max_depth"] == 1
    assert "FINAL_VAR" in str(init["custom_system_prompt"])
    assert init["backend_kwargs"] == {
        "temperature": 0.1,
        "base_url": "https://root.test/v1",
        "model_name": "root",
        "api_key": "root-key",
    }
    assert init["other_backend_kwargs"] == [
        {
            "base_url": "https://sub.test/v1",
            "model_name": "sub",
            "api_key": "not-required",
        }
    ]
    assert captured["prompt"] == {"evidence": [{"id": "evidence:1"}]}
    assert "Verify the service" in str(captured["root_prompt"])
    assert response.claims[0].evidence_ids == ("evidence:1",)
    assert response.claims[0].citations == (Citation("src/service.py", 1, 2),)
    assert response.unresolved_questions == ("One additional question.",)
    assert response.total_tokens == 195
    assert response.sub_calls == 3


def test_rlm_cli_adapter_falls_back_to_structured_chat_on_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRLM:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def completion(self, **_kwargs: object) -> object:
            raise RuntimeError("provider-specific details")

    monkeypatch.setitem(sys.modules, "rlm", SimpleNamespace(RLM=BrokenRLM))
    repaired = json.dumps(
        {
            "answer": "fallback",
            "claims": [],
            "unresolved_question": "The frozen pack lacks an implementation body.",
        }
    )
    monkeypatch.setattr(
        "code_scout.engines.verify.complete_chat",
        lambda *_args, **_kwargs: SimpleNamespace(content=repaired),
    )
    route = RLMModelRoute(
        OpenAIModelTarget("http://localhost:11434/v1", "local-model"),
        None,
    )
    request = RLMRequest(
        question="Find the implementation",
        external_context='{"evidence":[]}',
        tools={},
        root_model=route,
        sub_model=route,
        isolation=RLMIsolation("local"),
        budgets=VerifyBudgets(),
    )

    response = RLMCLIAdapter().run(request)

    assert response.unresolved_questions == (
        "The frozen pack lacks an implementation body.",
    )
    assert response.events[-1] == {
        "kind": "rlm-structured-fallback",
        "reason": "rlm-error:RuntimeError",
    }


def test_controller_enforces_timeout_and_requests_boundary_cancellation(
    tmp_path: Path,
) -> None:
    class SlowRLM(FakeRLM):
        cancelled = False

        def run(self, request: RLMRequest) -> RLMResponse:
            time.sleep(0.1)
            return RLMResponse("late")

        def cancel(self) -> None:
            self.cancelled = True

    boundary = SlowRLM(RLMResponse("unused"))
    config = _config(
        timeout_seconds=0.01,
        root_model=replace(_config().root_model, timeout_seconds=0.01),
        sub_model=replace(_config().sub_model, timeout_seconds=0.01),
    )

    with pytest.raises(VerifyError, match="exceeded"):
        _engine(tmp_path, boundary, config).execute(
            _request(tmp_path, timeout_seconds=1)
        )

    assert boundary.cancelled is True


def test_output_budget_uses_actual_answer_when_provider_underreports(
    tmp_path: Path,
) -> None:
    response = RLMResponse("x" * 2_001, output_characters=1)

    with pytest.raises(VerifyError, match="output-character budget"):
        _engine(tmp_path, FakeRLM(response)).execute(_request(tmp_path))


def test_endpoint_failures_are_actionable_and_exception_text_is_redacted(
    tmp_path: Path,
) -> None:
    boundary = FakeRLM(RLMEndpointError("sub-secret echoed by endpoint"))

    with pytest.raises(VerifyError, match="endpoint failed") as caught:
        _engine(tmp_path, boundary).execute(_request(tmp_path))

    assert "sub-secret" not in str(caught.value)
    trajectory = next((tmp_path / ".code-scout/trajectories/verify").glob("*.jsonl"))
    text = trajectory.read_text(encoding="utf-8")
    assert "sub-secret" not in text
    assert "endpoint_error" in text


def test_structured_claims_are_cross_checked_and_rendered_by_evidence_strength(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def service():\n    return True\n", encoding="utf-8")
    response = RLMResponse(
        "This prose must not be trusted directly.",
        claims=(
            RLMClaim(
                "The service exists.",
                ("evidence:strong",),
                (Citation("src/service.py", 1, 2),),
                0.99,
            ),
            RLMClaim(
                "The service may be reached dynamically.",
                ("evidence:heuristic",),
                (Citation("src/service.py", 1, 2),),
                0.99,
            ),
        ),
        unresolved_questions=("Does runtime dispatch add callers?",),
    )

    result = _claim_engine(FakeRLM(response)).execute(_request(tmp_path))

    assert "Confirmed\n- The service exists." in result.answer
    assert "Inferred\n- The service may be reached dynamically." in result.answer
    assert "Question: Does runtime dispatch add callers?" in result.answer
    assert "This prose must not be trusted directly" not in result.answer
    assert result.metadata["claim_classifications"] == {
        "confirmed": 1,
        "inferred": 1,
        "contradicted": 0,
        "unresolved": 0,
    }


def test_unsupported_contradicted_and_invalid_claims_are_not_confirmed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def service():\n    return True\n", encoding="utf-8")
    response = RLMResponse(
        "structured",
        claims=(
            RLMClaim(
                "A missing source proves this.",
                ("evidence:missing",),
                (Citation("src/service.py", 1, 1),),
                1.0,
            ),
            RLMClaim(
                "The service is absent.",
                ("evidence:strong",),
                (Citation("src/service.py", 1, 2),),
                0.95,
                contradicting_evidence_ids=("evidence:contradiction",),
            ),
            RLMClaim(
                "An invalid location proves this.",
                ("evidence:strong",),
                (Citation("../escape.py", 1, 1),),
                1.0,
            ),
        ),
    )

    result = _claim_engine(FakeRLM(response)).execute(_request(tmp_path))

    assert "Contradicted\n- The service is absent." in result.answer
    assert "Unresolved\n- A missing source proves this." in result.answer
    assert "- An invalid location proves this." in result.answer
    assert "Confirmed\n- None." in result.answer
    assert {warning.code for warning in result.warnings} >= {
        "verify-claim-unsupported-evidence",
        "verify-claim-invalid-citation",
        "citation-path-traversal",
    }


def test_every_caller_answer_reports_incomplete_and_unavailable_provider_coverage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def service():\n    return True\n", encoding="utf-8")
    response = RLMResponse(
        "structured",
        claims=(
            RLMClaim(
                "One caller is stored.",
                ("relationship:caller",),
                (Citation("src/service.py", 1, 1),),
                0.95,
            ),
        ),
    )
    request = replace(_request(tmp_path), query="Locate every caller of service")

    result = _claim_engine(FakeRLM(response), truncated=True).execute(request)

    assert "Caller coverage" in result.answer
    assert "- static: incomplete" in result.answer
    assert "- lsp: incomplete" in result.answer
    assert "- dede: unavailable" in result.answer
    assert result.metadata["caller_coverage"] == {
        "static": "incomplete",
        "lsp": "incomplete",
        "dede": "unavailable",
    }


def test_response_with_no_defensible_claims_is_explicitly_unresolved(
    tmp_path: Path,
) -> None:
    response = RLMResponse(
        "An unsupported prose answer.",
        unresolved_questions=("Which implementation is active?",),
    )

    result = _claim_engine(FakeRLM(response)).execute(_request(tmp_path))

    assert "Confirmed\n- None." in result.answer
    assert "Unresolved\n- Question: Which implementation is active?" in result.answer
    assert "No defensible claims were returned." in result.answer
    assert result.citations == ()
    assert result.warnings[0].code == "verify-no-structured-claims"


@pytest.mark.parametrize("known", [True, False])
def test_verify_local_assessments_override_provider_labels(tmp_path, known):
    from code_scout.commands.cli import render_scout_result
    from code_scout.engines.citations import validate_result_citations
    from code_scout.engines.contracts import SupportingEvidence
    from code_scout.evidence.assessment import assess_legacy_evidence

    source = tmp_path / "src/service.py"
    source.parent.mkdir()
    source.write_text("def service():\n    return True\n")
    pack = _claim_pack("query")
    local = assess_legacy_evidence(
        {
            "origin": "inferred",
            "unresolved": True,
            "resolver_conflicts": ["target"],
            "freshness": "stale",
        }
    )
    pack = replace(
        pack, evidence=(replace(pack.evidence[0], assessment=local.to_dict()),)
    )
    evidence_id = "evidence:strong" if known else "invented"
    forged = SupportingEvidence(
        evidence_id,
        assess_legacy_evidence(
            {
                "origin": "extracted",
                "validation": "corroborated",
                "freshness": "fresh",
            }
        ),
    )
    response = RLMResponse(
        "validated",
        claims=(
            RLMClaim(
                "A claim",
                (evidence_id,),
                (Citation("src/service.py", 1, 2, (forged,)),),
                1.0,
            ),
        ),
    )
    engine = VerifyEngine(
        _config(),
        boundary=FakeRLM(response),
        pack_factory=lambda workspace, *, question: pack,
        tools_factory=lambda workspace, pack: FakeTools(),
        environment={"ROOT_KEY": "root-secret", "SUB_KEY": "sub-secret"},
    )
    result = validate_result_citations(engine.execute(_request(tmp_path)), tmp_path)
    expected = (
        ["inferred", "unresolved", "conflicted", "stale"] if known else ["unknown"]
    )
    assert result.metadata["supporting_evidence"][0]["assessment"]["labels"] == expected
    assert ", ".join(expected) in result.answer
    payload = json.loads(render_scout_result(result, "json"))
    if known:
        assert (
            payload["citations"][0]["supporting_evidence"][0]["assessment"]["labels"]
            == expected
        )
    else:
        assert payload["citations"] == []
    assert "validated" not in render_scout_result(result)


def test_verify_empty_response_has_no_assessment_promotion(tmp_path):
    from code_scout.commands.cli import render_scout_result

    result = _engine(tmp_path, FakeRLM(RLMResponse("validated"))).execute(
        _request(tmp_path)
    )
    assert result.metadata["supporting_evidence"] == []
    assert result.citations == ()
    assert "validated" not in render_scout_result(result)
