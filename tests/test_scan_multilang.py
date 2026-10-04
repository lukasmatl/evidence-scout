from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from code_scout.pipeline.scan import scan_workspace
from code_scout.storage import database_path
from code_scout.workspace.config import create_workspace_config


def test_multilanguage_scan_persists_languages_symbols_edges_and_exports(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    _write_multilanguage_tier_fixtures(tmp_path)

    result = scan_workspace(
        str(tmp_path),
        created_at=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )

    connection = sqlite3.connect(database_path(tmp_path))
    connection.row_factory = sqlite3.Row
    file_rows = connection.execute(
        """
        SELECT path, kind, extension, language
        FROM files
        WHERE path IN (?, ?, ?, ?)
        ORDER BY path
        """,
        (
            "code/mob.dm",
            "pages/index.razor",
            "scripts/tool.ps1",
            "src/sample.py",
        ),
    ).fetchall()
    symbol_count = connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    registry_edge_rows = connection.execute(
        """
        SELECT relation_type, evidence_json
        FROM edges
        WHERE EXISTS (
            SELECT 1
            FROM json_each(edges.evidence_json)
            WHERE json_extract(json_each.value, '$.extractor_name')
                  IN ('byond', 'dotnet', 'heuristic', 'tree_sitter')
        )
        ORDER BY relation_type
        """,
    ).fetchall()
    connection.close()

    assert result.counts.files == 4
    assert [dict(row) for row in file_rows] == [
        {
            "path": "code/mob.dm",
            "kind": "source",
            "extension": ".dm",
            "language": "DM",
        },
        {
            "path": "pages/index.razor",
            "kind": "source",
            "extension": ".razor",
            "language": "Razor",
        },
        {
            "path": "scripts/tool.ps1",
            "kind": "source",
            "extension": ".ps1",
            "language": "PowerShell",
        },
        {
            "path": "src/sample.py",
            "kind": "source",
            "extension": ".py",
            "language": "Python",
        },
    ]
    assert symbol_count > 0
    assert registry_edge_rows
    assert {
        evidence["extractor_name"]
        for row in registry_edge_rows
        for evidence in json.loads(row["evidence_json"])
    } & {"dotnet", "byond"}


def _write_multilanguage_tier_fixtures(tmp_path) -> None:
    fixtures = {
        "src/sample.py": "class Sample:\n    pass\n",
        "scripts/tool.ps1": "function Invoke-Tool {\n}\n",
        "pages/index.razor": "\n".join(
            [
                '@page "/users"',
                "@model Sample.Pages.UsersModel",
                "@inject Sample.Services.UserService UserService",
                "<h1>Users</h1>",
                "",
            ],
        ),
        "code/mob.dm": "\n".join(
            [
                "/mob/living/carbon",
                '    name = "carbon mob"',
                "    proc/breathe()",
                "",
            ],
        ),
    }

    for relative_path, source in fixtures.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
