from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import subprocess
from pathlib import Path

from code_scout.commands import command_path_from_invocation, options_from_invocation
from code_scout.commands.cli import RootFallbackApp
from code_scout.commands.cli import app as typer_app
from code_scout.engines import (
    Citation,
    CodeScoutRequest,
    CodeScoutResult,
    EngineMetadata,
    EngineRegistry,
    TimingMetadata,
)
from code_scout.runtime.config import (
    CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE,
    load_global_config,
)

OPERATIONS = Path("docs/operations.md")
README = Path("README.md")
CODE_SCOUT_SKILL = Path("skills/code-scout-cli/SKILL.md")
CODE_SCOUT_REFERENCE = Path("skills/code-scout-cli/REFERENCE.md")
CODE_SCOUT_CHANGE_VERIFICATION_SKILL = Path(
    "skills/code-scout-change-verification/SKILL.md"
)
CODE_SCOUT_CHANGE_VERIFICATION_AGENT = Path(
    "skills/code-scout-change-verification/agents/openai.yaml"
)
CODE_SCOUT_EVIDENCE_QUESTION_DOCS_SKILL = Path(
    "skills/code-scout-evidence-question-docs/SKILL.md"
)
CODE_SCOUT_EVIDENCE_QUESTION_DOCS_AGENT = Path(
    "skills/code-scout-evidence-question-docs/agents/openai.yaml"
)
CODE_SCOUT_PREFLIGHT_SKILL = Path("skills/code-scout-preflight/SKILL.md")
CODE_SCOUT_EVALUATE_EVIDENCE_SKILL = Path(
    "skills/code-scout-evaluate-evidence/SKILL.md"
)
CODE_SCOUT_RECOMMEND_COLLECTION_SKILL = Path(
    "skills/code-scout-recommend-collection/SKILL.md"
)
CODE_SCOUT_CODEBASE_HEALTH_AUDIT_SKILL = Path(
    "skills/code-scout-codebase-health-audit/SKILL.md"
)
CODE_SCOUT_CODEBASE_HEALTH_AUDIT_REFERENCE = Path(
    "skills/code-scout-codebase-health-audit/REFERENCE.md"
)
CODE_SCOUT_CODEBASE_HEALTH_AUDIT_REPORT_TEMPLATE = Path(
    "skills/code-scout-codebase-health-audit/REPORT-TEMPLATE.md"
)
CODE_SCOUT_CODEBASE_HEALTH_AUDIT_AGENT = Path(
    "skills/code-scout-codebase-health-audit/agents/openai.yaml"
)
CODE_SCOUT_WORKSPACE_READINESS_SKILL = Path(
    "skills/code-scout-workspace-readiness/SKILL.md"
)
CODE_SCOUT_WORKSPACE_READINESS_REFERENCE = Path(
    "skills/code-scout-workspace-readiness/REFERENCE.md"
)
CODE_SCOUT_WORKSPACE_READINESS_AGENT = Path(
    "skills/code-scout-workspace-readiness/agents/openai.yaml"
)
CODE_SCOUT_IMPACT_REVIEW_SKILL = Path("skills/code-scout-impact-review/SKILL.md")
CODE_SCOUT_IMPACT_REVIEW_REFERENCE = Path(
    "skills/code-scout-impact-review/REFERENCE.md"
)
CODE_SCOUT_IMPACT_REVIEW_AGENT = Path(
    "skills/code-scout-impact-review/agents/openai.yaml"
)
CODE_SCOUT_TEXT_SEARCH_SKILL = Path("skills/code-scout-text-search/SKILL.md")
CODE_SCOUT_TEXT_SEARCH_REFERENCE = Path("skills/code-scout-text-search/REFERENCE.md")
CODE_SCOUT_TEXT_SEARCH_AGENT = Path("skills/code-scout-text-search/agents/openai.yaml")
NO_NEW_READINESS_CLI_DOC = Path(
    "docs/evidence/no-new-preflight-evaluate-evidence-recommend-collection-skills.md"
)
CHANGE_VERIFICATION_FIXTURE = Path("tests/fixtures/change-verification")
CHANGE_VERIFICATION_WORKFLOW = Path("docs/change-verification-workflow.md")


class _DocumentedEngine:
    def __init__(self, name: str) -> None:
        self.metadata = EngineMetadata(name=name, provider=f"documented-{name}")

    def execute(self, request: CodeScoutRequest) -> CodeScoutResult:
        return CodeScoutResult(
            answer=request.query,
            citations=(Citation("example.py", 1),),
            warnings=(),
            engine=self.metadata,
            timing=TimingMetadata(0),
        )


def test_complete_documented_global_config_is_executable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    section = text.split("## Complete redacted routing example", 1)[1]
    config_text = section.split("```toml", 1)[1].split("```", 1)[0].strip() + "\n"
    home = tmp_path / "global"
    home.mkdir()
    (home / "config.toml").write_text(config_text, encoding="utf-8")
    monkeypatch.setenv(CODE_SCOUT_HOME_ENVIRONMENT_VARIABLE, str(home))
    monkeypatch.setenv("VERIFY_SUB_API_KEY", "redacted-test-secret")

    config = load_global_config()

    assert config.selection.fast == "fast"
    assert config.selection.fallbacks["fast"] == ("verify",)
    assert config.fast.model.base_url == "http://127.0.0.1:11434/v1"
    assert config.verify.root_model.base_url == "http://127.0.0.1:8000/v1"
    assert config.verify.sub_model.api_key_env == "VERIFY_SUB_API_KEY"


