from __future__ import annotations

import json

import pytest

from code_scout.evidence.navigation import CodeScoutSymbolNavigator
from code_scout.evidence.pack import (
    EvidencePack,
    PackedRelationship,
    PackedSourceProvenance,
    RepositoryMapEntry,
)
from code_scout.evidence.serena import (
    SerenaConfig,
    SerenaMCPError,
    SerenaSymbolNavigator,
    configured_symbol_navigator,
)
from code_scout.evidence.tools import EvidenceTools
from code_scout.workspace.config import load_workspace_config


def test_definition_precedence_retains_all_extractor_provenance(tmp_path) -> None:
    navigator = CodeScoutSymbolNavigator(_pack(tmp_path))

    result = navigator.definitions("symbol:service")

    assert result.status == "confirmed"
    assert result.candidates[0].extractor == "lsp"
    assert [source.extractor for source in result.candidates[0].provenance] == [
        "lsp",
        "graphify",
        "tree_sitter",
    ]
    assert (result.candidates[0].start_line, result.candidates[0].end_line) == (
        1,
        2,
    )


def test_ambiguous_definitions_are_all_preserved(tmp_path) -> None:
    navigator = CodeScoutSymbolNavigator(_pack(tmp_path))

    result = navigator.definitions("handle")

    assert result.status == "ambiguous"
    assert [candidate.symbol_id for candidate in result.candidates] == [
        "symbol:handler:a",
        "symbol:handler:b",
    ]


def test_references_cover_lsp_graphify_and_lexical_facts(tmp_path) -> None:
    navigator = CodeScoutSymbolNavigator(_pack(tmp_path))

    result = navigator.references("symbol:service")

    assert [candidate.extractor for candidate in result.candidates] == [
        "lsp",
        "graphify",
        "lexical",
    ]
    assert [candidate.neighbor_id for candidate in result.candidates] == [
        "symbol:handler:a",
        "symbol:handler:b",
        "symbol:caller",
    ]
    assert result.coverage == {
        "lsp": "available",
        "graphify": "available",
        "tree_sitter": "unavailable",
        "lexical": "available",
    }


def test_implementations_callers_and_callees_use_navigator_in_verify_tools(
    tmp_path,
) -> None:
    pack = _pack(tmp_path)
    tools = EvidenceTools(tmp_path, pack)

    implementations = tools.find_implementations("symbol:service")
    callers = tools.list_callers("symbol:service")
    callees = tools.list_callees("symbol:handler:a")

    assert implementations["items"][0]["neighbor_id"] == "symbol:handler:b"
    assert callers["items"][0]["neighbor_id"] == "symbol:handler:a"
    assert callees["items"][0]["neighbor_id"] == "symbol:service"
    assert implementations["provenance"]["provider"] == "code-scout"
    json.dumps((implementations, callers, callees))


def test_unsupported_lsp_language_gracefully_uses_existing_graph(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("navigation must not start a language server")
        ),
    )
    pack = _pack(tmp_path, include_lsp=False)
    result = CodeScoutSymbolNavigator(pack).definitions("symbol:service")

    assert result.status == "confirmed"
    assert result.candidates[0].extractor == "graphify"
    assert result.coverage["lsp"] == "unavailable"
    assert result.warnings[0]["code"] == "language-server-evidence-unavailable"
    assert "without starting a language server" in result.warnings[0]["message"]


def test_missing_ranges_are_reported_without_guessing(tmp_path) -> None:
    pack = _pack(tmp_path)
    missing = RepositoryMapEntry(
        "symbol",
        "symbol:missing-range",
        "src/app.py",
        "missingRange",
        start_line=None,
        end_line=None,
        source_provenance=(
            PackedSourceProvenance("src/app.py", None, None, "graphify", 0.9),
        ),
    )
    navigator = CodeScoutSymbolNavigator(
        EvidencePack(
            query=pack.query,
            evidence=pack.evidence,
            relationships=pack.relationships,
            repository_map=(*pack.repository_map, missing),
            needs_review=pack.needs_review,
            context=pack.context,
            truncated=pack.truncated,
        )
    )

    result = navigator.definitions("symbol:missing-range")

    assert result.status == "unavailable"
    assert result.candidates == ()
    assert result.warnings[0]["code"] == "navigation-range-unavailable"


