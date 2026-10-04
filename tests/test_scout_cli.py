from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from code_scout.commands.cli import RootFallbackApp, render_scout_result
from code_scout.core.errors import CodeScoutError
from code_scout.engines import (
    Citation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    EngineRegistry,
    StaticEngine,
    TimingMetadata,
)
from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.storage import open_storage


class DummyEngine:
    metadata = EngineMetadata(name="dummy", provider="test-suite")

    def __init__(self) -> None:
        self.requests: list[CodeScoutRequest] = []

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        self.requests.append(request)
        return CodeScoutResult(
            answer=f"Answer for {request.query}",
            citations=(Citation("src/router.py", 4, 8),),
            warnings=(),
            engine=self.metadata,
            timing=TimingMetadata(duration_seconds=0.01),
        )


def _registry(engine: DummyEngine, *, fast_alias: bool = True) -> EngineRegistry:
    registry = EngineRegistry()
    registry.register(engine, aliases=("fast",) if fast_alias else ())
    return registry


def _invoke(app: RootFallbackApp, args: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = app(args)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _seed_router(workspace: Path) -> None:
    router = workspace / "src" / "router.py"
    router.parent.mkdir(parents=True, exist_ok=True)
    router.write_text(
        "\n".join(f"line {line}" for line in range(1, 9)), encoding="utf-8"
    )


def _seed_empty_static_index(workspace: Path) -> None:
    _seed_router(workspace)
    with open_storage(workspace) as connection:
        connection.commit()


def test_scout_queries_current_directory_with_default_fast_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = DummyEngine()
    monkeypatch.chdir(tmp_path)
    _seed_router(tmp_path)

    exit_code, stdout, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        ["-q", "Find the router"],
    )

    assert exit_code == 0
    assert stderr == ""
    assert stdout == (
        "Answer for Find the router\n\n"
        "Resolved provider: dummy (test-suite)\n\n"
        "Citations:\n- src/router.py:4-8\n"
        "  Source: revision-unknown; freshness=unknown\n"
        "  Evidence unmapped: unknown\n"
    )
    assert len(engine.requests) == 1
    assert engine.requests[0].workspace == tmp_path.resolve()
    assert engine.requests[0].engine == "fast"


def test_scout_query_text_format_preserves_default_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = DummyEngine()
    monkeypatch.chdir(tmp_path)
    _seed_router(tmp_path)

    exit_code, stdout, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        ["-q", "Find the router", "--format", "text"],
    )

    assert exit_code == 0
    assert stderr == ""
    assert stdout == (
        "Answer for Find the router\n\n"
        "Resolved provider: dummy (test-suite)\n\n"
        "Citations:\n- src/router.py:4-8\n"
        "  Source: revision-unknown; freshness=unknown\n"
        "  Evidence unmapped: unknown\n"
    )