def test_documented_engine_query_examples_execute_through_public_cli(
    tmp_path: Path,
) -> None:
    (tmp_path / "example.py").write_text("example\n", encoding="utf-8")
    registry = EngineRegistry()
    for name in ("fast", "static", "verify"):
        registry.register(_DocumentedEngine(name))
    app = RootFallbackApp(registry)

    for engine in ("fast", "static", "verify"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = app(
                [
                    "--engine",
                    engine,
                    "--workspace",
                    str(tmp_path),
                    "--query",
                    "documented query",
                ]
            )
        assert exit_code == 0
        assert stderr.getvalue() == ""
        assert f"Resolved provider: {engine} (documented-{engine})" in stdout.getvalue()


def test_operations_guide_pins_all_operational_cli_examples() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    required = (
        "code-scout config init",
        "code-scout config path",
        "code-scout config show",
        "code-scout config validate",
        "code-scout --engine fast",
        "--engine static",
        "code-scout --engine verify",
        "code-scout init --workspace .",
        "code-scout scan --workspace .",
        "code-scout status --workspace . --format json",
        "code-scout doctor --workspace . --format json",
        "code-scout impact --workspace . --staged --format json",
        "code-scout impact --workspace . --base main",
    )

    assert all(command in text for command in required)


def test_documented_status_to_impact_examples_match_shared_parser() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    workflow = text.split("## Status-to-impact workflow", 1)[1].split(
        "## Create the global configuration", 1
    )[0]
    commands = [
        line.strip()
        for block_index, block in enumerate(workflow.split("```sh"))
        if block_index
        for line in block.split("```", 1)[0].splitlines()
        if line.strip().startswith("code-scout ")
    ]
    parsed = []
    for command in commands:
        arguments = shlex.split(command)[1:]
        parsed.append(
            (
                command_path_from_invocation(arguments),
                options_from_invocation(arguments),
            )
        )

    assert any(
        path == ("status",) and options.output_format == "json"
        for path, options in parsed
    )
    assert any(
        path == ("doctor",) and options.output_format == "json"
        for path, options in parsed
    )
    assert any(path == ("impact",) and options.staged for path, options in parsed)
    assert any(
        path == ("impact",)
        and options.base_ref == "main"
        and options.max_hops == 2
        and options.max_results == 100
        for path, options in parsed
    )


def test_status_to_impact_docs_cover_review_semantics_and_remediation() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (README, OPERATIONS)
    )
    normalized = " ".join(combined.split())

    required = (
        "merge base",
        "unstaged working-tree",
        "index-to-blob match",
        "single-repository MVP",
        "truncation.truncated",
        "test_candidates.evidence_linked",
        "test_candidates.inferred",
        "endpoint reachability is deliberately not tested",
        "Concrete remediation",
        "### Exit codes",
    )
    assert all(fragment in normalized for fragment in required)


def test_user_docs_do_not_advertise_removed_benchmark_workflow() -> None:
    combined_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (README, OPERATIONS, Path("docs/GOAL.md"))
    )

    removed_fragments = (
        "benchmark",
        "benchmarks",
        "### Reproducible engine benchmarks",
        "code-scout benchmark",
        "benchmarks/manifest.json",
        ".code-scout/benchmarks",
        "comparison.json",
        "comparison.md",
    )

    assert all(fragment not in combined_text for fragment in removed_fragments)


def test_readme_documents_static_text_and_json_query_usage() -> None:
    text = README.read_text(encoding="utf-8")
    normalized_text = " ".join(text.split())
    required = (
        'code-scout -q "implement X" --workspace . --engine static',
        ('code-scout -q "implement X" --workspace . --engine static --format json'),
        "Static text output is intended for humans choosing likely starting points",
        "Static JSON output carries the same evidence-only result",
        "machine-readable shape for orchestrator skills and agents",
    )
    normalized_required = (
        "deterministic, local-index backed, evidence-only results",
        "local, deterministic evidence before implementation",
    )

    assert all(fragment in text for fragment in required)
    assert all(fragment in normalized_text for fragment in normalized_required)


