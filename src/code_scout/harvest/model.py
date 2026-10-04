from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from code_scout.extractors.asyncapi import AsyncAPIWarning
from code_scout.extractors.graphify import GraphifyWarning
from code_scout.extractors.lsp import TypeScriptLSPRun
from code_scout.extractors.openapi import OpenAPIWarning
from code_scout.extractors.package_json import RepositoryPackageFacts
from code_scout.models.facts import ActiveFact
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
from code_scout.models.workspace import Workspace

HarvestWarning: TypeAlias = OpenAPIWarning | AsyncAPIWarning | GraphifyWarning


@dataclass(frozen=True)
class PackageFactCounts:
    repositories_detected: int = 0
    package_files: int = 0


@dataclass(frozen=True)
class HarvestGraphFragment:
    repositories: tuple[Repository, ...] = ()
    files: tuple[File, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    services: tuple[Service, ...] = ()
    contracts: tuple[Contract, ...] = ()
    concepts: tuple[Concept, ...] = ()
    decisions: tuple[Decision, ...] = ()
    edges: tuple[Edge, ...] = ()


@dataclass(frozen=True)
class LSPValidationInputs:
    runs: tuple[TypeScriptLSPRun, ...] = ()
    authoritative_symbols: tuple[Symbol, ...] = ()
    authoritative_edges: tuple[Edge, ...] = ()


@dataclass(frozen=True)
class EvidenceHarvestResult:
    active_facts: tuple[ActiveFact, ...] = ()
    graph_fragment: HarvestGraphFragment = HarvestGraphFragment()
    warnings: tuple[HarvestWarning, ...] = ()
    package_counts: PackageFactCounts = PackageFactCounts()
    package_facts: tuple[RepositoryPackageFacts, ...] = ()
    lsp_validation_inputs: LSPValidationInputs | None = None


@dataclass(frozen=True)
class ScanCounts:
    files: int = 0
    package_files: int = 0
    repositories_detected: int = 0
    symbols: int = 0
    services: int = 0
    contracts: int = 0
    concepts: int = 0
    decisions: int = 0
    edges: int = 0


@dataclass(frozen=True)
class ScanResult:
    workspace: Workspace
    snapshot_id: str
    extractor: str | None = None
    counts: ScanCounts = ScanCounts()
    warnings: tuple[HarvestWarning, ...] = ()

    def summary(self) -> str:
        extractor_label = self.extractor or "all enabled extractors"
        lines = [
            "Scan complete.",
            f"Workspace: {self.workspace.name} ({self.workspace.id})",
            f"Snapshot: {self.snapshot_id}",
            f"Extractor: {extractor_label}",
            f"Files analyzed: {self.counts.files}",
            f"Package files analyzed: {self.counts.package_files}",
            f"Repositories detected: {self.counts.repositories_detected}",
            f"Symbols: {self.counts.symbols}",
            f"Services detected: {self.counts.services}",
            f"Contracts detected: {self.counts.contracts}",
            f"ADRs detected: {self.counts.decisions}",
            f"Concepts detected: {self.counts.concepts}",
            f"Edges created: {self.counts.edges}",
            f"Warnings: {len(self.warnings)}",
        ]
        lines.extend(
            f"- {warning.path}: {warning.message}" for warning in self.warnings
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class IncrementalFileWarning:
    path: str
    message: str
    hint: str | None = None


@dataclass(frozen=True)
class ChangedFileReplacementResult:
    facts_added: int
    facts_removed: int
    nodes_updated: int
    change_event_id: str | None = None
    warnings: tuple[HarvestWarning | IncrementalFileWarning, ...] = ()