def test_scout_query_json_format_renders_machine_readable_result(
    tmp_path: Path,
) -> None:
    engine = DummyEngine()
    _seed_router(tmp_path)

    exit_code, stdout, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        [
            "-q",
            "Find the router",
            "--workspace",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    payload = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert payload["answer"] == "Answer for Find the router"
    assert payload["engine"] == "dummy"
    assert payload["provider"] == "test-suite"
    assert payload["citations"] == [
        json.loads(json.dumps(Citation("src/router.py", 4, 8).to_dict())),
    ]
    assert payload["warnings"] == []
    assert payload["timing"]["duration_seconds"] == 0.01


def test_scout_json_result_renders_static_machine_readable_evidence() -> None:
    result = CodeScoutResult(
        answer='Static index matches for: "Find the router"',
        citations=(Citation("src/router.py", 4, 8),),
        warnings=(),
        engine=EngineMetadata(name="static", provider="local-index"),
        timing=TimingMetadata(duration_seconds=0.01),
        metadata={
            "query": "Find the router",
            "requested_engine": "static",
            "resolved_engine": "static",
            "resolved_provider": "local-index",
            "fallback_used": False,
            "result_kind": "matched",
            "match_count": 1,
            "query_mode": "deterministic-hybrid",
            "index_path": ".code-scout/evidence.db",
            "matches": [
                {
                    "rank": 1,
                    "score": 0.8754,
                    "source_file": "src/router.py",
                    "line_range": {"start": 4, "end": 8},
                    "owner_entity": {
                        "type": "symbol",
                        "id": "symbol:route_request",
                    },
                    "text": "route_request implements request routing.",
                    "scoring_reasons": ["semantic match", "path match"],
                }
            ],
        },
    )

    payload = json.loads(render_scout_result(result, "json"))

    assert set(payload) == {
        "query",
        "engine",
        "provider",
        "likely_starting_points",
        "evidence",
        "needs_review",
        "citations",
        "warnings",
        "metadata",
    }
    assert payload["query"] == "Find the router"
    assert payload["engine"] == "static"
    assert payload["provider"] == "local-index"
    assert payload["likely_starting_points"] == [
        {
            "rank": 1,
            "evidence_id": None,
            "assessment": json.loads(json.dumps(assess_legacy_evidence({}).to_dict())),
            "score": 0.8754,
            "source_file": "src/router.py",
            "start_line": 4,
            "end_line": 8,
            "owner_entity_id": "symbol:route_request",
            "scoring_reasons": ["semantic match", "path match"],
            "evidence_text": "route_request implements request routing.",
        }
    ]
    assert payload["evidence"] == [
        {
            "rank": 1,
            "evidence_id": None,
            "assessment": json.loads(json.dumps(assess_legacy_evidence({}).to_dict())),
            "source_file": "src/router.py",
            "start_line": 4,
            "end_line": 8,
            "text": "route_request implements request routing.",
        }
    ]
    assert payload["needs_review"] == []
    assert payload["citations"] == [
        json.loads(json.dumps(Citation("src/router.py", 4, 8).to_dict()))
    ]
    assert payload["warnings"] == []
    assert payload["metadata"] == {
        "fallback_used": False,
        "gap_reason": None,
        "index_path": ".code-scout/evidence.db",
        "match_count": 1,
        "query_mode": "deterministic-hybrid",
        "requested_engine": "static",
        "resolved_engine": "static",
        "resolved_provider": "local-index",
        "result_kind": "matched",
        "timing": {"duration_seconds": 0.01, "started_at": None},
    }
    assert "answer" not in payload


def test_scout_json_result_renders_static_no_match_review_item() -> None:
    result = CodeScoutResult(
        answer='No static evidence found for: "Find the router"',
        citations=(),
        warnings=(),
        engine=EngineMetadata(name="static", provider="local-index"),
        timing=TimingMetadata(duration_seconds=0.01),
        metadata={
            "query": "Find the router",
            "requested_engine": "static",
            "resolved_engine": "static",
            "resolved_provider": "local-index",
            "fallback_used": False,
            "result_kind": "no_match",
            "gap_reason": "no_static_evidence_found",
            "match_count": 0,
            "matches": [],
            "query_mode": "deterministic-hybrid",
            "index_path": ".code-scout/evidence.db",
            "needs_review": [
                {
                    "code": "no-static-evidence-found",
                    "message": (
                        "No static evidence was found for this query in the "
                        "scanned workspace."
                    ),
                }
            ],
        },
    )

    payload = json.loads(render_scout_result(result, "json"))

    assert payload["likely_starting_points"] == []
    assert payload["evidence"] == []
    assert payload["needs_review"] == [
        {
            "code": "no-static-evidence-found",
            "message": (
                "No static evidence was found for this query in the scanned workspace."
            ),
        }
    ]
    assert payload["citations"] == []
    assert payload["warnings"] == []
    assert payload["metadata"]["result_kind"] == "no_match"
    assert payload["metadata"]["gap_reason"] == "no_static_evidence_found"
    assert "answer" not in payload


def test_scout_static_json_no_match_command_exits_successfully(
    tmp_path: Path,
) -> None:
    _seed_empty_static_index(tmp_path)
    registry = EngineRegistry()
    registry.register(StaticEngine())

    exit_code, stdout, stderr = _invoke(
        RootFallbackApp(registry),
        [
            "-q",
            "Where is request routing implemented?",
            "--engine",
            "static",
            "--workspace",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    payload = json.loads(stdout)
    assert exit_code == 0
    assert stderr == ""
    assert payload["likely_starting_points"] == []
    assert payload["evidence"] == []
    assert payload["needs_review"] == [
        {
            "code": "no-static-evidence-found",
            "message": (
                "No static evidence was found for this query in the scanned workspace."
            ),
        }
    ]
    assert payload["citations"] == []
    assert payload["warnings"] == []
    assert payload["metadata"]["result_kind"] == "no_match"
    assert payload["metadata"]["match_count"] == 0


def test_scout_text_result_renders_static_likely_starting_points() -> None:
    citations = (Citation("src/router.py", 4, 8),)
    result = CodeScoutResult(
        answer='Static index matches for: "Find the router"',
        citations=citations,
        warnings=(),
        engine=EngineMetadata(name="static", provider="local-index"),
        timing=TimingMetadata(duration_seconds=0.01),
        metadata={
            "resolved_engine": "static",
            "resolved_provider": "local-index",
            "result_kind": "matched",
            "match_count": 1,
            "matches": [
                {
                    "rank": 1,
                    "score": 0.8754,
                    "source_file": "src/router.py",
                    "line_range": {"start": 4, "end": 8},
                    "owner_entity": {
                        "type": "symbol",
                        "id": "symbol:route_request",
                    },
                    "text": "route_request implements request routing.",
                    "scoring_reasons": ["semantic match", "path match"],
                }
            ],
        },
    )

    rendered = render_scout_result(result)

    assert "Needs review" not in rendered
    assert rendered == (
        'Static index matches for: "Find the router"\n\n'
        "Resolved provider: static (local-index)\n\n"
        "Likely starting points from static evidence:\n"
        "- #1 src/router.py:4-8\n"
        "  Evidence: route_request implements request routing.\n"
        "  Labels: unknown\n"
        "  Scoring reasons: semantic match, path match; score=0.875\n\n"
        "Citations:\n"
        "- src/router.py:4-8\n"
        "  Source: revision-unknown; freshness=unknown\n"
        "  Evidence unmapped: unknown"
    )
    assert result.citations == citations


def test_scout_text_result_renders_static_needs_review_from_metadata() -> None:
    result = CodeScoutResult(
        answer='Static index matches for: "Find the router"',
        citations=(),
        warnings=(),
        engine=EngineMetadata(name="static", provider="local-index"),
        timing=TimingMetadata(duration_seconds=0.01),
        metadata={
            "resolved_engine": "static",
            "resolved_provider": "local-index",
            "result_kind": "matched",
            "match_count": 1,
            "matches": [
                {
                    "rank": 1,
                    "score": 0.8754,
                    "source_file": "src/router.py",
                    "line_range": {"start": 4, "end": 8},
                    "text": "route_request implements request routing.",
                    "scoring_reasons": ["semantic match"],
                }
            ],
            "gaps": [
                {
                    "code": "missing-callers",
                    "message": "No caller evidence was present in the static index.",
                }
            ],
            "warnings": ["Static metadata may be stale."],
        },
    )

    rendered = render_scout_result(result)

    assert rendered == (
        'Static index matches for: "Find the router"\n\n'
        "Resolved provider: static (local-index)\n\n"
        "Likely starting points from static evidence:\n"
        "- #1 src/router.py:4-8\n"
        "  Evidence: route_request implements request routing.\n"
        "  Labels: unknown\n"
        "  Scoring reasons: semantic match; score=0.875\n\n"
        "Needs review:\n"
        "- Evidence gap [missing-callers]: "
        "No caller evidence was present in the static index.\n"
        "- Warning: Static metadata may be stale."
    )


def test_scout_text_result_omits_static_starting_points_without_matches() -> None:
    result = CodeScoutResult(
        answer='No static evidence found for: "Find the router"',
        citations=(),
        warnings=(),
        engine=EngineMetadata(name="static", provider="local-index"),
        timing=TimingMetadata(duration_seconds=0.01),
        metadata={
            "resolved_engine": "static",
            "resolved_provider": "local-index",
            "result_kind": "no_match",
            "match_count": 0,
            "matches": [],
        },
    )

    rendered = render_scout_result(result)

    assert "Likely starting points" not in rendered
    assert rendered == (
        'No static evidence found for: "Find the router"\n'
        "Refresh static evidence with `code-scout scan --workspace <path>` "
        "before relying on this result.\n\n"
        "Resolved provider: static (local-index)"
    )


def test_scout_queries_an_alternate_workspace_with_explicit_engine(
    tmp_path: Path,
) -> None:
    engine = DummyEngine()
    workspace = tmp_path / "target"
    workspace.mkdir()
    _seed_router(workspace)

    exit_code, _, stderr = _invoke(
        RootFallbackApp(_registry(engine, fast_alias=False)),
        [
            "--query",
            "Find every caller",
            "--engine",
            "dummy",
            "--workspace",
            str(workspace),
        ],
    )

    assert exit_code == 0
    assert stderr == ""
    assert engine.requests[0].workspace == workspace.resolve()
    assert engine.requests[0].engine == "dummy"


def test_scout_accepts_a_request_timeout_override(tmp_path: Path) -> None:
    engine = DummyEngine()

    exit_code, _, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        [
            "-q",
            "Find the router",
            "--workspace",
            str(tmp_path),
            "--timeout-seconds",
            "300",
        ],
    )

    assert exit_code == 0
    assert stderr == ""
    assert engine.requests[0].runtime.timeout_seconds == 300


def test_scout_accepts_a_request_turn_budget_override(tmp_path: Path) -> None:
    engine = DummyEngine()

    exit_code, _, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        [
            "-q",
            "Find the router",
            "--workspace",
            str(tmp_path),
            "--max-turns",
            "20",
        ],
    )

    assert exit_code == 0
    assert stderr == ""
    assert engine.requests[0].runtime.max_turns_override == 20


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "the following arguments are required: -q/--query"),
        (["-q", "   "], "Query must not be empty."),
        (
            ["-q", "Find the router", "--workspace", "missing"],
            "Workspace does not exist: missing.",
        ),
        (
            ["-q", "Find the router", "--engine", "missing"],
            "Unknown engine: missing.",
        ),
        (
            ["-q", "Find the router", "--max-turns", "101"],
            "argument --max-turns: must be between 1 and 100",
        ),
        (
            ["-q", "Find the router", "--format", "yaml"],
            "argument --format: invalid choice: 'yaml'",
        ),
    ],
)
def test_scout_reports_invalid_inputs_without_tracebacks(
    tmp_path: Path,
    args: list[str],
    message: str,
) -> None:
    engine = DummyEngine()

    exit_code, stdout, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        args,
    )

    assert exit_code != 0
    assert stdout == ""
    assert message in stderr
    assert "Traceback" not in stderr


def test_scout_reports_engine_failures_without_tracebacks(tmp_path: Path) -> None:
    class FailingEngine(DummyEngine):
        def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
            raise RuntimeError("provider stopped")

    engine = FailingEngine()

    exit_code, stdout, stderr = _invoke(
        RootFallbackApp(_registry(engine)),
        ["-q", "Find the router", "--workspace", str(tmp_path)],
    )

    assert exit_code == 1
    assert stdout == ""
    assert "Error: Engine 'fast' failed: provider stopped" in stderr
    assert "Traceback" not in stderr


def test_scout_preserves_actionable_expected_engine_errors(tmp_path: Path) -> None:
    class FailingEngine(DummyEngine):
        def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
            raise CodeScoutError("Engine rejected the request.", "Try another query.")

    exit_code, _, stderr = _invoke(
        RootFallbackApp(_registry(FailingEngine())),
        ["-q", "Find the router", "--workspace", str(tmp_path)],
    )

    assert exit_code == 1
    assert stderr == ("Error: Engine rejected the request.\nHint: Try another query.\n")