def test_readme_routes_packaged_agent_skills_and_validates_examples() -> None:
    text = README.read_text(encoding="utf-8")
    section = text.split("## Agent skill selection", 1)[1].split(
        "## Global Model Configuration", 1
    )[0]
    normalized = " ".join(section.split())
    skills = {
        "code-scout-workspace-readiness": CODE_SCOUT_WORKSPACE_READINESS_SKILL,
        "code-scout-impact-review": CODE_SCOUT_IMPACT_REVIEW_SKILL,
        "code-scout-text-search": CODE_SCOUT_TEXT_SEARCH_SKILL,
        "code-scout-change-verification": CODE_SCOUT_CHANGE_VERIFICATION_SKILL,
        "code-scout-cli": CODE_SCOUT_SKILL,
    }

    readme_links = dict(
        re.findall(r"\[`([^]]+)`\]\((skills/[^)]+/SKILL\.md)\)", section)
    )
    assert readme_links == {name: path.as_posix() for name, path in skills.items()}

    for name, path in skills.items():
        skill = path.read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1]
        assert re.search(rf"^name: {re.escape(name)}$", frontmatter, re.MULTILINE)
        assert re.search(r"^description: \S", frontmatter, re.MULTILINE)
        assert Path(readme_links[name]).is_file()
        for link in re.findall(r"\[[^]]+\]\(([^)]+)\)", skill):
            if "://" not in link:
                assert (path.parent / link).is_file()

    for skill in (
        CODE_SCOUT_WORKSPACE_READINESS_SKILL,
        CODE_SCOUT_IMPACT_REVIEW_SKILL,
        CODE_SCOUT_TEXT_SEARCH_SKILL,
        CODE_SCOUT_CHANGE_VERIFICATION_SKILL,
    ):
        agent = skill.parent / "agents/openai.yaml"
        assert agent.is_file()
        assert "interface:" in agent.read_text(encoding="utf-8")

    commands = re.findall(r"`(code-scout (?:status|doctor|impact) [^`]+)`", section)
    parsed = [
        (
            command_path_from_invocation(shlex.split(command)[1:]),
            options_from_invocation(shlex.split(command)[1:]),
        )
        for command in commands
    ]
    assert any(path == ("status",) for path, _ in parsed)
    assert any(path == ("doctor",) for path, _ in parsed)
    assert any(path == ("impact",) and options.staged for path, options in parsed)
    assert any(
        path == ("impact",) and options.base_ref == "main" for path, options in parsed
    )

    routing_expectations = (
        ("index is fresh", "code-scout-workspace-readiness"),
        ("model verification is unavailable", "code-scout-workspace-readiness"),
        ("what I have staged", "code-scout-impact-review"),
        ("branch against main", "code-scout-impact-review"),
        ("every exact reference", "code-scout-text-search"),
        ("Run the required checks for this session.", "code-scout-change-verification"),
        ("Does this old green receipt still apply?", "code-scout-change-verification"),
        ("request routing crosses modules", "code-scout-cli"),
        ("Where is `CODE_SCOUT_HOME` defined?", "No skill"),
    )
    assert all(
        prompt in section and route in section for prompt, route in routing_expectations
    )
    assert "Ralph invokes the agent selected by `agent = ...`" in normalized
    assert "does not guarantee that every Ralph agent can discover" in normalized
    assert "silently install global skills" in normalized
    assert "shipped package" in normalized
    assert "installed or discoverable" in normalized
    assert "manual discovery as unverified" in normalized


def test_change_verification_package_contract_and_documented_lifecycle_syntax() -> None:
    skill = CODE_SCOUT_CHANGE_VERIFICATION_SKILL.read_text(encoding="utf-8")
    reference = (
        CODE_SCOUT_CHANGE_VERIFICATION_SKILL.parent / "REFERENCE.md"
    ).read_text(encoding="utf-8")
    agent = CODE_SCOUT_CHANGE_VERIFICATION_AGENT.read_text(encoding="utf-8")
    fixture_readme = (CHANGE_VERIFICATION_FIXTURE / "README.md").read_text(
        encoding="utf-8"
    )
    workflow = CHANGE_VERIFICATION_WORKFLOW.read_text(encoding="utf-8")
    frontmatter = skill.split("---", 2)[1]
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", skill)

    assert "name: code-scout-change-verification" in frontmatter
    assert re.search(r"^description: \S", frontmatter, re.MULTILINE)
    assert links == ["REFERENCE.md"]
    assert all(
        (CODE_SCOUT_CHANGE_VERIFICATION_SKILL.parent / link).is_file() for link in links
    )
    assert 'display_name: "Change Verification"' in agent
    assert "$code-scout-change-verification" in agent
    assert "no index or model" in agent
    assert "does **not** establish" in reference
    assert "green `demo` profile" in fixture_readme
    assert "user-visible release note" in fixture_readme
    assert "api-check" in fixture_readme
    assert "client-check" in fixture_readme
    assert "cross-repository-contract" in fixture_readme
    assert "one execution path" in workflow

    commands: list[str] = []
    for block_index, block in enumerate(skill.split("```sh")):
        if not block_index:
            continue
        command_lines: list[str] = []
        for line in block.split("```", 1)[0].splitlines():
            stripped = line.strip().removesuffix("\\")
            if stripped.startswith("code-scout change "):
                if command_lines:
                    commands.append(" ".join(command_lines))
                command_lines = [stripped]
            elif command_lines and stripped:
                command_lines.append(stripped)
        if command_lines:
            commands.append(" ".join(command_lines))
    parsed = [
        (
            command_path_from_invocation(shlex.split(command)[1:]),
            options_from_invocation(shlex.split(command)[1:]),
        )
        for command in commands
    ]
    documented_paths = {
        path[:3] if path[:2] == ("change", "session") else path[:2]
        for path, _ in parsed
    }
    assert documented_paths == {
        ("change", "session", "begin"),
        ("change", "session", "show"),
        ("change", "show"),
        ("change", "evidence"),
        ("change", "begin"),
        ("change", "verify"),
        ("change", "finish"),
        ("change", "abandon"),
        ("change", "session", "verify"),
        ("change", "session", "finish"),
    }
    assert any(options.policy_path == "verification.toml" for _, options in parsed)
    assert any(options.attempt_id == "CHANGE_ID" for _, options in parsed)
    assert any(options.run_id == "RUN_ID" for _, options in parsed)


