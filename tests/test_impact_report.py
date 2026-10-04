from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from code_scout.core.errors import CodeScoutError, CommandUsageError
from code_scout.impact.report import generate_impact_report
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.pipeline.scan import scan_workspace
from code_scout.storage import database_path, open_storage, upsert_active_facts
from code_scout.workspace.status import WorkspaceStatusReport


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "code-scout@example.test")
    _git(repository, "config", "user.name", "Code Scout Test")
    return repository


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "--all")
    _git(repository, "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _status(repository: Path, freshness: str) -> WorkspaceStatusReport:
    return WorkspaceStatusReport(
        workspace={
            "id": "workspace",
            "root_path": str(repository),
            "repositories": [{"id": "workspace", "path": "."}],
        },
        index={"state": "ready", "database_path": str(database_path(repository))},
        freshness={
            "state": freshness,
            "coverage": "complete",
            "coverage_reasons": (
                [] if freshness == "fresh" else [{"message": "content changed"}]
            ),
        },
        changes={},
        coverage={
            "grammar_support": [{"language": "Python", "state": "observed"}],
            "extractors": {
                "states": [{"name": "symbol_extraction", "state": "observed"}]
            },
            "relationship_extraction": {
                "state": "observed",
                "relationship_count": 1,
            },
        },
    )


def _file_fact(path: str, content: str) -> ActiveFact:
    return ActiveFact(
        id=f"fact:file:{path}",
        extractor="file_discovery",
        kind="file",
        source_file=path,
        identity={"entity_type": "file", "entity_id": f"file:workspace:{path}"},
        properties={
            "repository_id": "workspace",
            "path": path,
            "kind": "source",
            "extension": ".py",
            "language": "Python",
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        },
        evidence=(FactEvidence(path, "file_discovery", 1.0, 1, None),),
        confidence=1.0,
    )


def _symbol_fact(path: str, name: str) -> ActiveFact:
    return ActiveFact(
        id=f"fact:symbol:{name}",
        extractor="tree_sitter",
        kind="symbol",
        source_file=path,
        identity={
            "entity_type": "symbol",
            "entity_id": f"symbol:workspace:{name}",
        },
        properties={
            "file_id": f"file:workspace:{path}",
            "name": name,
            "kind": "function",
            "qualified_name": name,
            "start_line": 1,
            "end_line": 2,
        },
        evidence=(FactEvidence(path, "tree_sitter", 0.9, 1, 2),),
        confidence=0.9,
    )


def _seed_cited_index(repository: Path, changed_content: str) -> None:
    caller_content = "def caller():\n    return changed()\n"
    facts = [
        _file_fact("changed.py", changed_content),
        _symbol_fact("changed.py", "changed"),
        _file_fact("caller.py", caller_content),
        _symbol_fact("caller.py", "caller"),
        ActiveFact(
            id="fact:relationship:caller-changed",
            extractor="lsp",
            kind="relationship",
            source_file="caller.py",
            identity={
                "subject": {"type": "symbol", "id": "symbol:workspace:caller"},
                "relation": "calls",
                "object": {"type": "symbol", "id": "symbol:workspace:changed"},
            },
            properties={},
            evidence=(FactEvidence("caller.py", "lsp", 0.95, 2, 2),),
            confidence=0.95,
        ),
    ]
    with open_storage(repository) as connection:
        upsert_active_facts(connection, facts)
        connection.commit()


@pytest.mark.parametrize("freshness", ["fresh", "stale"])
def test_staged_report_contains_versioned_cited_findings_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    freshness: str,
) -> None:
    repository = _repository(tmp_path)
    changed = repository / "changed.py"
    changed.write_text("def changed():\n    return 1\n", encoding="utf-8")
    (repository / "caller.py").write_text(
        "def caller():\n    return changed()\n", encoding="utf-8"
    )
    _commit(repository, "initial")
    new_content = "def changed():\n    return 2\n"
    changed.write_text(new_content, encoding="utf-8")
    _git(repository, "add", "changed.py")
    _seed_cited_index(repository, new_content)
    monkeypatch.setattr(
        "code_scout.impact.report.workspace_status",
        lambda _workspace: _status(repository, freshness),
    )
    index_mtime = database_path(repository).stat().st_mtime_ns

    report = generate_impact_report(
        repository,
        staged=True,
        max_hops=1,
        max_results=1,
    )
    payload = json.loads(report.render("json"))
    text = report.render("text")

    assert payload["schema_version"] == "1"
    assert payload["comparison"]["mode"] == "staged"
    assert payload["index"]["freshness"]["state"] == freshness
    assert payload["changed_files"][0]["mapping_status"] == "mapped"
    assert payload["changed_symbols"][0]["name"] == "changed"
    assert payload["affected_entities"][0]["name"] == "caller"
    assert payload["paths"][0]["citations"][0] == {
        "repository_id": "workspace",
        "path": "caller.py",
        "start_line": 2,
        "end_line": 2,
    }
    assert payload["truncation"] == {
        "truncated": False,
        "max_hops": 1,
        "max_results": 1,
    }
    assert "Citation: caller.py:2" in text
    if freshness == "stale":
        assert "Limitation:" in text
    assert database_path(repository).stat().st_mtime_ns == index_mtime


