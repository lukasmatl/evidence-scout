from __future__ import annotations

import sqlite3

from code_scout.domain.normalize import NormalizedGraphFacts
from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    File,
    Repository,
    Service,
    Symbol,
)
from code_scout.storage.common import _count_rows, _json_string_tuple, _json_tuple


def upsert_workspace(
    connection: sqlite3.Connection,
    *,
    id: str,
    name: str,
    root_path: str,
    schema_version: str = "1",
) -> str:
    connection.execute(
        """
        INSERT INTO workspaces (id, name, root_path, schema_version)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            root_path = excluded.root_path,
            schema_version = excluded.schema_version,
            updated_at = CURRENT_TIMESTAMP
        """,
        (id, name, root_path, schema_version),
    )
    return id


def upsert_repository(
    connection: sqlite3.Connection,
    workspace_id: str,
    repository: Repository,
) -> str:
    connection.execute(
        """
        INSERT INTO repositories (
            id,
            workspace_id,
            name,
            path,
            remote_url,
            default_branch,
            language_hints_json,
            framework_hints_json,
            module_hints_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            workspace_id = excluded.workspace_id,
            name = excluded.name,
            path = excluded.path,
            remote_url = excluded.remote_url,
            default_branch = excluded.default_branch,
            language_hints_json = excluded.language_hints_json,
            framework_hints_json = excluded.framework_hints_json,
            module_hints_json = excluded.module_hints_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            repository.id,
            workspace_id,
            repository.name,
            repository.path,
            repository.remote_url,
            repository.default_branch,
            _json_string_tuple(repository.language_hints),
            _json_string_tuple(repository.framework_hints),
            _json_string_tuple(repository.module_hints),
        ),
    )
    return repository.id


def upsert_file(connection: sqlite3.Connection, file: File) -> str:
    connection.execute(
        """
        INSERT INTO files (
            id,
            repository_id,
            path,
            kind,
            extension,
            language,
            content_hash,
            modified_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            repository_id = excluded.repository_id,
            path = excluded.path,
            kind = excluded.kind,
            extension = excluded.extension,
            language = excluded.language,
            content_hash = excluded.content_hash,
            modified_at = excluded.modified_at,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            file.id,
            file.repository_id,
            file.path,
            file.kind,
            file.extension,
            file.language,
            file.content_hash,
            file.modified_at,
        ),
    )
    return file.id


def upsert_symbol(connection: sqlite3.Connection, symbol: Symbol) -> str:
    connection.execute(
        """
        INSERT INTO symbols (
            id,
            file_id,
            name,
            kind,
            qualified_name,
            start_line,
            end_line
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            file_id = excluded.file_id,
            name = excluded.name,
            kind = excluded.kind,
            qualified_name = excluded.qualified_name,
            start_line = excluded.start_line,
            end_line = excluded.end_line,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            symbol.id,
            symbol.file_id,
            symbol.name,
            symbol.kind,
            symbol.qualified_name,
            symbol.start_line,
            symbol.end_line,
        ),
    )
    return symbol.id


def upsert_service(connection: sqlite3.Connection, service: Service) -> str:
    connection.execute(
        """
        INSERT INTO services (
            id, repository_id, name, description, aliases_json, evidence_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            repository_id = excluded.repository_id,
            name = excluded.name,
            description = excluded.description,
            aliases_json = excluded.aliases_json,
            evidence_json = excluded.evidence_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            service.id,
            service.repository_id,
            service.name,
            service.description,
            _json_string_tuple(service.aliases),
            _json_tuple(service.evidence),
        ),
    )
    return service.id