def test_documented_change_lifecycle_runs_in_disposable_fixture_through_both_adapters(
    tmp_path: Path,
) -> None:
    """Exercise docs examples as deterministic local CLI behavior, not host routing."""
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["sh", str(CHANGE_VERIFICATION_FIXTURE / "bootstrap.sh"), str(workspace)],
        check=True,
        capture_output=True,
        text=True,
    )

    def invoke(adapter, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = adapter(arguments)
                code = 0 if result is None else result
            except SystemExit as error:
                code = int(error.code or 0)
        return code, stdout.getvalue(), stderr.getvalue()

    root = RootFallbackApp()
    begin_session = [
        "change",
        "session",
        "begin",
        "--workspace",
        str(workspace),
        "--repo",
        "api=api",
        "--repo",
        "client=client",
        "--reference-repo",
        "schemas=schemas",
        "--policy",
        "verification.toml",
        "--profile",
        "demo",
        "--format",
        "json",
    ]
    code, output, error = invoke(root, begin_session)
    assert code == 0, error
    session = json.loads(output)

    code, output, error = invoke(
        typer_app,
        [
            "change",
            "begin",
            "A",
            "--session",
            session["session_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    attempt = json.loads(output)
    before_checkpoint = attempt["before_checkpoint_id"]
    (workspace / "api/src/legacy.txt").unlink()
    (workspace / "api/src/state.txt").write_text(
        "api=1\ndirty-before-session=yes\ncontract=trial\n", encoding="utf-8"
    )

    code, output, error = invoke(
        root,
        [
            "change",
            "verify",
            attempt["change_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 1, error
    failed_run = json.loads(output)
    assert failed_run["outcome"] == "FAILED"

    # Inspection succeeds for a failed historical run; its exit status is not a pass.
    code, output, error = invoke(
        typer_app,
        [
            "change",
            "evidence",
            failed_run["run_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    assert json.loads(output)["outcome"] == "FAILED"

    for command in (
        [
            "change",
            "show",
            "change-missing",
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
        [
            "change",
            "evidence",
            "run-missing",
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    ):
        code, _output, error = invoke(root, command)
        assert code == 1
        assert error

    (workspace / "api/src/state.txt").write_text(
        "api=1\ndirty-before-session=yes\ncontract=ready\n", encoding="utf-8"
    )
    (workspace / "client/src/state.txt").write_text(
        "client=1\ndirty-before-session=yes\ncontract=ready\n", encoding="utf-8"
    )
    # Resume retains A's original baseline rather than creating a replacement attempt.
    code, output, error = invoke(
        typer_app,
        [
            "change",
            "show",
            attempt["change_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    assert json.loads(output)["attempt"]["before_checkpoint_id"] == before_checkpoint

    code, output, error = invoke(
        root,
        [
            "change",
            "verify",
            attempt["change_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    successful_run = json.loads(output)
    assert successful_run["outcome"] == "VERIFIED"
    code, _output, error = invoke(
        typer_app,
        [
            "change",
            "finish",
            attempt["change_id"],
            "--run",
            successful_run["run_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error

    code, output, error = invoke(
        root,
        [
            "change",
            "begin",
            "B",
            "--session",
            session["session_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    second = json.loads(output)
    assert second["previous_attempt_id"] == attempt["change_id"]
    (workspace / "api/src/state.txt").write_text(
        "api=1\ndirty-before-session=yes\ncontract=ready\nfeature=b\n", encoding="utf-8"
    )
    code, output, error = invoke(
        typer_app,
        [
            "change",
            "show",
            attempt["change_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    previous_run = next(
        run
        for run in json.loads(output)["runs"]
        if run["run_id"] == successful_run["run_id"]
    )
    assert previous_run["current_applicability"]["status"] == "different"

    # A changed admitted policy rejects a new lifecycle baseline instead of adopting it.
    (workspace / "verification.toml").write_text(
        (workspace / "verification.toml").read_text(encoding="utf-8") + "\n# changed\n",
        encoding="utf-8",
    )
    code, _output, error = invoke(
        root,
        [
            "change",
            "abandon",
            second["change_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 0, error
    code, _output, error = invoke(
        typer_app,
        [
            "change",
            "begin",
            "C",
            "--session",
            session["session_id"],
            "--workspace",
            str(workspace),
            "--format",
            "json",
        ],
    )
    assert code == 1
    assert "policy source changed" in error


def test_packaged_skills_route_search_and_evidence_workflows() -> None:
    code_scout_skill = CODE_SCOUT_SKILL.read_text(encoding="utf-8")
    code_scout_reference = CODE_SCOUT_REFERENCE.read_text(encoding="utf-8")
    evidence_docs_skill = CODE_SCOUT_EVIDENCE_QUESTION_DOCS_SKILL.read_text(
        encoding="utf-8"
    )
    preflight_skill = CODE_SCOUT_PREFLIGHT_SKILL.read_text(encoding="utf-8")
    evaluate_evidence_skill = CODE_SCOUT_EVALUATE_EVIDENCE_SKILL.read_text(
        encoding="utf-8"
    )
    recommend_collection_skill = CODE_SCOUT_RECOMMEND_COLLECTION_SKILL.read_text(
        encoding="utf-8"
    )
    evidence_docs_agent = CODE_SCOUT_EVIDENCE_QUESTION_DOCS_AGENT.read_text(
        encoding="utf-8"
    )
    packaged_skill_text = "\n".join(
        (
            code_scout_skill,
            code_scout_reference,
            evidence_docs_skill,
            preflight_skill,
            evaluate_evidence_skill,
            recommend_collection_skill,
        )
    )
    normalized_cli = " ".join(code_scout_skill.split())
    normalized_reference = " ".join(code_scout_reference.split())
    normalized_docs = " ".join(evidence_docs_skill.split())
    normalized_preflight = " ".join(preflight_skill.split())
    normalized_evaluation = " ".join(evaluate_evidence_skill.split())
    normalized_recommendation = " ".join(recommend_collection_skill.split())

    assert len(code_scout_skill.splitlines()) <= 110
    assert "semantic `rg`" in normalized_cli
    assert "Use for non-trivial codebase exploration, debugging" in normalized_cli
    assert "including when some identifiers are already known" in normalized_cli
    assert "Skip only simple exact lookups" in normalized_cli
    assert "execute at least one focused `--engine static` query" in normalized_cli
    assert "Do not complete a Code Scout request with `rg` alone" in normalized_cli
    assert "Prefer tgrep for repeated broad" in normalized_cli
    assert "tgrep -F" in code_scout_skill
    assert "code-scout-text-search" in code_scout_skill
    assert "does not explain code or prove behavior" in normalized_cli
    assert "Use `--format json` only when a script or orchestrator" in normalized_cli
    assert "A no-match means only that the current scanned index" in normalized_cli
    assert "Run `fast` after the static pass" in normalized_cli
    assert "Run `verify` after the static pass" in normalized_cli
    assert "prefer `verify` for claims and impact" in normalized_cli
    assert "Before this static-first contract applies" in normalized_cli
    assert "alternative top-level route with no scan or model prerequisite" in (
        normalized_cli
    )
    assert "this skill's `verify` assesses an unresolved model-backed claim" in (
        normalized_cli
    )
    assert "Do not run a model-backed engine for a trivial exact lookup" in (
        normalized_cli
    )
    assert "task episode" not in normalized_cli
    assert "readiness report" not in normalized_cli
    assert "Evidence states" not in code_scout_skill
    assert "--timeout-seconds 300" not in code_scout_skill

    assert "hybrid of semantic embedding matches and weighted lexical matches" in (
        normalized_reference
    )
    assert "Results are limited ranked candidates, not an exhaustive grep" in (
        normalized_reference
    )
    assert "An invoked Code Scout workflow must execute or consume" in (
        normalized_reference
    )
    assert "Use `rg` for small lookups" in normalized_reference
    assert "A no-match describes this query against this scan" in normalized_reference
    assert "Do not rescan reflexively for every query" in normalized_reference
    assert "use `fast` for unresolved localization and cross-file flow tracing" in (
        normalized_reference
    )
    assert "Use `verify` for bounded behavioral claims" in normalized_reference
    assert "`--max-turns COUNT` overrides the configured request budget" in (
        normalized_reference
    )
    assert "Run a declared frozen profile" in normalized_reference
    assert "`change verify` is deterministic execution" in normalized_reference
    assert "receipt-only request" in normalized_reference
    assert "Staged/base impact remains the impact-review route" in normalized_reference

    change_verification_agent = CODE_SCOUT_CHANGE_VERIFICATION_AGENT.read_text(
        encoding="utf-8"
    )
    assert 'display_name: "Change Verification"' in change_verification_agent
    assert "no index or model" in change_verification_agent

    assert "Code Scout helps locate evidence; it is not itself the proof" in (
        normalized_docs
    )
    assert "receipt-only question" in normalized_docs
    assert "without initializing an index, scanning, or running gates" in (
        normalized_docs
    )
    assert "checkpoint/member scope" in normalized_docs
    assert "historical and current claims separately" in normalized_docs
    assert "cannot refresh that candidate identity" in normalized_docs
    assert (
        "Run at least one question-focused `--engine static` query" in normalized_docs
    )
    assert "Do not produce a Code Scout evidence document from `rg` alone" in (
        normalized_docs
    )
    assert "Combine the static pass with `rg` for exact symbols" in normalized_docs
    assert "Do not require graph inspection or a model-backed query" in normalized_docs
    assert "state the exact reason in the document" in normalized_docs
    assert "Every behavioral claim needs a concrete file and line citation" in (
        normalized_docs
    )

    assert (
        "Use when the user or orchestrator explicitly asks for Code Scout preflight"
        in (normalized_preflight)
    )
    assert "Keep it separate from ordinary repository search" in normalized_preflight
    assert "requires at least one task-focused `--engine static` query" in (
        normalized_preflight
    )
    assert "Do not satisfy Code Scout preflight with `rg` alone" in normalized_preflight
    assert "Run `init` when workspace metadata is absent" in normalized_preflight
    assert "state the exact limitation in the report" in normalized_preflight
    assert "Open at least one relevant returned file or cited range" in (
        normalized_preflight
    )
    assert "Static output remains a list of leads" in normalized_preflight
    assert "**Code Scout discovery**" in preflight_skill
    for state in (
        "Supported",
        "Partially supported",
        "Missing",
        "Unobserved",
        "Not applicable",
    ):
        assert state in preflight_skill
    assert "Ready with named risks" in normalized_preflight
    assert "session, change/attempt, and run identities" in normalized_preflight
    assert "protected-input constraints" in normalized_preflight
    assert (
        "Dirty state, uncommitted work, and overlapping sequential edits are normal"
        in (normalized_preflight)
    )
    assert "Preflight does not open, resume, rebaseline, verify, finish" in (
        normalized_preflight
    )

    assert "must consume at least one Code Scout result" in normalized_evaluation
    assert "A sufficient structured verification receipt is a Code Scout result" in (
        normalized_evaluation
    )
    assert (
        "If the bundle has neither a sufficient verification receipt nor another "
        "Code Scout result" in normalized_evaluation
    )
    assert "run one focused static query" in normalized_evaluation
    assert "do not silently evaluate only generic search evidence" in (
        normalized_evaluation
    )
    assert "Static matches are retrieval leads until their source is inspected" in (
        normalized_evaluation
    )
    assert "Contradicted" in evaluate_evidence_skill
    assert "Create a repair finding only when the bundle supports all of" in (
        normalized_evaluation
    )
    for dimension in (
        "Historical outcome",
        "Current applicability",
        "Integrity",
        "Evidence completeness",
    ):
        assert dimension in evaluate_evidence_skill
    assert "do not upgrade an" in normalized_evaluation
    assert (
        "A model explanation cannot override a deterministic gate result"
        in normalized_evaluation
    )

    assert "Recommend evidence collection, not a code change" in (
        normalized_recommendation
    )
    assert "must execute one relevant Code Scout operation" in normalized_recommendation
    assert (
        "Do not return a Code Scout collection recommendation based on `rg` alone"
        in (normalized_recommendation)
    )
    assert "Otherwise, normally use a focused static query, or `graph inspect`" in (
        normalized_recommendation
    )
    assert "read-only `change show`, `change session show`, or `change evidence`" in (
        recommend_collection_skill
    )
    assert "do not require static or graph access in this route" in (
        normalized_recommendation
    )
    assert (
        "A narrow validation command is diagnostic evidence only"
        in normalized_recommendation
    )
    assert "entire frozen profile" in normalized_recommendation
    assert "Do not recommend a repository-wide scan" in normalized_recommendation

    assert "name: code-scout-evidence-question-docs" in evidence_docs_skill
    assert "$code-scout-evidence-question-docs" in evidence_docs_agent
    assert "code-scout benchmark" not in packaged_skill_text


def test_evidence_doc_documents_prefixed_wrapper_skill_boundary() -> None:
    text = NO_NEW_READINESS_CLI_DOC.read_text(encoding="utf-8")
    normalized_text = " ".join(text.split())

    assert "## Supported Skill Entrypoints" in text
    for skill_name in (
        "code-scout-cli",
        "code-scout-preflight",
        "code-scout-evaluate-evidence",
        "code-scout-recommend-collection",
        "code-scout-evidence-question-docs",
        "code-scout-codebase-health-audit",
    ):
        assert f"`{skill_name}`" in text

    assert "routes non-trivial repository exploration through static discovery" in (
        normalized_text
    )
    assert "`fast` handles unresolved localization and cross-file tracing" in (
        normalized_text
    )
    assert "`verify` handles bounded claims" in normalized_text
    assert "specialized skills own their evidence classifications" in normalized_text
    assert "a sufficient verification receipt is such a result" in normalized_text
    assert "read-only `change show`" in text
    assert "No model result can override a deterministic gate outcome" in text
    assert "still does not add dedicated readiness CLI commands" in normalized_text
    assert "code-scout preflight" not in text
    assert "code-scout evaluate-evidence" not in text
    assert "code-scout recommend-collection" not in text


def test_packaged_codebase_health_audit_skill_contract() -> None:
    skill = CODE_SCOUT_CODEBASE_HEALTH_AUDIT_SKILL.read_text(encoding="utf-8")
    reference = CODE_SCOUT_CODEBASE_HEALTH_AUDIT_REFERENCE.read_text(encoding="utf-8")
    report_template = CODE_SCOUT_CODEBASE_HEALTH_AUDIT_REPORT_TEMPLATE.read_text(
        encoding="utf-8"
    )
    agent = CODE_SCOUT_CODEBASE_HEALTH_AUDIT_AGENT.read_text(encoding="utf-8")
    normalized_skill = " ".join(skill.split())
    normalized_reference = " ".join(reference.split())

    assert len(skill.splitlines()) <= 100
    assert "name: code-scout-codebase-health-audit" in skill
    assert "$code-scout-codebase-health-audit" in agent
    assert "Read `code-scout-cli`, ensure its index is available" in normalized_skill
    assert "Run at least one focused static query for the audit" in normalized_skill
    assert "Do not complete this skill with generic search alone" in normalized_skill
    assert "`graph inspect --format json`" in normalized_skill
    assert "the required static discovery pass" in normalized_skill
    assert "Search matches and counts are leads, not findings" in normalized_skill
    assert "CONTEXT.md" in skill
    assert "docs/adr/" in skill
    for dimension in (
        "Architecture and ownership",
        "Build and static quality",
        "Tests and safeguards",
        "Likely defects and error risk",
        "Observability and operations",
        "Dependencies and configuration",
        "Documentation and change safety",
    ):
        assert dimension in reference
    for finding_field in (
        "Observed evidence",
        "Impact",
        "Smallest owner-aligned repair",
        "Validation route",
        "Severity",
        "Confidence",
        "Recommendation strength",
        "ADR relation",
    ):
        assert finding_field in reference
    assert "Do not combine them into a numeric health score" in normalized_reference
    assert "Do not claim comprehensive security coverage" in normalized_skill
    assert "do not represent it as a completed Code Scout audit" in normalized_skill
    assert "Begin with the required focused static discovery pass" in (
        normalized_reference
    )
    assert "use `rg` and file discovery for known names and structures" in (
        normalized_reference
    )
    assert "Static output supplies ranked leads, not findings" in normalized_reference
    assert "# Codebase Health Audit: <repository>" in report_template
    assert "## Current architecture" in report_template
    assert "#### Before / after" in report_template
    assert "### Fix this first" in report_template
    assert "Retained execution receipts" in reference
    assert "receipt/run ID" in normalized_reference
    assert "checkpoint/member scope" in normalized_reference
    assert "historical required-gate outcome" in normalized_reference
    assert "current applicability" in report_template
    assert "never treat inferred tests as executed" in report_template
    assert "code-scout health-audit" not in "\n".join((skill, reference))


def test_packaged_workspace_readiness_skill_contract_and_links() -> None:
    skill = CODE_SCOUT_WORKSPACE_READINESS_SKILL.read_text(encoding="utf-8")
    reference = CODE_SCOUT_WORKSPACE_READINESS_REFERENCE.read_text(encoding="utf-8")
    agent = CODE_SCOUT_WORKSPACE_READINESS_AGENT.read_text(encoding="utf-8")
    normalized_skill = " ".join(skill.split())
    normalized_reference = " ".join(reference.split())

    frontmatter = skill.split("---", 2)[1]
    assert "name: code-scout-workspace-readiness" in frontmatter
    assert "description:" in frontmatter
    assert len(skill.splitlines()) <= 80
    assert "$code-scout-workspace-readiness" in agent
    assert 'display_name: "Workspace Readiness"' in agent

    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", skill)
    assert links == ["REFERENCE.md"]
    assert all(
        (CODE_SCOUT_WORKSPACE_READINESS_SKILL.parent / link).is_file() for link in links
    )

    for trigger in (
        "index freshness",
        "extraction coverage",
        "expected evidence is missing",
        "diagnose local setup",
    ):
        assert trigger in normalized_skill
    assert "`status` reports graph index readiness" in normalized_skill
    assert "`doctor` diagnoses configuration and local dependencies" in normalized_skill
    assert "general architecture or codebase-health audit" in normalized_skill
    assert (
        "does not automatically install, scan, initialize, repair, or probe"
        in normalized_skill
    )
    assert "run status before doctor" in normalized_skill
    assert (
        "Reuse relevant status or doctor output already collected" in normalized_skill
    )
    assert (
        "does not make local `--engine static` search unavailable" in normalized_skill
    )
    assert "Keep three observations in separate lanes" in skill
    assert "do not invent a `doctor` gate or receipt field" in normalized_skill
    assert "actual approval rules" in normalized_skill

    for state in ("fresh", "stale", "unknown", "missing", "partial"):
        assert state in normalized_reference
    assert "configuration-only" in normalized_reference
    assert "endpoint is reachable" in normalized_reference
    assert "## Separate state lanes" in reference
    assert "Verification receipt/applicability" in reference
    assert "cannot alter that receipt's candidate checkpoint" in normalized_reference
    for scenario in (
        "Fresh full scan",
        "Stale workspace",
        "Missing index",
        "Missing optional provider",
        "Old verification receipt",
        "Should not trigger",
    ):
        assert f"### {scenario}" in reference
    assert "Audit the whole repository architecture" in reference


def test_workspace_readiness_command_examples_match_shared_parser() -> None:
    reference = CODE_SCOUT_WORKSPACE_READINESS_REFERENCE.read_text(encoding="utf-8")
    commands = [
        line.strip()
        for block_index, block in enumerate(reference.split("```sh"))
        if block_index
        for line in block.split("```", 1)[0].splitlines()
        if line.strip().startswith("code-scout ")
    ]

    parsed = []
    for command in commands:
        arguments = shlex.split(command)[1:]
        path = command_path_from_invocation(arguments)
        options = options_from_invocation(arguments)
        parsed.append((path, options))

    assert len(parsed) == 4
    assert {path for path, _ in parsed} == {("status",), ("doctor",)}
    assert {options.output_format for _, options in parsed} == {"text", "json"}
    assert all(options.workspace == "." for _, options in parsed)


def test_packaged_impact_review_skill_contract_and_links() -> None:
    skill = CODE_SCOUT_IMPACT_REVIEW_SKILL.read_text(encoding="utf-8")
    reference = CODE_SCOUT_IMPACT_REVIEW_REFERENCE.read_text(encoding="utf-8")
    agent = CODE_SCOUT_IMPACT_REVIEW_AGENT.read_text(encoding="utf-8")
    normalized_skill = " ".join(skill.split())
    normalized_reference = " ".join(reference.split())

    frontmatter = skill.split("---", 2)[1]
    assert "name: code-scout-impact-review" in frontmatter
    assert "description:" in frontmatter
    assert len(skill.splitlines()) <= 100
    assert "$code-scout-impact-review" in agent
    assert 'display_name: "Change Impact Review"' in agent

    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", skill)
    assert set(links) == {"../code-scout-text-search/SKILL.md", "REFERENCE.md"}
    assert all(
        (CODE_SCOUT_IMPACT_REVIEW_SKILL.parent / link).is_file() for link in links
    )

    for trigger in (
        "staged-change",
        "branch-change",
        "retained task/session checkpoint",
        "affected callers",
        "contracts",
        "services",
        "selection of tests",
    ):
        assert trigger in normalized_skill
    assert "ordinary identifier/text lookup" in normalized_skill
    assert "whole-codebase health" in normalized_skill
    assert "Never stage files, switch branches, fetch refs" in normalized_skill
    assert "silently substitute working-tree changes" in normalized_skill
    assert "execute gates" in normalized_skill
    assert "report the exact evidence gap and stop" in normalized_skill
    assert "Reuse a current readiness result" in normalized_skill
    assert "Do not call a model merely to restate" in normalized_skill
    assert "test candidates are recommendations" in normalized_skill
    assert "cannot establish safety or exhaustive caller coverage" in normalized_skill

    for field in (
        "Comparison identity",
        "Changed seeds",
        "Affected evidence",
        "Test candidates",
        "Inspection and validation plan",
        "Limits",
    ):
        assert field in reference
    assert "weakest evidence strength/certainty" in normalized_reference
    assert "current working-tree content is outside" in normalized_reference
    assert "change show CHANGE_ID" in reference
    assert "change session show SESSION_ID" in reference
    assert "change evidence RUN_ID" in reference
    assert "before/after checkpoint IDs" in normalized_reference
    assert "aggregate or per-test execution" in normalized_reference
    for scenario in (
        "Staged-only changes",
        "Branch comparison",
        "Partially staged file",
        "Stale mappings",
        "No changes",
        "Retained task checkpoint",
        "Retained session checkpoint",
        "Missing retained evidence",
        "Should not trigger",
    ):
        assert f"### {scenario}" in reference


def test_impact_review_command_examples_match_shared_parser(tmp_path: Path) -> None:
    documents = (
        CODE_SCOUT_IMPACT_REVIEW_SKILL,
        CODE_SCOUT_IMPACT_REVIEW_REFERENCE,
    )
    commands = [
        line.strip()
        for document in documents
        for block_index, block in enumerate(
            document.read_text(encoding="utf-8").split("```sh")
        )
        if block_index
        for line in block.split("```", 1)[0].splitlines()
        if line.strip().startswith("code-scout ")
    ]

    operational_commands = [command for command in commands if " -q " not in command]
    query_commands = [command for command in commands if " -q " in command]
    parsed = []
    for command in operational_commands:
        arguments = shlex.split(command)[1:]
        path = command_path_from_invocation(arguments)
        options = options_from_invocation(arguments)
        parsed.append((path, options))

    assert len(parsed) == 12
    assert sum(path == ("status",) for path, _ in parsed) == 2
    assert sum(path == ("impact",) for path, _ in parsed) == 4
    assert sum(path[:2] == ("change", "show") for path, _ in parsed) == 2
    assert sum(path[:3] == ("change", "session", "show") for path, _ in parsed) == 2
    assert sum(path[:2] == ("change", "evidence") for path, _ in parsed) == 2
    impact_options = [options for path, options in parsed if path == ("impact",)]
    assert {options.staged for options in impact_options} == {False, True}
    assert {options.base_ref for options in impact_options} == {None, "main"}

    (tmp_path / "example.py").write_text("example\n", encoding="utf-8")
    registry = EngineRegistry()
    for name in ("fast", "static", "verify"):
        registry.register(_DocumentedEngine(name))
    app = RootFallbackApp(registry)
    executed_engines = set()
    for command in query_commands:
        arguments = shlex.split(command)[1:]
        workspace_index = arguments.index("--workspace") + 1
        arguments[workspace_index] = str(tmp_path)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = app(arguments)
        assert exit_code == 0
        assert stderr.getvalue() == ""
        engine = arguments[arguments.index("--engine") + 1]
        executed_engines.add(engine)
        assert f"Resolved provider: {engine} (documented-{engine})" in stdout.getvalue()
    assert executed_engines == {"static", "fast", "verify"}


def test_packaged_text_search_companion_skill_contract_and_links() -> None:
    skill = CODE_SCOUT_TEXT_SEARCH_SKILL.read_text(encoding="utf-8")
    reference = CODE_SCOUT_TEXT_SEARCH_REFERENCE.read_text(encoding="utf-8")
    agent = CODE_SCOUT_TEXT_SEARCH_AGENT.read_text(encoding="utf-8")
    normalized_skill = " ".join(skill.split())
    normalized_reference = " ".join(reference.split())

    frontmatter = skill.split("---", 2)[1]
    assert "name: code-scout-text-search" in frontmatter
    assert "description:" in frontmatter
    assert len(skill.splitlines()) <= 100
    assert "$code-scout-text-search" in agent
    assert 'display_name: "Code Scout Text Search"' in agent

    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", skill)
    assert links == ["REFERENCE.md"]
    assert all((CODE_SCOUT_TEXT_SEARCH_SKILL.parent / link).is_file() for link in links)

    for trigger in (
        "repeated broad exact searches",
        "large workspaces",
        "known identifier",
        "configuration key",
        "script",
    ):
        assert trigger in normalized_skill
    assert "conceptual architecture or behavior questions" in normalized_skill
    assert "small one-off lookup" in normalized_skill
    assert "If they require `rg` first, use `rg` first" in normalized_skill
    assert "installation alone does not change agent routing" in normalized_skill

    assert "tgrep retrieves literal or regex text occurrences" in normalized_skill
    assert "Code Scout `--engine static` retrieves ranked" in normalized_skill
    assert "Graph evidence can establish a recorded relationship" in normalized_skill
    assert "not a confirmed call, dependency" in normalized_skill
    assert "separately from Code Scout index freshness" in normalized_skill
    assert "Before using an indexed no-match as absence evidence" in normalized_skill
    assert "Fall back to `rg`" in normalized_skill
    assert "Do not build an index or start `tgrep serve`" in normalized_skill

    assert "official [microsoft/tgrep README]" in normalized_reference
    assert "optional tgrep 1.0.4" in normalized_reference
    assert "reported no index for this workspace" in normalized_reference
    assert "indexed query and ready-index/server result were unavailable" in (
        normalized_reference
    )
    assert "`-m` limits matches per file, not the total result set" in (
        normalized_reference
    )
    assert "adds no Code Scout runtime search backend" in normalized_reference
    assert "graph facts or writes" in normalized_reference
    assert "status/doctor integration" in normalized_reference
    assert "daemon lifecycle management" in normalized_reference
    assert "## Live, indexed, and retained bytes" in reference
    assert "retained-byte source" in normalized_reference
    assert "cannot change the receipt candidate" in normalized_reference
    assert "not a deterministic-check prerequisite" in normalized_reference
    for scenario in (
        "Ready indexing",
        "Missing executable",
        "Incomplete indexing",
        "Textual false-positive caller",
        "rg-first instruction precedence",
        "Historical checkpoint question",
    ):
        assert f"### {scenario}" in reference