def test_base_report_records_requested_and_resolved_comparison(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    changed = repository / "changed.py"
    changed.write_text("def changed():\n    return 1\n", encoding="utf-8")
    base_commit = _commit(repository, "base")
    new_content = "def changed():\n    return 2\n"
    changed.write_text(new_content, encoding="utf-8")
    _commit(repository, "head")
    _seed_cited_index(repository, new_content)
    monkeypatch.setattr(
        "code_scout.impact.report.workspace_status",
        lambda _workspace: _status(repository, "fresh"),
    )

    payload = generate_impact_report(repository, base_ref=base_commit).to_dict()

    assert payload["comparison"]["mode"] == "base_ref"
    assert payload["comparison"]["requested_base_ref"] == base_commit
    assert payload["comparison"]["resolved_base_commit"] == base_commit


def test_scanned_staged_report_includes_contract_consumers_without_writes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / ".gitignore").write_text(".code-scout/\n", encoding="utf-8")
    changed = repository / "contract.py"
    changed.write_text("ORDER_VERSION = 1\n", encoding="utf-8")
    (repository / "client.py").write_text("ORDER_CLIENT = True\n", encoding="utf-8")
    _commit(repository, "initial")

    config_path = repository / ".code-scout" / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
workspace:
  id: workspace
  name: Contract consumer fixture
repositories:
  - id: workspace
    path: .
extractors:
  tree_sitter:
    enabled: false
  lsp:
    enabled: false
  graphify:
    enabled: true
    json_path: .code-scout/impact-graph.json
  adr:
    enabled: false
  docs:
    enabled: false
  infra:
    enabled: false
  openapi:
    enabled: false
  asyncapi:
    enabled: false
  package_metadata:
    enabled: false
  symbol_extraction:
    enabled: false