class _FakeSerenaTransport:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.calls = []

    def call_tool(self, name, arguments, timeout):
        self.calls.append((name, arguments, timeout))
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        return response


def _serena(tmp_path, responses) -> tuple[SerenaSymbolNavigator, _FakeSerenaTransport]:
    transport = _FakeSerenaTransport(responses)
    navigator = SerenaSymbolNavigator(
        tmp_path,
        _pack(tmp_path),
        SerenaConfig(url="http://127.0.0.1:9121/mcp", timeout_seconds=0.25),
        transport=transport,
    )
    return navigator, transport


def _serena_location(*, path: str = "src/app.py", line: int = 0, name="service"):
    return {
        "name_path": name,
        "kind": "Function",
        "relative_path": path,
        "body_location": {"start_line": line, "end_line": line + 1},
    }


def test_serena_successful_lookup_is_validated_and_has_provider_provenance(
    tmp_path,
) -> None:
    navigator, transport = _serena(tmp_path, {"find_symbol": [_serena_location()]})

    result = navigator.definitions("service")

    assert result.provider == "serena"
    assert result.candidates[0].path == "src/app.py"
    assert result.candidates[0].extractor == "serena"
    assert result.candidates[0].provenance[0].extractor == "serena"
    assert transport.calls == [
        (
            "find_symbol",
            {"name_path_pattern": "service", "include_body": False},
            0.25,
        )
    ]


def test_serena_references_use_read_only_referencing_symbol_tool(tmp_path) -> None:
    navigator, _transport = _serena(
        tmp_path,
        {"find_referencing_symbols": [_serena_location(line=2, name="alpha.handle")]},
    )

    result = navigator.references("service")

    assert result.operation == "references"
    assert result.candidates[0].neighbor_id == "alpha.handle"
    assert result.candidates[0].start_line == 3


@pytest.mark.parametrize(
    ("failure", "warning"),
    [
        (SerenaMCPError("offline"), "serena-unavailable"),
        (TimeoutError(), "serena-timeout"),
        (SerenaMCPError("missing", unsupported=True), "serena-unsupported-operation"),
        ({"unexpected": "shape"}, "serena-malformed-response"),
    ],
)
def test_serena_failures_degrade_to_code_scout(tmp_path, failure, warning) -> None:
    navigator, _transport = _serena(tmp_path, {"find_symbol": failure})

    result = navigator.definitions("symbol:service")

    assert result.provider == "code-scout"
    assert result.candidates[0].extractor == "lsp"
    assert result.warnings[0]["code"] == warning


def test_serena_invalid_paths_are_rejected_before_fallback(tmp_path) -> None:
    navigator, _transport = _serena(
        tmp_path, {"find_symbol": [_serena_location(path="../escape.py")]}
    )

    result = navigator.definitions("symbol:service")

    assert result.provider == "code-scout"
    assert [warning["code"] for warning in result.warnings[:2]] == [
        "citation-path-traversal",
        "serena-no-valid-locations",
    ]


def test_serena_partial_language_support_falls_back_per_operation(tmp_path) -> None:
    navigator, _transport = _serena(
        tmp_path,
        {
            "find_symbol": [_serena_location()],
            "find_implementations": SerenaMCPError(
                "not supported for language", unsupported=True
            ),
        },
    )

    definition = navigator.definitions("service")
    implementation = navigator.implementations("symbol:service")
    callees = navigator.callees("symbol:handler:a")

    assert definition.provider == "serena"
    assert implementation.provider == "code-scout"
    assert implementation.warnings[0]["code"] == "serena-unsupported-operation"
    assert callees.warnings[0]["code"] == "serena-unsupported-operation"


