from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, get_args

from code_scout.core.errors import ExtractionError
from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    Evidence,
    NodeType,
    Service,
    Symbol,
)
from code_scout.models.workspace import Workspace
from code_scout.utils.identifiers import slugify_identifier
from code_scout.workspace.config import ExtractorEntryConfig, ExtractorWatchConfig

GRAPHIFY_SETUP_MESSAGE = "Graphify source is unavailable."
GRAPHIFY_SETUP_HINT = (
    "Configure `extractors.graphify.command` or `extractors.graphify.json_path` "
    "in `.code-scout/config.yaml`, then rerun "
    "`code-scout scan --extractor graphify`."
)


@dataclass(frozen=True)
class GraphifyExtraction:
    symbols: tuple[Symbol, ...] = ()
    services: tuple[Service, ...] = ()
    contracts: tuple[Contract, ...] = ()
    concepts: tuple[Concept, ...] = ()
    decisions: tuple[Decision, ...] = ()
    edges: tuple[Edge, ...] = ()


@dataclass(frozen=True)
class GraphifyWarning:
    repository_id: str
    path: str
    message: str


@dataclass(frozen=True)
class GraphifyWatchEvent:
    graph_json: str
    source: str = "graphify-watch"


class GraphifyAdapter(Protocol):
    def run_command(
        self,
        *,
        workspace_root: str,
        command: tuple[str, ...],
    ) -> str: ...

    def load_file(
        self,
        *,
        workspace_root: str,
        json_path: str,
    ) -> str: ...


class GraphifyWatchAdapter(Protocol):
    def watch_events(
        self,
        *,
        workspace_root: str,
        watch_config: ExtractorWatchConfig,
    ) -> Iterable[GraphifyWatchEvent]: ...


