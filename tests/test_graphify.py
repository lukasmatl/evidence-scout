from __future__ import annotations

from pathlib import Path

import pytest

from code_scout.core.errors import ExtractionError
from code_scout.domain.normalize import normalize_graph_facts
from code_scout.extractors.graphify import (
    GraphifyProcessAdapter,
    GraphifyWatchEvent,
    extract_graphify,
    graphify_extraction_from_json,
    graphify_extraction_from_watch_events,
)
from code_scout.models.graph import Edge, Evidence, Symbol
from code_scout.models.workspace import RepositoryConfig, Workspace
from code_scout.workspace.config import ExtractorEntryConfig

FIXTURES = Path(__file__).parent / "fixtures" / "graphify"


def test_graphify_loads_configured_json_file(tmp_path) -> None:
    graph_file = tmp_path / ".code-scout" / "graphify.json"
    graph_file.parent.mkdir()
    graph_file.write_text(
        """
{
  "symbols": [
    {
      "id": "symbol:workspace:src/app.ts:graphify:App",
      "file_id": "file:workspace:src/app.ts",
      "name": "App",
      "kind": "function",
      "qualified_name": "App",
      "start_line": 1,
      "end_line": 3
    }
  ],
  "services": [
    {
      "id": "service:web",
      "name": "Web",
      "repository_id": "workspace"
    }
  ],
  "contracts": [
    {
      "id": "contract:workspace:http:get:app",
      "name": "GET /app",
      "kind": "http",
      "method": "GET",
      "path": "/app",
      "evidence": [
        {
          "file_path": "src/app.ts",
          "confidence": 0.9,
          "metadata": {"repository_id": "workspace"}
        }
      ]
    }
  ],
  "concepts": [{"id": "concept:web", "name": "Web"}],
  "decisions": [
    {"id": "decision:api-shape", "title": "API shape", "status": "candidate"}
  ],
  "edges": [
    {
      "source_type": "service",
      "source_id": "service:web",
      "target_type": "contract",
      "target_id": "contract:workspace:http:get:app",
      "relation_type": "serves",
      "confidence": 0.8,
      "evidence": [{"file_path": "src/app.ts"}]
    }
  ]
}
""",
        encoding="utf-8",
    )
    workspace = _workspace(tmp_path)

    extraction = extract_graphify(
        workspace,
        ExtractorEntryConfig(json_path=".code-scout/graphify.json"),
    )

    assert extraction.symbols[0].name == "App"
    assert extraction.services[0].name == "Web"
    assert extraction.contracts[0].method == "GET"
    assert extraction.contracts[0].evidence[0].extractor_name == "graphify"
    assert extraction.concepts[0].name == "Web"
    assert extraction.decisions[0].title == "API shape"
    assert extraction.edges[0].evidence[0].extractor_name == "graphify"


def test_graphify_command_uses_adapter_stdout(tmp_path) -> None:
    class RecordingAdapter:
        def __init__(self) -> None:
            self.commands: list[tuple[str, tuple[str, ...]]] = []

        def run_command(
            self,
            *,
            workspace_root: str,
            command: tuple[str, ...],
        ) -> str:
            self.commands.append((workspace_root, command))
            return '{"concepts":[{"id":"concept:graphify","name":"Graphify"}]}'

        def load_file(self, *, workspace_root: str, json_path: str) -> str:
            raise AssertionError("file mode should not be used")

    adapter = RecordingAdapter()
    workspace = _workspace(tmp_path)

    extraction = extract_graphify(
        workspace,
        ExtractorEntryConfig(command=("graphify", "--json")),
        adapter=adapter,
    )

    assert adapter.commands == [(str(tmp_path), ("graphify", "--json"))]
    assert extraction.concepts[0].id == "concept:graphify"