def test_serena_symbol_overview_is_available_through_evidence_tools(tmp_path) -> None:
    pack = _pack(tmp_path)
    transport = _FakeSerenaTransport(
        {"get_symbols_overview": [_serena_location(name="service")]}
    )
    navigator = SerenaSymbolNavigator(
        tmp_path,
        pack,
        SerenaConfig(command=("serena", "start-mcp-server")),
        transport=transport,
    )

    result = EvidenceTools(tmp_path, pack, symbol_navigator=navigator).symbol_overview(
        "src/app.py"
    )

    assert result["provenance"]["provider"] == "serena"
    assert transport.calls[0][0] == "get_symbols_overview"


def test_serena_config_requires_exactly_one_local_process_or_url() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        SerenaConfig()
    with pytest.raises(ValueError, match="exactly one"):
        SerenaConfig(command=("serena",), url="http://localhost:9121/mcp")


def test_workspace_configuration_selects_optional_serena_provider(tmp_path) -> None:
    state = tmp_path / ".code-scout"
    state.mkdir()
    (state / "config.yaml").write_text(
        """workspace:
  id: test
  name: Test
repositories:
  - id: test
    path: .
navigation:
  serena:
    enabled: true
    url: http://127.0.0.1:9121/mcp
    timeout_seconds: 0.5
""",
        encoding="utf-8",
    )
    pack = _pack(tmp_path)
    transport = _FakeSerenaTransport({"find_symbol": [_serena_location()]})

    config = load_workspace_config(tmp_path)
    navigator = configured_symbol_navigator(tmp_path, pack, transport=transport)
    result = navigator.definitions("service")

    assert config.navigation.serena.url == "http://127.0.0.1:9121/mcp"
    assert result.provider == "serena"
    assert transport.calls[0][2] == 0.5


def _pack(tmp_path, *, include_lsp: bool = True) -> EvidencePack:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir(exist_ok=True)
    source.write_text(
        "def service():\n    pass\ndef handle():\n    service()\n"
        "def handle_two():\n    service()\ndef caller():\n    service()\n",
        encoding="utf-8",
    )
    service_sources = [
        PackedSourceProvenance("src/app.py", 1, 2, "graphify", 0.9),
        PackedSourceProvenance("src/app.py", 1, 2, "tree_sitter", 0.8),
    ]
    if include_lsp:
        service_sources.append(PackedSourceProvenance("src/app.py", 1, 2, "lsp", 0.96))
    entries = (
        _entry("symbol:service", "service", 1, 2, tuple(service_sources)),
        _entry("symbol:handler:a", "alpha.handle", 3, 4, (_source(4, "lsp"),)),
        _entry("symbol:handler:b", "beta.handle", 5, 6, (_source(6, "graphify"),)),
        _entry("symbol:caller", "caller", 7, 8, (_source(8, "lexical"),)),
    )
    relationships = (
        _edge("1", "symbol:handler:a", "calls", "symbol:service", 4, "lsp"),
        _edge(
            "2",
            "symbol:handler:b",
            "implements",
            "symbol:service",
            6,
            "graphify",
        ),
        _edge("3", "symbol:caller", "uses", "symbol:service", 8, "lexical"),
    )
    return EvidencePack(
        query="service references",
        evidence=(),
        relationships=relationships,
        repository_map=entries,
        needs_review=(),
        context="{}",
        truncated=False,
    )


def _entry(
    symbol_id: str,
    name: str,
    start: int,
    end: int,
    sources: tuple[PackedSourceProvenance, ...],
) -> RepositoryMapEntry:
    return RepositoryMapEntry(
        "symbol",
        symbol_id,
        "src/app.py",
        name,
        start_line=start,
        end_line=end,
        source_provenance=sources,
    )


def _source(line: int, extractor: str) -> PackedSourceProvenance:
    return PackedSourceProvenance("src/app.py", line, line, extractor, 0.9)


def _edge(
    edge_id: str,
    source: str,
    relation: str,
    target: str,
    line: int,
    extractor: str,
) -> PackedRelationship:
    return PackedRelationship(
        f"relationship:{edge_id}",
        "symbol",
        source,
        relation,
        "symbol",
        target,
        0.9,
        (extractor,),
        (_source(line, extractor),),
    )
