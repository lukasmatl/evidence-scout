from __future__ import annotations

from dataclasses import asdict, is_dataclass

from code_scout.evidence.assessment import assess_legacy_evidence
from code_scout.models.graph import (
    Concept,
    Contract,
    Decision,
    Edge,
    Evidence,
    File,
    Repository,
    Service,
    Snapshot,
    Symbol,
)


def test_graph_models_are_typed_dataclasses() -> None:
    assert is_dataclass(Repository)
    assert is_dataclass(File)
    assert is_dataclass(Symbol)
    assert is_dataclass(Service)
    assert is_dataclass(Contract)
    assert is_dataclass(Concept)
    assert is_dataclass(Decision)
    assert is_dataclass(Edge)
    assert is_dataclass(Evidence)
    assert is_dataclass(Snapshot)


def test_evidence_model_serializes_source_location_and_extractor_signal() -> None:
    evidence = Evidence(
        file_path="src/code_scout/workspace.py",
        start_line=12,
        end_line=18,
        extractor_name="tree_sitter",
        confidence=0.92,
    )

    assert asdict(evidence) == {
        "assessment": assess_legacy_evidence(
            {}, extractor_provenance=("tree_sitter",)
        ).to_dict(),
        "file_path": "src/code_scout/workspace.py",
        "extractor_name": "tree_sitter",
        "confidence": 0.92,
        "start_line": 12,
        "end_line": 18,
        "metadata": {},
    }


def test_edge_model_serializes_typed_endpoints_relation_and_evidence() -> None:
    evidence = Evidence(
        file_path="src\/code_scout\/commands\/cli.py",
        extractor_name="lsp",
        confidence=0.83,
        start_line=20,
    )
    edge = Edge(
        source_type="service",
        source_id="cli",
        target_type="symbol",
        target_id="code_scout.commands.cli.app",
        relation_type="defines",
        confidence=0.88,
        evidence=(evidence,),
    )

    assert asdict(edge) == {
        "source_type": "service",
        "source_id": "cli",
        "target_type": "symbol",
        "target_id": "code_scout.commands.cli.app",
        "relation_type": "defines",
        "confidence": 0.88,
        "evidence": (
            {
                "assessment": assess_legacy_evidence(
                    {}, extractor_provenance=("lsp",)
                ).to_dict(),
                "file_path": "src\/code_scout\/commands\/cli.py",
                "extractor_name": "lsp",
                "confidence": 0.83,
                "start_line": 20,
                "end_line": None,
                "metadata": {},
            },
        ),
    }


def test_snapshot_serialization_includes_representative_graph_models() -> None:
    repository = Repository(
        id="code-scout",
        name="code-scout",
        path=".",
        default_branch="main",
    )
    file = File(
        id="file:src/code_scout/graph.py",
        repository_id="code-scout",
        path="src/code_scout/graph.py",
        language="python",
    )
    symbol = Symbol(
        id="symbol:code_scout.models.graph.Snapshot",
        file_id=file.id,
        name="Snapshot",
        kind="class",
        qualified_name="code_scout.models.graph.Snapshot",
        start_line=90,
        end_line=108,
    )
    service = Service(
        id="service:graph",
        name="Graph model vocabulary",
        repository_id=repository.id,
    )
    contract = Contract(
        id="contract:health",
        name="Health endpoint",
        kind="http",
        path="/health",
    )
    concept = Concept(
        id="concept:architecture-memory",
        name="Architecture memory",
    )
    decision = Decision(
        id="decision:stdlib-dataclasses",
        title="Use stdlib dataclasses for early graph models",
        status="accepted",
        path="docs/adr/0001-graph-models.md",
    )
    evidence = Evidence(
        file_path="src/code_scout/graph.py",
        extractor_name="manual",
        confidence=1.0,
    )
    edge = Edge(
        source_type="service",
        source_id=service.id,
        target_type="concept",
        target_id=concept.id,
        relation_type="documents",
        confidence=0.9,
        evidence=(evidence,),
    )
    snapshot = Snapshot(
        id="snapshot:2026-06-20",
        workspace_id="workspace",
        created_at="2026-06-20T10:00:00Z",
        repositories=(repository,),
        files=(file,),
        symbols=(symbol,),
        services=(service,),
        contracts=(contract,),
        concepts=(concept,),
        decisions=(decision,),
        edges=(edge,),
        evidence=(evidence,),
    )

    assert snapshot.to_dict() == {
        "id": "snapshot:2026-06-20",
        "workspace_id": "workspace",
        "created_at": "2026-06-20T10:00:00Z",
        "repositories": (
            {
                "id": "code-scout",
                "name": "code-scout",
                "path": ".",
                "remote_url": None,
                "default_branch": "main",
                "language_hints": (),
                "framework_hints": (),
                "module_hints": (),
                "evidence": (),
            },
        ),
        "files": (asdict(file),),
        "symbols": (asdict(symbol),),
        "services": (asdict(service),),
        "contracts": (asdict(contract),),
        "concepts": (asdict(concept),),
        "decisions": (asdict(decision),),
        "edges": (asdict(edge),),
        "evidence": (asdict(evidence),),
    }
