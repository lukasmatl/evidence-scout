from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Row
from typing import Any

from code_scout.exporters.output_warnings import (
    OutputReviewWarning,
    resolver_conflict_warnings,
)
from code_scout.storage import open_storage


@dataclass(frozen=True)
class OutputGraphData:
    repositories: tuple[dict[str, Any], ...]
    files: tuple[dict[str, Any], ...]
    services: tuple[dict[str, Any], ...]
    contracts: tuple[dict[str, Any], ...]
    concepts: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    symbols: tuple[dict[str, Any], ...] = ()

    @property
    def warnings(self) -> tuple[OutputReviewWarning, ...]:
        return resolver_conflict_warnings(
            node_rows=(
                ("service", self.services),
                ("contract", self.contracts),
                ("decision", self.decisions),
            ),
            edges=self.edges,
        )

    @property
    def needs_review(self) -> tuple[dict[str, Any], ...]:
        return tuple(warning.to_dict() for warning in self.warnings)


def load_docs_graph(workspace_root: Path) -> OutputGraphData:
    with open_storage(workspace_root) as connection:
        return OutputGraphData(
            repositories=_rows(
                connection.execute(
                    """
                    SELECT id, name, path, remote_url, default_branch,
                           language_hints_json, framework_hints_json
                    FROM repositories
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            files=_rows(
                connection.execute(
                    """
                    SELECT id, repository_id, path, kind, extension, language
                    FROM files
                    ORDER BY path, id
                    """,
                ).fetchall(),
            ),
            services=_rows(
                connection.execute(
                    """
                    SELECT id, repository_id, name, description, aliases_json,
                           evidence_json
                    FROM services
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            contracts=_rows(
                connection.execute(
                    """
                    SELECT id, name, kind, path, version, method, producer_hint,
                           operation_id, request_dtos_json, response_dtos_json,
                           evidence_json
                    FROM contracts
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            concepts=_rows(
                connection.execute(
                    """
                    SELECT id, name, description
                    FROM concepts
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            decisions=_rows(
                connection.execute(
                    """
                    SELECT id, title, status, adr_id, path, decided_at, context,
                           decision, consequences, evidence_json
                    FROM decisions
                    ORDER BY title, id
                    """,
                ).fetchall(),
            ),
            edges=_load_edges(connection),
        )


def load_fastcontext_graph(workspace_root: Path) -> OutputGraphData:
    with open_storage(workspace_root) as connection:
        return OutputGraphData(
            repositories=_rows(
                connection.execute(
                    """
                    SELECT id, name, path, remote_url, default_branch,
                           language_hints_json, framework_hints_json
                    FROM repositories
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            files=_rows(
                connection.execute(
                    """
                    SELECT id, repository_id, path, kind, extension, language
                    FROM files
                    ORDER BY path, id
                    """,
                ).fetchall(),
            ),
            symbols=_rows(
                connection.execute(
                    """
                    SELECT id, file_id, name, kind, qualified_name,
                           start_line, end_line
                    FROM symbols
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            services=_rows(
                connection.execute(
                    """
                    SELECT id, repository_id, name, description, aliases_json
                    FROM services
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            contracts=_rows(
                connection.execute(
                    """
                    SELECT id, name, kind, path, version, method, producer_hint,
                           operation_id, request_dtos_json, response_dtos_json,
                           evidence_json
                    FROM contracts
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            concepts=_rows(
                connection.execute(
                    """
                    SELECT id, name, description
                    FROM concepts
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            decisions=_rows(
                connection.execute(
                    """
                    SELECT id, title, status, adr_id, path, decided_at, context,
                           decision, consequences, evidence_json
                    FROM decisions
                    ORDER BY title, id
                    """,
                ).fetchall(),
            ),
            edges=_load_edges(connection),
        )


def load_okf_graph(workspace_root: Path) -> OutputGraphData:
    with open_storage(workspace_root) as connection:
        return OutputGraphData(
            repositories=_rows(
                connection.execute(
                    """
                    SELECT id, workspace_id, name, path, remote_url, default_branch,
                           language_hints_json, framework_hints_json,
                           module_hints_json
                    FROM repositories
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            files=_rows(
                connection.execute(
                    """
                    SELECT id, repository_id, path, kind, extension
                    FROM files
                    ORDER BY repository_id, path
                    """,
                ).fetchall(),
            ),
            services=_rows(
                connection.execute(
                    """
                    SELECT id, repository_id, name, description, aliases_json,
                           evidence_json
                    FROM services
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            contracts=_rows(
                connection.execute(
                    """
                    SELECT id, name, kind, path, version, method, producer_hint,
                           operation_id, request_dtos_json, response_dtos_json,
                           evidence_json
                    FROM contracts
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            decisions=_rows(
                connection.execute(
                    """
                    SELECT id, title, status, adr_id, path, decided_at, context,
                           decision, consequences, evidence_json
                    FROM decisions
                    ORDER BY title, id
                    """,
                ).fetchall(),
            ),
            concepts=_rows(
                connection.execute(
                    """
                    SELECT id, name, description
                    FROM concepts
                    ORDER BY name, id
                    """,
                ).fetchall(),
            ),
            edges=_load_edges(connection),
        )


def _load_edges(connection: Any) -> tuple[dict[str, Any], ...]:
    return _rows(
        connection.execute(
            """
            SELECT source_type, source_id, target_type, target_id,
                   relation_type, confidence, evidence_json
            FROM edges
            ORDER BY source_type, source_id, relation_type, target_type, target_id
            """,
        ).fetchall(),
    )


def _rows(rows: list[Row]) -> tuple[dict[str, Any], ...]:
    return tuple(_decode_json_columns(dict(row)) for row in rows)


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key, value in row.items():
        if key.endswith("_json"):
            decoded[key.removesuffix("_json")] = json.loads(value)
            del decoded[key]
    return decoded