def test_graphify_parses_networkx_graph_json_output(tmp_path) -> None:
    source_file = tmp_path / "src" / "app.ts"
    source_file.parent.mkdir()
    source_file.write_text("export function App() { return 1; }\n", encoding="utf-8")

    extraction = graphify_extraction_from_json(
        f"""
{{
  "directed": false,
  "multigraph": false,
  "graph": {{}},
  "nodes": [
    {{
      "label": "app.ts",
      "file_type": "code",
      "source_file": "{source_file}",
      "source_location": "L1",
      "id": "tmp_src_app_ts"
    }},
    {{
      "label": "App()",
      "file_type": "code",
      "source_file": "{source_file}",
      "source_location": "L1",
      "id": "app_app"
    }}
  ],
  "links": [
    {{
      "relation": "contains",
      "source_file": "{source_file}",
      "source_location": "L1",
      "source": "tmp_src_app_ts",
      "target": "app_app",
      "confidence_score": 1.0
    }}
  ]
}}
""",
        workspace_root=str(tmp_path),
    )

    assert {
        (symbol.name, symbol.kind, symbol.file_id) for symbol in extraction.symbols
    } == {
        ("App", "function", "file:workspace:src/app.ts"),
    }
    assert (
        "file",
        "file:workspace:src/app.ts",
        "symbol",
        "symbol:workspace:src/app.ts:graphify:app",
        "declares_symbol",
    ) in {
        (
            edge.source_type,
            edge.source_id,
            edge.target_type,
            edge.target_id,
            edge.relation_type,
        )
        for edge in extraction.edges
    }


def test_graphify_skips_networkx_nodes_without_source_file(tmp_path) -> None:
    source_file = tmp_path / "src" / "app.ts"
    source_file.parent.mkdir()
    source_file.write_text("export function App() { return 1; }\n", encoding="utf-8")

    extraction = graphify_extraction_from_json(
        f"""
{{
  "nodes": [
    {{
      "label": "ControllerBase",
      "file_type": "code",
      "source_file": "",
      "source_location": "",
      "id": "controllerbase"
    }},
    {{
      "label": "App()",
      "file_type": "code",
      "source_file": "{source_file}",
      "source_location": "L1",
      "id": "app_app"
    }}
  ],
  "links": [
    {{
      "relation": "inherits",
      "source_file": "{source_file}",
      "source_location": "L1",
      "source": "app_app",
      "target": "controllerbase",
      "confidence_score": 1.0
    }}
  ]
}}
""",
        workspace_root=str(tmp_path),
    )

    assert [symbol.name for symbol in extraction.symbols] == ["App"]
    assert all(edge.target_id != "controllerbase" for edge in extraction.edges)


def test_missing_graphify_file_returns_actionable_setup_error(tmp_path) -> None:
    with pytest.raises(ExtractionError) as error:
        extract_graphify(
            _workspace(tmp_path),
            ExtractorEntryConfig(json_path=".code-scout/missing-graphify.json"),
        )

    assert "Graphify source is unavailable" in error.value.message
    assert "extractors.graphify.json_path" in (error.value.hint or "")