""".lstrip(),
        encoding="utf-8",
    )
    graph = {
        "services": [
            {
                "id": "service:workspace:consumer",
                "name": "Consumer Service",
                "repository_id": "workspace",
                "evidence": [
                    {
                        "file_path": "client.py",
                        "start_line": 1,
                        "end_line": 1,
                        "confidence": 0.8,
                    }
                ],
            }
        ],
        "contracts": [
            {
                "id": "contract:workspace:orders",
                "name": "Orders",
                "kind": "http",
                "path": "/orders",
                "evidence": [
                    {
                        "file_path": "contract.py",
                        "start_line": 1,
                        "end_line": 1,
                        "confidence": 0.9,
                    }
                ],
            }
        ],
        "edges": [
            {
                "source_type": "file",
                "source_id": "file:workspace:contract.py",
                "target_type": "contract",
                "target_id": "contract:workspace:orders",
                "relation_type": "declares_contract",
                "confidence": 0.9,
                "evidence": [
                    {
                        "file_path": "contract.py",
                        "start_line": 1,
                        "end_line": 1,
                        "confidence": 0.9,
                    }
                ],
            },
            {
                "source_type": "service",
                "source_id": "service:workspace:consumer",
                "target_type": "contract",
                "target_id": "contract:workspace:orders",
                "relation_type": "consumes",
                "confidence": 0.8,
                "evidence": [
                    {
                        "file_path": "client.py",
                        "start_line": 1,
                        "end_line": 1,
                        "confidence": 0.8,
                    }
                ],
            },
            {
                "source_type": "file",
                "source_id": "file:workspace:client.py",
                "target_type": "contract",
                "target_id": "contract:workspace:orders",
                "relation_type": "consumes",
                "confidence": 0.8,
                "evidence": [
                    {
                        "file_path": "client.py",
                        "start_line": 1,
                        "end_line": 1,
                        "confidence": 0.8,
                    }
                ],
            },
        ],
    }
    (repository / ".code-scout" / "impact-graph.json").write_text(
        json.dumps(graph), encoding="utf-8"
    )
    scan_workspace(str(repository))

    with open_storage(repository) as connection:
        connection.execute("UPDATE snapshots SET metadata_json = '{}' ")
        connection.commit()

    changed.write_text("ORDER_VERSION = 2\n", encoding="utf-8")
    _git(repository, "add", "contract.py")
    git_before = (
        _git(repository, "status", "--porcelain=v1"),
        _git(repository, "rev-parse", "HEAD"),
        (repository / ".git" / "index").read_bytes(),
    )
    database_before = database_path(repository).read_bytes()

    payload = json.loads(
        generate_impact_report(
            repository, staged=True, max_hops=2, max_results=17
        ).render("json")
    )

    assert payload["affected_services"][0]["affected_entity"]["id"] == (
        "service:workspace:consumer"
    )
    assert "file:workspace:client.py" in {
        item["id"] for item in payload["affected_entities"]
    }
    service_path = payload["affected_services"][0]
    assert [step["relation"] for step in service_path["relationships"]] == [
        "declares_contract",
        "consumes",
    ]
    assert service_path["relationships"][1]["source"]["id"] == (
        "service:workspace:consumer"
    )
    assert service_path["relationships"][1]["target"]["id"] == (
        "contract:workspace:http:unknown:orders"
    )
    assert service_path["extractors"] == ["graphify"]
    assert payload["index"]["freshness"]["state"] == "stale"
    assert payload["index"]["freshness"]["coverage"] == "unknown"
    assert payload["index"]["freshness"]["coverage_reasons"][0]["code"] == (
        "scan_scope_unknown"
    )
    assert (
        "Historical scan metadata does not identify the extraction scope."
        in service_path["freshness_limitations"]
    )
    assert payload["category_coverage"]["services"]["state"] == "observed"
    assert "tests_relationship_coverage_unavailable" in {
        gap["code"] for gap in payload["coverage_gaps"]
    }
    assert payload["truncation"] == {
        "truncated": False,
        "max_hops": 2,
        "max_results": 17,
    }
    assert database_path(repository).read_bytes() == database_before
    assert (
        _git(repository, "status", "--porcelain=v1"),
        _git(repository, "rev-parse", "HEAD"),
        (repository / ".git" / "index").read_bytes(),
    ) == git_before


def test_empty_comparison_succeeds_and_reports_no_changes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "app.py").write_text("value = 1\n", encoding="utf-8")
    _commit(repository, "initial")

    report = generate_impact_report(repository, staged=True)

    assert report.to_dict()["changed_files"] == []
    assert "No changes detected" in report.render("text")


def test_changed_comparison_requires_a_readable_index(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    source = repository / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    _commit(repository, "initial")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(repository, "add", "app.py")

    with pytest.raises(CodeScoutError, match="index is unavailable") as caught:
        generate_impact_report(repository, staged=True)

    assert "code-scout init" in (caught.value.hint or "")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "exactly one"),
        ({"staged": True, "base_ref": "main"}, "exactly one"),
        ({"staged": True, "max_hops": 0}, "max-hops"),
        ({"staged": True, "max_results": 1001}, "max-results"),
    ],
)
def test_request_validation_is_a_usage_error(
    tmp_path: Path,
    arguments: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(CommandUsageError, match=message):
        generate_impact_report(tmp_path, **arguments)