def upsert_contract(connection: sqlite3.Connection, contract: Contract) -> str:
    connection.execute(
        """
        INSERT INTO contracts (
            id,
            name,
            kind,
            path,
            version,
            method,
            producer_hint,
            operation_id,
            request_dtos_json,
            response_dtos_json,
            evidence_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            kind = excluded.kind,
            path = excluded.path,
            version = excluded.version,
            method = excluded.method,
            producer_hint = excluded.producer_hint,
            operation_id = excluded.operation_id,
            request_dtos_json = excluded.request_dtos_json,
            response_dtos_json = excluded.response_dtos_json,
            evidence_json = excluded.evidence_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            contract.id,
            contract.name,
            contract.kind,
            contract.path,
            contract.version,
            contract.method,
            contract.producer_hint,
            contract.operation_id,
            _json_string_tuple(contract.request_dtos),
            _json_string_tuple(contract.response_dtos),
            _json_tuple(contract.evidence),
        ),
    )
    return contract.id


def upsert_concept(connection: sqlite3.Connection, concept: Concept) -> str:
    connection.execute(
        """
        INSERT INTO concepts (id, name, description)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            concept.id,
            concept.name,
            concept.description,
        ),
    )
    return concept.id


def upsert_decision(connection: sqlite3.Connection, decision: Decision) -> str:
    connection.execute(
        """
        INSERT INTO decisions (
            id,
            title,
            status,
            adr_id,
            path,
            decided_at,
            context,
            decision,
            consequences,
            evidence_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            status = excluded.status,
            adr_id = excluded.adr_id,
            path = excluded.path,
            decided_at = excluded.decided_at,
            context = excluded.context,
            decision = excluded.decision,
            consequences = excluded.consequences,
            evidence_json = excluded.evidence_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            decision.id,
            decision.title,
            decision.status,
            decision.adr_id,
            decision.path,
            decision.decided_at,
            decision.context,
            decision.decision,
            decision.consequences,
            _json_tuple(decision.evidence),
        ),
    )
    return decision.id


def upsert_edge(connection: sqlite3.Connection, edge: Edge) -> int:
    cursor = connection.execute(
        """
        INSERT INTO edges (
            source_type,
            source_id,
            target_type,
            target_id,
            relation_type,
            confidence,
            evidence_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(
            source_type,
            source_id,
            target_type,
            target_id,
            relation_type
        ) DO UPDATE SET
            confidence = excluded.confidence,
            evidence_json = excluded.evidence_json,
            updated_at = CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            edge.source_type,
            edge.source_id,
            edge.target_type,
            edge.target_id,
            edge.relation_type,
            edge.confidence,
            _json_tuple(edge.evidence),
        ),
    )
    return int(cursor.fetchone()["id"])


def replace_current_graph(
    connection: sqlite3.Connection,
    workspace_id: str,
    graph: NormalizedGraphFacts,
) -> None:
    """Replace current graph tables with a resolved active-fact graph."""
    connection.execute("SAVEPOINT replace_current_graph")
    try:
        _clear_current_graph(connection)
        for repository in graph.repositories:
            upsert_repository(connection, workspace_id, repository)
        for file in graph.files:
            upsert_file(connection, file)
        for concept in graph.concepts:
            upsert_concept(connection, concept)
        for symbol in graph.symbols:
            upsert_symbol(connection, symbol)
        for decision in graph.decisions:
            upsert_decision(connection, decision)
        for edge in graph.edges:
            upsert_edge(connection, edge)
        for service in graph.services:
            upsert_service(connection, service)
        for contract in graph.contracts:
            upsert_contract(connection, contract)
        connection.execute("RELEASE SAVEPOINT replace_current_graph")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT replace_current_graph")
        connection.execute("RELEASE SAVEPOINT replace_current_graph")
        raise


def graph_summary_counts(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "files": _count_rows(connection, "files"),
        "symbols": _count_rows(connection, "symbols"),
        "services": _count_rows(connection, "services"),
        "contracts": _count_rows(connection, "contracts"),
        "concepts": _count_rows(connection, "concepts"),
        "decisions": _count_rows(connection, "decisions"),
        "edges": _count_rows(connection, "edges"),
    }


def _clear_current_graph(connection: sqlite3.Connection) -> None:
    for table_name in (
        "edges",
        "symbols",
        "files",
        "services",
        "contracts",
        "concepts",
        "decisions",
        "repositories",
    ):
        connection.execute(f"DELETE FROM {table_name}")
