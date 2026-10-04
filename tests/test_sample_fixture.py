from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from code_scout.pipeline.scan import scan_workspace
from code_scout.storage import database_path

FIXTURE = Path(__file__).parent / "fixtures" / "typescript-monorepo"


def test_sample_typescript_monorepo_fixture_exercises_mvp_stack(tmp_path) -> None:
    workspace_path = tmp_path / "workspace"
    shutil.copytree(FIXTURE, workspace_path)

    result = scan_workspace(
        str(workspace_path),
        created_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
    )

    assert result.warnings == ()
    assert result.counts.files == 13
    assert result.counts.package_files == 5
    assert result.counts.repositories_detected == 1
    assert result.counts.services == 0
    assert result.counts.decisions == 1
    assert result.counts.contracts == 2
    assert result.counts.symbols == 0
    assert result.counts.concepts >= 4

    connection = sqlite3.connect(database_path(workspace_path))
    connection.row_factory = sqlite3.Row

    repository = connection.execute(
        "SELECT id, language_hints_json, framework_hints_json FROM repositories"
    ).fetchone()
    contracts = {
        row["id"]: row
        for row in connection.execute(
            """
            SELECT id, method, path, operation_id, request_dtos_json,
                   response_dtos_json, evidence_json
            FROM contracts
            ORDER BY id
            """
        ).fetchall()
    }
    symbols = {
        (row["name"], row["kind"])
        for row in connection.execute(
            "SELECT name, kind FROM symbols ORDER BY name, kind"
        ).fetchall()
    }
    decisions = connection.execute(
        "SELECT id, title, status, path FROM decisions"
    ).fetchall()
    connection.close()

    assert repository["id"] == "sample"
    assert json.loads(repository["language_hints_json"]) == [
        "JSON",
        "Markdown",
        "TypeScript",
        "YAML",
    ]
    assert json.loads(repository["framework_hints_json"]) == [
        "NestJS",
        "Next.js",
        "turbo",
    ]
    assert {
        "contract:sample:http:get:users",
        "contract:sample:http:get:users-id",
    }.issubset(contracts)
    assert contracts["contract:sample:http:get:users"]["path"] == "/users"
    assert contracts["contract:sample:http:get:users"]["operation_id"] == "listUsers"
    user_contract_evidence = json.loads(
        contracts["contract:sample:http:get:users-id"]["evidence_json"]
    )
    assert {item["extractor_name"] for item in user_contract_evidence} == {"openapi"}
    assert json.loads(
        contracts["contract:sample:http:get:users-id"]["response_dtos_json"]
    ) == ["User"]
    assert symbols == set()
    assert [dict(row) for row in decisions] == [
        {
            "id": "decision:sample:docs/adr/0001-use-modular-monorepo.md",
            "title": "Use a modular TypeScript monorepo",
            "status": "accepted",
            "path": "docs/adr/0001-use-modular-monorepo.md",
        }
    ]