def test_missing_graphify_command_returns_actionable_setup_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def fail_run(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("missing")

    monkeypatch.setattr("subprocess.run", fail_run)

    with pytest.raises(ExtractionError) as error:
        GraphifyProcessAdapter().run_command(
            workspace_root=str(tmp_path),
            command=("missing-graphify", "--json"),
        )

    assert "Graphify source is unavailable" in error.value.message
    assert "extractors.graphify.command" in (error.value.hint or "")


def test_graphify_invalid_json_schema_returns_extraction_error() -> None:
    with pytest.raises(ExtractionError) as error:
        graphify_extraction_from_json('{"symbols":[{"id":"missing-fields"}]}')

    assert "Graphify output has an invalid schema" in error.value.message
    assert "symbols[].file_id" in (error.value.hint or "")


def test_graphify_watch_events_convert_through_graphify_json_path() -> None:
    extraction = graphify_extraction_from_watch_events(
        (
            GraphifyWatchEvent(
                graph_json="""
{
  "graph": {
    "symbols": [
      {
        "id": "symbol:workspace:src/app.ts:graphify:App",
        "file_id": "file:workspace:src/app.ts",
        "name": "App",
        "kind": "function"
      }
    ]
  }
}
""",
            ),
            GraphifyWatchEvent(
                graph_json="""
{
  "changed_graph": {
    "edges": [
      {
        "source_type": "file",
        "source_id": "file:workspace:src/app.ts",
        "target_type": "symbol",
        "target_id": "symbol:workspace:src/app.ts:graphify:App",
        "relation_type": "declares_symbol",
        "evidence": [{"file_path": "src/app.ts"}]
      }
    ]
  }
}
""",
            ),
        )
    )

    assert extraction.symbols[0].name == "App"
    assert extraction.edges[0].relation_type == "declares_symbol"
    assert extraction.edges[0].evidence[0].extractor_name == "graphify"


def test_graphify_normalizes_representative_graph_sections() -> None:
    extraction = graphify_extraction_from_json(
        (FIXTURES / "representative.json").read_text(encoding="utf-8")
    )

    assert {symbol.qualified_name for symbol in extraction.symbols} == {
        "UsersController",
        "UsersService",
        "users.controller",
    }
    assert extraction.services[0].id == "service:workspace:users-api"
    assert extraction.contracts[0].id == "contract:workspace:graphify:get:users-id"
    assert extraction.concepts[0].id == "concept:workspace:users"
    assert {
        (edge.source_type, edge.target_type, edge.relation_type)
        for edge in extraction.edges
    } == {
        ("file", "symbol", "declares_symbol"),
        ("file", "file", "imports"),
        ("symbol", "symbol", "calls"),
        ("service", "contract", "serves"),
    }
    assert {
        evidence.extractor_name
        for edge in extraction.edges
        for evidence in edge.evidence
    } == {"graphify"}


def test_graphify_facts_dedupe_with_lsp_without_losing_evidence() -> None:
    extraction = graphify_extraction_from_json(
        (FIXTURES / "representative.json").read_text(encoding="utf-8")
    )
    lsp_symbol = Symbol(
        id="symbol:workspace:src/users.controller.ts:lsp:userscontroller",
        file_id="file:workspace:src/users.controller.ts",
        name="UsersController",
        kind="lsp_class",
        qualified_name="UsersController",
        start_line=3,
        end_line=12,
    )
    lsp_edge = Edge(
        source_type="file",
        source_id="file:workspace:src/users.controller.ts",
        target_type="symbol",
        target_id=lsp_symbol.id,
        relation_type="declares_symbol",
        confidence=0.9,
        evidence=(
            Evidence(
                file_path="src/users.controller.ts",
                extractor_name="lsp",
                confidence=0.9,
                start_line=3,
                end_line=12,
                metadata={"repository_id": "workspace"},
            ),
        ),
    )

    normalized = normalize_graph_facts(
        symbols=(*extraction.symbols, lsp_symbol),
        services=extraction.services,
        contracts=extraction.contracts,
        concepts=extraction.concepts,
        edges=(*extraction.edges, lsp_edge),
    )

    assert [
        symbol.id for symbol in normalized.symbols if symbol.name == "UsersController"
    ] == ["symbol:workspace:src/users.controller.ts:userscontroller"]
    declares_edge = next(
        edge
        for edge in normalized.edges
        if edge.relation_type == "declares_symbol"
        and edge.target_id == "symbol:workspace:src/users.controller.ts:userscontroller"
    )
    assert declares_edge.confidence == 0.92
    assert {evidence.extractor_name for evidence in declares_edge.evidence} == {
        "graphify",
        "lsp",
    }


def _workspace(tmp_path) -> Workspace:
    return Workspace(
        id="workspace",
        name="Workspace",
        root_path=str(tmp_path),
        repositories=(
            RepositoryConfig(
                id="workspace",
                display_name="workspace",
                path=".",
            ),
        ),
    )
