from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypeAlias

from code_scout.evidence.assessment import assess_legacy_evidence

NodeType: TypeAlias = Literal[
    "repository",
    "file",
    "symbol",
    "service",
    "contract",
    "concept",
    "decision",
    "package",
]


@dataclass(frozen=True)
class Evidence:
    file_path: str
    extractor_name: str
    confidence: float | None = None
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    assessment: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assessment",
            assess_legacy_evidence(
                self.metadata,
                extractor_provenance=(self.extractor_name,),
                certainty=self.metadata.get("certainty"),
            ).to_dict(),
        )


@dataclass(frozen=True)
class Repository:
    id: str
    name: str
    path: str
    remote_url: str | None = None
    default_branch: str | None = None
    language_hints: tuple[str, ...] = ()
    framework_hints: tuple[str, ...] = ()
    module_hints: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class File:
    id: str
    repository_id: str
    path: str
    kind: str = "source"
    extension: str = ""
    language: str | None = None
    content_hash: str | None = None
    modified_at: str | None = None
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Symbol:
    id: str
    file_id: str
    name: str
    kind: str
    qualified_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    repository_id: str | None = None
    description: str | None = None
    aliases: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Contract:
    id: str
    name: str
    kind: str
    path: str | None = None
    version: str | None = None
    method: str | None = None
    producer_hint: str | None = None
    operation_id: str | None = None
    request_dtos: tuple[str, ...] = ()
    response_dtos: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Concept:
    id: str
    name: str
    description: str | None = None
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Decision:
    id: str
    title: str
    status: str
    adr_id: str | None = None
    path: str | None = None
    decided_at: str | None = None
    context: str | None = None
    decision: str | None = None
    consequences: str | None = None
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Edge:
    source_type: NodeType
    source_id: str
    target_type: NodeType
    target_id: str
    relation_type: str
    confidence: float
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    id: str
    workspace_id: str
    created_at: str
    repositories: tuple[Repository, ...] = ()
    files: tuple[File, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    services: tuple[Service, ...] = ()
    contracts: tuple[Contract, ...] = ()
    concepts: tuple[Concept, ...] = ()
    decisions: tuple[Decision, ...] = ()
    edges: tuple[Edge, ...] = ()
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