class GraphifyProcessAdapter:
    def run_command(
        self,
        *,
        workspace_root: str,
        command: tuple[str, ...],
    ) -> str:
        try:
            result = subprocess.run(
                command,
                cwd=workspace_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise ExtractionError(
                GRAPHIFY_SETUP_MESSAGE,
                hint=(
                    f"Command `{command[0]}` was not found. "
                    "Update `extractors.graphify.command` in `.code-scout/config.yaml`."
                ),
            ) from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() or error.stdout.strip()
            hint = "Fix the configured Graphify command and rerun the scan."
            if detail:
                hint = f"{hint} Command output: {detail}"
            raise ExtractionError(GRAPHIFY_SETUP_MESSAGE, hint=hint) from error

        return result.stdout

    def load_file(
        self,
        *,
        workspace_root: str,
        json_path: str,
    ) -> str:
        path = Path(json_path)
        if not path.is_absolute():
            path = Path(workspace_root) / path
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ExtractionError(
                GRAPHIFY_SETUP_MESSAGE,
                hint=(
                    f"Graphify JSON file not found at `{json_path}`. "
                    "Update `extractors.graphify.json_path` in "
                    "`.code-scout/config.yaml`."
                ),
            ) from error

    def watch_events(
        self,
        *,
        workspace_root: str,
        watch_config: ExtractorWatchConfig,
    ) -> Iterable[GraphifyWatchEvent]:
        if watch_config.output_path is not None:
            return _watch_events_from_text(
                self.load_file(
                    workspace_root=workspace_root,
                    json_path=watch_config.output_path,
                ),
                source=watch_config.output_path,
            )
        if not watch_config.command:
            raise ExtractionError(
                GRAPHIFY_SETUP_MESSAGE,
                hint="Configure `extractors.graphify.watch.command` or `output_path`.",
            )
        return _watch_events_from_text(
            self.run_command(
                workspace_root=workspace_root,
                command=watch_config.command,
            ),
            source=" ".join(watch_config.command),
        )


def extract_graphify(
    workspace: Workspace,
    config: ExtractorEntryConfig,
    *,
    adapter: GraphifyAdapter | None = None,
) -> GraphifyExtraction:
    if not config.enabled:
        return GraphifyExtraction()
    if config.json_path is None and not config.command:
        raise ExtractionError(GRAPHIFY_SETUP_MESSAGE, hint=GRAPHIFY_SETUP_HINT)

    graphify_adapter = GraphifyProcessAdapter() if adapter is None else adapter
    if config.json_path is not None:
        graph_text = graphify_adapter.load_file(
            workspace_root=workspace.root_path,
            json_path=config.json_path,
        )
    else:
        graph_text = graphify_adapter.run_command(
            workspace_root=workspace.root_path,
            command=config.command,
        )

    return graphify_extraction_from_json(graph_text, workspace_root=workspace.root_path)


def extract_graphify_watch(
    workspace: Workspace,
    config: ExtractorEntryConfig,
    *,
    adapter: GraphifyWatchAdapter | None = None,
) -> tuple[GraphifyExtraction, tuple[GraphifyWarning, ...]]:
    if not config.enabled:
        return GraphifyExtraction(), ()
    if config.watch is None:
        return extract_graphify(workspace, config), ()

    watch_adapter = GraphifyProcessAdapter() if adapter is None else adapter
    try:
        events = tuple(
            watch_adapter.watch_events(
                workspace_root=workspace.root_path,
                watch_config=config.watch,
            )
        )
        return graphify_extraction_from_watch_events(events), ()
    except ExtractionError as error:
        return GraphifyExtraction(), (
            GraphifyWarning(
                repository_id=workspace.repositories[0].id
                if workspace.repositories
                else workspace.id,
                path=_watch_warning_path(config.watch),
                message=_watch_warning_message(error),
            ),
        )


def graphify_extraction_from_watch_events(
    events: Iterable[GraphifyWatchEvent],
) -> GraphifyExtraction:
    extractions = tuple(
        graphify_extraction_from_json(_watch_graph_json(event)) for event in events
    )
    return GraphifyExtraction(
        symbols=tuple(symbol for item in extractions for symbol in item.symbols),
        services=tuple(service for item in extractions for service in item.services),
        contracts=tuple(
            contract for item in extractions for contract in item.contracts
        ),
        concepts=tuple(concept for item in extractions for concept in item.concepts),
        decisions=tuple(
            decision for item in extractions for decision in item.decisions
        ),
        edges=tuple(edge for item in extractions for edge in item.edges),
    )


def graphify_extraction_from_json(
    graph_text: str,
    *,
    workspace_root: str | None = None,
) -> GraphifyExtraction:
    try:
        raw_graph = json.loads(graph_text)
    except json.JSONDecodeError as error:
        raise ExtractionError(
            "Graphify output is not valid JSON.",
            hint=f"Fix Graphify output near line {error.lineno}: {error.msg}.",
        ) from error
    if not isinstance(raw_graph, dict):
        raise ExtractionError(
            "Graphify output has an invalid schema.",
            hint="Expected a JSON object containing code-scout graph fact arrays.",
        )

    graphify_graph = _graphify_graph_from_value(
        raw_graph,
        workspace_root=workspace_root,
    )
    return GraphifyExtraction(
        symbols=(
            *_symbols_from_value(raw_graph.get("symbols", [])),
            *graphify_graph.symbols,
        ),
        services=(
            *_services_from_value(raw_graph.get("services", [])),
            *graphify_graph.services,
        ),
        contracts=(
            *_contracts_from_value(raw_graph.get("contracts", [])),
            *graphify_graph.contracts,
        ),
        concepts=(
            *_concepts_from_value(raw_graph.get("concepts", [])),
            *graphify_graph.concepts,
        ),
        decisions=_decisions_from_value(raw_graph.get("decisions", [])),
        edges=(
            *_edges_from_value(raw_graph.get("edges", [])),
            *graphify_graph.edges,
        ),
    )


def _watch_events_from_text(
    text: str,
    *,
    source: str,
) -> tuple[GraphifyWatchEvent, ...]:
    stripped = text.strip()
    if not stripped:
        return ()

    if "\n" not in stripped:
        return (_watch_event_from_json_text(stripped, source=source),)

    events: list[GraphifyWatchEvent] = []
    for line_number, line in enumerate(stripped.splitlines(), start=1):
        if not line.strip():
            continue
        events.append(
            _watch_event_from_json_text(line, source=f"{source}:{line_number}")
        )
    return tuple(events)


def _watch_graph_json(event: GraphifyWatchEvent) -> str:
    return _watch_event_from_json_text(
        event.graph_json,
        source=event.source,
    ).graph_json


def _watch_event_from_json_text(text: str, *, source: str) -> GraphifyWatchEvent:
    try:
        raw_event = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExtractionError(
            "Graphify watch output is not valid JSON.",
            hint=f"Fix Graphify watch output near line {error.lineno}: {error.msg}.",
        ) from error
    if not isinstance(raw_event, dict):
        raise ExtractionError(
            "Graphify watch output has an invalid schema.",
            hint="Expected each Graphify watch event to be a JSON object.",
        )

    graph_value = _graph_value_from_watch_event(raw_event)
    return GraphifyWatchEvent(graph_json=json.dumps(graph_value), source=source)


def _graph_value_from_watch_event(raw_event: dict[str, Any]) -> dict[str, Any]:
    for key in ("graph", "changed_graph", "graph_json"):
        value = raw_event.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as error:
                raise ExtractionError(
                    "Graphify watch event contains invalid graph JSON.",
                    hint=f"Fix `{key}` near line {error.lineno}: {error.msg}.",
                ) from error
            if isinstance(decoded, dict):
                return decoded
            raise ExtractionError(
                "Graphify watch event contains an invalid graph payload.",
                hint=f"`{key}` must decode to a JSON object.",
            )

    return raw_event


def _watch_warning_path(watch_config: ExtractorWatchConfig) -> str:
    if watch_config.output_path is not None:
        return watch_config.output_path
    return " ".join(watch_config.command) or "graphify watch"


def _watch_warning_message(error: ExtractionError) -> str:
    if error.hint:
        return f"{error.message} {error.hint}"
    return error.message


def _graphify_graph_from_value(
    raw_graph: dict[str, Any],
    *,
    workspace_root: str | None = None,
) -> GraphifyExtraction:
    symbols: list[Symbol] = []
    services: list[Service] = []
    contracts: list[Contract] = []
    concepts: list[Concept] = []
    edges: list[Edge] = []

    networkx_graph = _networkx_graphify_extraction(raw_graph, workspace_root)
    symbols.extend(networkx_graph.symbols)
    edges.extend(networkx_graph.edges)

    module_graph = _optional_mapping(raw_graph, "module_graph")
    if module_graph is not None:
        for item in _required_mapping_items(
            module_graph.get("modules", []), "module_graph.modules"
        ):
            symbol = _module_symbol(item, "module_graph.modules[]")
            symbols.append(symbol)
            edges.append(
                _declares_symbol_edge(symbol, _item_evidence(item, symbol.file_id))
            )

    symbol_graph = _optional_mapping(raw_graph, "symbol_graph")
    if symbol_graph is not None:
        for item in _required_mapping_items(
            symbol_graph.get("symbols", []), "symbol_graph.symbols"
        ):
            symbol = _graphify_symbol(item, "symbol_graph.symbols[]")
            symbols.append(symbol)
            edges.append(
                _declares_symbol_edge(symbol, _item_evidence(item, symbol.file_id))
            )
        edges.extend(
            _edges_from_value(symbol_graph.get("edges", []))
            if "edges" in symbol_graph
            else ()
        )

    dependency_graph = _optional_mapping(raw_graph, "dependency_graph")
    if dependency_graph is not None:
        for item in _required_mapping_items(
            dependency_graph.get("dependencies", []),
            "dependency_graph.dependencies",
        ):
            edges.append(_dependency_edge(item))

    call_graph = _optional_mapping(raw_graph, "call_graph")
    if call_graph is not None:
        for item in _required_mapping_items(
            call_graph.get("calls", []), "call_graph.calls"
        ):
            edges.append(_call_edge(item))

    repository_graph = _optional_mapping(raw_graph, "repository_graph")
    if repository_graph is not None:
        for item in _required_mapping_items(
            repository_graph.get("services", []),
            "repository_graph.services",
        ):
            services.append(_repository_service(item))
        for item in _required_mapping_items(
            repository_graph.get("contracts", []),
            "repository_graph.contracts",
        ):
            contracts.append(_repository_contract(item))
        for item in _required_mapping_items(
            repository_graph.get("concepts", []),
            "repository_graph.concepts",
        ):
            concepts.append(
                Concept(
                    id=_required_string(item, "id", "repository_graph.concepts[].id"),
                    name=_required_string(
                        item,
                        "name",
                        "repository_graph.concepts[].name",
                    ),
                    description=_optional_string(
                        item,
                        "description",
                        "repository_graph.concepts[].description",
                    ),
                )
            )
        edges.extend(
            _edges_from_value(repository_graph.get("edges", []))
            if "edges" in repository_graph
            else ()
        )

    return GraphifyExtraction(
        symbols=tuple(symbols),
        services=tuple(services),
        contracts=tuple(contracts),
        concepts=tuple(concepts),
        edges=tuple(edge for edge in edges if edge.evidence or edge.confidence > 0),
    )


def _networkx_graphify_extraction(
    raw_graph: dict[str, Any],
    workspace_root: str | None,
) -> GraphifyExtraction:
    if "nodes" not in raw_graph and "links" not in raw_graph:
        return GraphifyExtraction()

    nodes = _required_mapping_items(raw_graph.get("nodes", []), "nodes")
    links = _required_mapping_items(raw_graph.get("links", []), "links")
    symbols_by_graph_id: dict[str, Symbol] = {}
    symbols: list[Symbol] = []
    edges: list[Edge] = []

    for node in nodes:
        if not node.get("source_file"):
            continue
        symbol = _networkx_node_symbol(node, workspace_root)
        symbols_by_graph_id[_required_string(node, "id", "nodes[].id")] = symbol
        if symbol.kind != "file":
            symbols.append(symbol)
            edges.append(_declares_symbol_edge(symbol, symbol.evidence))

    for link in links:
        source = symbols_by_graph_id.get(
            _required_string(link, "source", "links[].source")
        )
        target = symbols_by_graph_id.get(
            _required_string(link, "target", "links[].target")
        )
        if source is None or target is None:
            continue
        relation = _optional_string(link, "relation", "links[].relation") or "related"
        if relation == "contains" and source.kind == "file":
            edges.append(
                _declares_symbol_edge(
                    target, _networkx_link_evidence(link, workspace_root)
                )
            )
            continue
        edges.append(
            Edge(
                source_type="symbol",
                source_id=source.id,
                target_type="symbol",
                target_id=target.id,
                relation_type=_networkx_relation_type(relation),
                confidence=_networkx_confidence(link),
                evidence=_networkx_link_evidence(link, workspace_root),
            )
        )

    return GraphifyExtraction(
        symbols=tuple(symbols),
        edges=tuple(edges),
    )


def _networkx_node_symbol(
    node: dict[str, Any],
    workspace_root: str | None,
) -> Symbol:
    label = _required_string(node, "label", "nodes[].label")
    source_file = _required_string(node, "source_file", "nodes[].source_file")
    file_path = _graphify_source_path(source_file, workspace_root)
    name = _networkx_symbol_name(label)
    kind = _networkx_symbol_kind(label)
    start_line = _line_from_source_location(
        _optional_string(node, "source_location", "nodes[].source_location")
    )
    evidence = (
        Evidence(
            file_path=file_path,
            extractor_name="graphify",
            confidence=1.0,
            start_line=start_line,
            end_line=start_line,
            metadata={
                "graphify_node_id": _required_string(node, "id", "nodes[].id"),
                "graphify_file_type": _optional_string(
                    node,
                    "file_type",
                    "nodes[].file_type",
                )
                or "",
            },
        ),
    )
    return Symbol(
        id=_symbol_id("workspace", file_path, name),
        file_id=_file_id("workspace", file_path),
        name=name,
        kind=kind,
        qualified_name=name,
        start_line=start_line,
        end_line=start_line,
        evidence=evidence,
    )


def _networkx_link_evidence(
    link: dict[str, Any],
    workspace_root: str | None,
) -> tuple[Evidence, ...]:
    source_file = _optional_string(link, "source_file", "links[].source_file")
    file_path = (
        _graphify_source_path(source_file, workspace_root)
        if source_file is not None
        else "graphify-out/graph.json"
    )
    start_line = _line_from_source_location(
        _optional_string(link, "source_location", "links[].source_location")
    )
    return (
        Evidence(
            file_path=file_path,
            extractor_name="graphify",
            confidence=_networkx_confidence(link),
            start_line=start_line,
            end_line=start_line,
            metadata={
                "graphify_relation": _optional_string(
                    link,
                    "relation",
                    "links[].relation",
                )
                or "",
            },
        ),
    )


def _graphify_source_path(source_file: str, workspace_root: str | None) -> str:
    source_path = Path(source_file)
    if source_path.is_absolute() and workspace_root is not None:
        try:
            return (
                source_path.resolve(strict=False)
                .relative_to(Path(workspace_root).resolve(strict=False))
                .as_posix()
            )
        except ValueError:
            pass
    return source_path.as_posix().removeprefix("./").lstrip("/")


def _networkx_symbol_name(label: str) -> str:
    return label[:-2] if label.endswith("()") else label


def _networkx_symbol_kind(label: str) -> str:
    if label.endswith("()"):
        return "function"
    if Path(label).suffix:
        return "file"
    return "symbol"


def _networkx_relation_type(relation: str) -> str:
    normalized = slugify_identifier(relation, fallback="related").replace("-", "_")
    return "contains" if normalized == "contains" else normalized


def _networkx_confidence(link: dict[str, Any]) -> float:
    value = link.get("confidence_score", link.get("weight", 1.0))
    if isinstance(value, int | float):
        return float(value)
    return 1.0


def _line_from_source_location(source_location: str | None) -> int | None:
    if source_location is None:
        return None
    match = re.match(r"^L(?P<line>\d+)", source_location)
    if match is None:
        return None
    return int(match.group("line"))


def _file_path_from_file_id(file_id: str) -> str:
    return file_id.removeprefix("file:").split(":", 1)[-1]


def _symbols_from_value(value: Any) -> tuple[Symbol, ...]:
    return tuple(
        Symbol(
            id=_required_string(item, "id", "symbols[].id"),
            file_id=_required_string(item, "file_id", "symbols[].file_id"),
            name=_required_string(item, "name", "symbols[].name"),
            kind=_required_string(item, "kind", "symbols[].kind"),
            qualified_name=_optional_string(
                item, "qualified_name", "symbols[].qualified_name"
            ),
            start_line=_optional_int(item, "start_line", "symbols[].start_line"),
            end_line=_optional_int(item, "end_line", "symbols[].end_line"),
        )
        for item in _required_mapping_items(value, "symbols")
    )


def _services_from_value(value: Any) -> tuple[Service, ...]:
    return tuple(
        Service(
            id=_required_string(item, "id", "services[].id"),
            name=_required_string(item, "name", "services[].name"),
            repository_id=_optional_string(
                item,
                "repository_id",
                "services[].repository_id",
            ),
            description=_optional_string(item, "description", "services[].description"),
            evidence=_evidence_from_value(
                item.get("evidence", []), "services[].evidence"
            ),
        )
        for item in _required_mapping_items(value, "services")
    )


def _contracts_from_value(value: Any) -> tuple[Contract, ...]:
    return tuple(
        Contract(
            id=_required_string(item, "id", "contracts[].id"),
            name=_required_string(item, "name", "contracts[].name"),
            kind=_required_string(item, "kind", "contracts[].kind"),
            path=_optional_string(item, "path", "contracts[].path"),
            version=_optional_string(item, "version", "contracts[].version"),
            method=_optional_string(item, "method", "contracts[].method"),
            producer_hint=_optional_string(
                item,
                "producer_hint",
                "contracts[].producer_hint",
            ),
            operation_id=_optional_string(
                item, "operation_id", "contracts[].operation_id"
            ),
            request_dtos=_optional_string_tuple(
                item,
                "request_dtos",
                "contracts[].request_dtos",
            ),
            response_dtos=_optional_string_tuple(
                item,
                "response_dtos",
                "contracts[].response_dtos",
            ),
            evidence=_evidence_from_value(
                item.get("evidence", []), "contracts[].evidence"
            ),
        )
        for item in _required_mapping_items(value, "contracts")
    )


def _concepts_from_value(value: Any) -> tuple[Concept, ...]:
    return tuple(
        Concept(
            id=_required_string(item, "id", "concepts[].id"),
            name=_required_string(item, "name", "concepts[].name"),
            description=_optional_string(item, "description", "concepts[].description"),
        )
        for item in _required_mapping_items(value, "concepts")
    )


def _decisions_from_value(value: Any) -> tuple[Decision, ...]:
    return tuple(
        Decision(
            id=_required_string(item, "id", "decisions[].id"),
            title=_required_string(item, "title", "decisions[].title"),
            status=_required_string(item, "status", "decisions[].status"),
            adr_id=_optional_string(item, "adr_id", "decisions[].adr_id"),
            path=_optional_string(item, "path", "decisions[].path"),
            decided_at=_optional_string(item, "decided_at", "decisions[].decided_at"),
            context=_optional_string(item, "context", "decisions[].context"),
            decision=_optional_string(item, "decision", "decisions[].decision"),
            consequences=_optional_string(
                item,
                "consequences",
                "decisions[].consequences",
            ),
            evidence=_evidence_from_value(
                item.get("evidence", []), "decisions[].evidence"
            ),
        )
        for item in _required_mapping_items(value, "decisions")
    )


def _edges_from_value(value: Any) -> tuple[Edge, ...]:
    return tuple(
        Edge(
            source_type=_node_type(item, "source_type", "edges[].source_type"),
            source_id=_required_string(item, "source_id", "edges[].source_id"),
            target_type=_node_type(item, "target_type", "edges[].target_type"),
            target_id=_required_string(item, "target_id", "edges[].target_id"),
            relation_type=_required_string(
                item, "relation_type", "edges[].relation_type"
            ),
            confidence=_optional_float(item, "confidence", 1.0, "edges[].confidence"),
            evidence=_evidence_from_value(item.get("evidence", []), "edges[].evidence"),
        )
        for item in _required_mapping_items(value, "edges")
    )


def _module_symbol(item: dict[str, Any], path: str) -> Symbol:
    repository_id = _optional_string(item, "repository_id", f"{path}.repository_id")
    file_path = _required_path(item, path)
    name = _optional_string(item, "name", f"{path}.name") or Path(file_path).stem
    qualified_name = (
        _optional_string(item, "qualified_name", f"{path}.qualified_name") or name
    )
    return Symbol(
        id=_optional_string(item, "id", f"{path}.id")
        or _symbol_id(repository_id or "workspace", file_path, qualified_name),
        file_id=_file_id(repository_id or "workspace", file_path),
        name=name,
        kind=_optional_string(item, "kind", f"{path}.kind") or "graphify_module",
        qualified_name=qualified_name,
        start_line=_optional_int(item, "start_line", f"{path}.start_line"),
        end_line=_optional_int(item, "end_line", f"{path}.end_line"),
    )


def _graphify_symbol(item: dict[str, Any], path: str) -> Symbol:
    repository_id = _optional_string(item, "repository_id", f"{path}.repository_id")
    file_path = _required_path(item, path)
    name = _required_string(item, "name", f"{path}.name")
    qualified_name = (
        _optional_string(item, "qualified_name", f"{path}.qualified_name") or name
    )
    return Symbol(
        id=_optional_string(item, "id", f"{path}.id")
        or _symbol_id(repository_id or "workspace", file_path, qualified_name),
        file_id=_file_id(repository_id or "workspace", file_path),
        name=name,
        kind=_optional_string(item, "kind", f"{path}.kind") or "graphify_symbol",
        qualified_name=qualified_name,
        start_line=_optional_int(item, "start_line", f"{path}.start_line"),
        end_line=_optional_int(item, "end_line", f"{path}.end_line"),
    )


def _dependency_edge(item: dict[str, Any]) -> Edge:
    source_type, source_id = _graph_endpoint(
        item, "source", "dependency_graph.dependencies[]"
    )
    target_type, target_id = _graph_endpoint(
        item, "target", "dependency_graph.dependencies[]"
    )
    confidence = _optional_float(
        item,
        "confidence",
        0.85,
        "dependency_graph.dependencies[].confidence",
    )
    return Edge(
        source_type=source_type,
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        relation_type=_optional_string(
            item,
            "relation_type",
            "dependency_graph.dependencies[].relation_type",
        )
        or "depends_on",
        confidence=confidence,
        evidence=_item_evidence(item, source_id, confidence=confidence),
    )


def _call_edge(item: dict[str, Any]) -> Edge:
    source_type, source_id = _graph_endpoint(item, "source", "call_graph.calls[]")
    target_type, target_id = _graph_endpoint(item, "target", "call_graph.calls[]")
    confidence = _optional_float(
        item, "confidence", 0.85, "call_graph.calls[].confidence"
    )
    return Edge(
        source_type=source_type,
        source_id=source_id,
        target_type=target_type,
        target_id=target_id,
        relation_type=_optional_string(
            item, "relation_type", "call_graph.calls[].relation_type"
        )
        or "calls",
        confidence=confidence,
        evidence=_item_evidence(item, source_id, confidence=confidence),
    )


def _repository_service(item: dict[str, Any]) -> Service:
    return Service(
        id=_required_string(item, "id", "repository_graph.services[].id"),
        name=_required_string(item, "name", "repository_graph.services[].name"),
        repository_id=_optional_string(
            item,
            "repository_id",
            "repository_graph.services[].repository_id",
        ),
        description=_optional_string(
            item,
            "description",
            "repository_graph.services[].description",
        ),
    )


def _repository_contract(item: dict[str, Any]) -> Contract:
    return Contract(
        id=_required_string(item, "id", "repository_graph.contracts[].id"),
        name=_required_string(item, "name", "repository_graph.contracts[].name"),
        kind=_required_string(item, "kind", "repository_graph.contracts[].kind"),
        path=_optional_string(item, "path", "repository_graph.contracts[].path"),
        version=_optional_string(
            item,
            "version",
            "repository_graph.contracts[].version",
        ),
        method=_optional_string(item, "method", "repository_graph.contracts[].method"),
        producer_hint=_optional_string(
            item,
            "producer_hint",
            "repository_graph.contracts[].producer_hint",
        ),
        operation_id=_optional_string(
            item,
            "operation_id",
            "repository_graph.contracts[].operation_id",
        ),
        request_dtos=_optional_string_tuple(
            item,
            "request_dtos",
            "repository_graph.contracts[].request_dtos",
        ),
        response_dtos=_optional_string_tuple(
            item,
            "response_dtos",
            "repository_graph.contracts[].response_dtos",
        ),
        evidence=_evidence_from_value(
            item.get("evidence", []),
            "repository_graph.contracts[].evidence",
        ),
    )


def _declares_symbol_edge(
    symbol: Symbol,
    evidence: tuple[Evidence, ...],
) -> Edge:
    return Edge(
        source_type="file",
        source_id=symbol.file_id,
        target_type="symbol",
        target_id=symbol.id,
        relation_type="declares_symbol",
        confidence=_confidence_from_evidence(evidence),
        evidence=evidence,
    )


def _evidence_from_value(value: Any, path: str) -> tuple[Evidence, ...]:
    return tuple(
        Evidence(
            file_path=_required_string(item, "file_path", f"{path}[].file_path"),
            extractor_name=_optional_string(
                item,
                "extractor_name",
                f"{path}[].extractor_name",
            )
            or "graphify",
            confidence=_optional_float(item, "confidence", 1.0, f"{path}[].confidence"),
            start_line=_optional_int(item, "start_line", f"{path}[].start_line"),
            end_line=_optional_int(item, "end_line", f"{path}[].end_line"),
            metadata=_optional_metadata(item, "metadata", f"{path}[].metadata"),
        )
        for item in _required_mapping_items(value, path)
    )


def _item_evidence(
    item: dict[str, Any],
    fallback_file_path: str,
    *,
    confidence: float | None = None,
) -> tuple[Evidence, ...]:
    if "evidence" in item:
        return _evidence_from_value(item["evidence"], "graphify.evidence")

    file_path = (
        _optional_string(item, "file_path", "graphify.file_path")
        or _optional_string(item, "path", "graphify.path")
        or _optional_string(item, "source_path", "graphify.source_path")
        or _optional_string(item, "source_file_path", "graphify.source_file_path")
        or fallback_file_path.removeprefix("file:").split(":", 1)[-1]
    )
    return (
        Evidence(
            file_path=file_path,
            extractor_name="graphify",
            confidence=confidence
            if confidence is not None
            else _optional_float(item, "confidence", 1.0, "graphify.confidence"),
            start_line=_optional_int(item, "start_line", "graphify.start_line"),
            end_line=_optional_int(item, "end_line", "graphify.end_line"),
            metadata=_graphify_metadata(item),
        ),
    )


def _graphify_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(_optional_metadata(item, "metadata", "graphify.metadata"))
    repository_id = _optional_string(item, "repository_id", "graphify.repository_id")
    if repository_id is not None:
        metadata.setdefault("repository_id", repository_id)
    graph_kind = _optional_string(item, "graph", "graphify.graph")
    if graph_kind is not None:
        metadata.setdefault("graph", graph_kind)
    return metadata


def _graph_endpoint(
    item: dict[str, Any],
    prefix: str,
    path: str,
) -> tuple[NodeType, str]:
    endpoint_type = _optional_string(item, f"{prefix}_type", f"{path}.{prefix}_type")
    endpoint_id = _optional_string(item, f"{prefix}_id", f"{path}.{prefix}_id")
    if endpoint_type is not None and endpoint_id is not None:
        if endpoint_type not in get_args(NodeType):
            _raise_schema_error(
                f"`{path}.{prefix}_type` must be a supported node type."
            )
        return endpoint_type, endpoint_id  # type: ignore[return-value]

    repository_id = (
        _optional_string(item, "repository_id", f"{path}.repository_id") or "workspace"
    )
    file_path = _optional_string(
        item, f"{prefix}_path", f"{path}.{prefix}_path"
    ) or _optional_string(item, f"{prefix}_file_path", f"{path}.{prefix}_file_path")
    name = _optional_string(
        item, f"{prefix}_name", f"{path}.{prefix}_name"
    ) or _optional_string(
        item, f"{prefix}_qualified_name", f"{path}.{prefix}_qualified_name"
    )
    if file_path is not None and name is not None:
        return (
            "symbol",
            _symbol_id(repository_id, file_path, name),
        )
    if file_path is not None:
        return "file", _file_id(repository_id, file_path)

    _raise_schema_error(
        f"`{path}` must provide `{prefix}_id`, `{prefix}_path`, or "
        f"`{prefix}_file_path`."
    )


def _optional_mapping(
    mapping: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        _raise_schema_error(f"`{key}` must be an object when provided.")
    return value


def _required_path(item: dict[str, Any], path: str) -> str:
    return (
        _optional_string(item, "path", f"{path}.path")
        or _optional_string(item, "file_path", f"{path}.file_path")
        or _required_string(item, "module_path", f"{path}.module_path")
    )


def _confidence_from_evidence(evidence: tuple[Evidence, ...]) -> float:
    if not evidence:
        return 1.0
    return max(item.confidence for item in evidence)


def _symbol_id(repository_id: str, path: str, qualified_name: str) -> str:
    symbol_slug = slugify_identifier(qualified_name, fallback="symbol")
    return f"symbol:{repository_id}:{path}:graphify:{symbol_slug}"


def _file_id(repository_id: str, path: str) -> str:
    return f"file:{repository_id}:{path}"


def _required_mapping_items(value: Any, path: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        _raise_schema_error(f"`{path}` must be a list.")
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            _raise_schema_error(f"`{path}` must contain objects.")
        items.append(item)
    return tuple(items)


def _required_string(mapping: dict[str, Any], key: str, path: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        _raise_schema_error(f"`{path}` must be a non-empty string.")
    return value


def _optional_string(mapping: dict[str, Any], key: str, path: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        _raise_schema_error(f"`{path}` must be a non-empty string when provided.")
    return value


def _optional_string_tuple(
    mapping: dict[str, Any],
    key: str,
    path: str,
) -> tuple[str, ...]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        _raise_schema_error(f"`{path}` must be a list of non-empty strings.")
    return tuple(value)


def _optional_int(mapping: dict[str, Any], key: str, path: str) -> int | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        _raise_schema_error(f"`{path}` must be an integer when provided.")
    return value


def _optional_float(
    mapping: dict[str, Any],
    key: str,
    default: float,
    path: str,
) -> float:
    value = mapping.get(key, default)
    if not isinstance(value, int | float):
        _raise_schema_error(f"`{path}` must be a number.")
    return float(value)


def _optional_metadata(
    mapping: dict[str, Any],
    key: str,
    path: str,
) -> dict[str, Any]:
    value = mapping.get(key, {})
    if not isinstance(value, dict):
        _raise_schema_error(f"`{path}` must be an object.")
    return value


def _node_type(mapping: dict[str, Any], key: str, path: str) -> NodeType:
    value = _required_string(mapping, key, path)
    if value not in get_args(NodeType):
        _raise_schema_error(f"`{path}` must be a supported node type.")
    return value


def _raise_schema_error(hint: str) -> None:
    raise ExtractionError("Graphify output has an invalid schema.", hint=hint)
