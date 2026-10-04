"""Complete local, read-only Git change impact reports."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from code_scout.core.errors import CodeScoutError, CommandUsageError
from code_scout.impact.git import GitChangeSelection, collect_git_changes
from code_scout.impact.mapping import (
    ChangeSeedResult,
    map_changed_ranges_to_symbols,
)
from code_scout.impact.paths import (
    ImpactPath,
    ImpactPathResult,
    trace_impact_paths,
)
from code_scout.workspace.status import (
    WorkspaceStatusReport,
    workspace_status,
)

IMPACT_REPORT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ImpactReport:
    selection: GitChangeSelection
    status: WorkspaceStatusReport
    seeds: ChangeSeedResult | None
    impact: ImpactPathResult | None
    max_hops: int
    max_results: int

    def to_dict(self) -> dict[str, Any]:
        paths = self.impact.paths if self.impact is not None else ()
        return {
            "schema_version": IMPACT_REPORT_SCHEMA_VERSION,
            "repository": asdict(self.selection.repository),
            "comparison": asdict(self.selection.comparison),
            "index": {
                "state": self.status.index.get("state", "unknown"),
                "freshness": self.status.freshness,
            },
            "changed_files": (
                [asdict(item) for item in self.seeds.files]
                if self.seeds is not None
                else [asdict(item) for item in self.selection.files]
            ),
            "changed_symbols": (
                [asdict(item) for item in self.seeds.symbols]
                if self.seeds is not None
                else []
            ),
            "affected_entities": _affected_entities(paths),
            "affected_contracts": _path_dicts(
                self.impact.affected_contracts if self.impact is not None else ()
            ),
            "affected_services": _path_dicts(
                self.impact.affected_services if self.impact is not None else ()
            ),
            "test_candidates": {
                "evidence_linked": _path_dicts(
                    self.impact.test_candidates if self.impact is not None else ()
                ),
                "inferred": (
                    [asdict(item) for item in self.impact.inferred_test_candidates]
                    if self.impact is not None
                    else []
                ),
            },
            "paths": _path_dicts(paths),
            "category_coverage": (
                self.impact.category_coverage if self.impact is not None else {}
            ),
            "coverage_gaps": (
                [asdict(item) for item in self.impact.coverage_gaps]
                if self.impact is not None
                else []
            ),
            "truncation": {
                "truncated": self.impact.truncated
                if self.impact is not None
                else False,
                "max_hops": self.max_hops,
                "max_results": self.max_results,
            },
        }

    def render(self, output_format: str = "text") -> str:
        if output_format == "json":
            return json.dumps(self.to_dict(), indent=2, sort_keys=True)
        if output_format != "text":
            raise CommandUsageError(
                f"Unsupported impact format {output_format!r}.",
                "Use `--format text` or `--format json`.",
            )
        return _render_text(self)


def generate_impact_report(
    workspace: str | Path,
    *,
    staged: bool = False,
    base_ref: str | None = None,
    max_hops: int = 2,
    max_results: int = 100,
) -> ImpactReport:
    """Collect, map, and traverse one explicit comparison without side effects."""
    _validate_request(staged, base_ref, max_hops, max_results)
    selection = collect_git_changes(workspace, staged=staged, base_ref=base_ref)
    status = workspace_status(workspace)
    if not selection.files:
        return ImpactReport(selection, status, None, None, max_hops, max_results)
    if status.index.get("state") != "ready":
        _raise_index_unavailable(workspace, status)
    seeds = map_changed_ranges_to_symbols(workspace, selection, status)
    if any(gap.code == "index_unavailable" for gap in seeds.coverage_gaps):
        _raise_index_unavailable(workspace, status)
    impact = trace_impact_paths(
        workspace,
        seeds,
        status,
        max_hops=max_hops,
        max_results=max_results,
    )
    if any(
        gap.code == "relationship_index_unavailable" for gap in impact.coverage_gaps
    ):
        _raise_index_unavailable(workspace, status)
    return ImpactReport(selection, status, seeds, impact, max_hops, max_results)


def _raise_index_unavailable(
    workspace: str | Path,
    status: WorkspaceStatusReport,
) -> None:
    remediation = next(
        iter(status.recommendations),
        f"code-scout scan --workspace {Path(workspace).expanduser().resolve()}",
    )
    raise CodeScoutError(
        "The code-scout index is unavailable for impact analysis "
        f"(state: {status.index.get('state', 'unknown')}).",
        f"Run `{remediation}` and retry the impact command.",
    )


def _validate_request(
    staged: bool,
    base_ref: str | None,
    max_hops: int,
    max_results: int,
) -> None:
    if staged == (base_ref is not None):
        raise CommandUsageError(
            "Select exactly one Git change mode: --staged or --base REF.",
            "Run `code-scout impact --help` for usage.",
        )
    if base_ref is not None and not base_ref.strip():
        raise CommandUsageError(
            "The Git base ref must not be empty.",
            "Pass a branch, tag, or commit such as `main` or `HEAD~1`.",
        )
    if (
        not isinstance(max_hops, int)
        or isinstance(max_hops, bool)
        or not 1 <= max_hops <= 5
    ):
        raise CommandUsageError(
            "--max-hops must be between 1 and 5.",
            "Pass an integer from 1 through 5.",
        )
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or not 1 <= max_results <= 1000
    ):
        raise CommandUsageError(
            "--max-results must be between 1 and 1000.",
            "Pass an integer from 1 through 1000.",
        )


def _path_dicts(paths: tuple[ImpactPath, ...]) -> list[dict[str, Any]]:
    return [asdict(path) for path in paths]


def _affected_entities(paths: tuple[ImpactPath, ...]) -> list[dict[str, Any]]:
    entities: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        key = (path.affected_entity.type, path.affected_entity.id)
        entities.setdefault(
            key,
            {
                **asdict(path.affected_entity),
                "certainty": path.certainty,
                "path_count": 0,
            },
        )
        entities[key]["path_count"] += 1
    return [entities[key] for key in sorted(entities)]


def _render_text(report: ImpactReport) -> str:
    comparison = report.selection.comparison
    freshness = report.status.freshness
    lines = [
        "Impact report:",
        f"Comparison: {_comparison_label(comparison)}",
        "Repository: "
        f"{report.selection.repository.id} ({report.selection.repository.root})",
        f"Index: {report.status.index.get('state', 'unknown')}",
        "Freshness: "
        f"{freshness.get('state', 'unknown')} "
        f"({freshness.get('coverage', 'unknown')})",
        f"Changed files: {len(report.selection.files)}",
    ]
    lines.extend(_freshness_reason_lines(freshness))
    if not report.selection.files:
        lines.extend(
            (
                "No changes detected for the selected Git comparison.",
                f"Traversal: max {report.max_hops} hops, "
                f"max {report.max_results} results; not truncated.",
            )
        )
        return "\n".join(lines)

    assert report.seeds is not None and report.impact is not None
    for changed in report.seeds.files:
        path = changed.new_path or changed.old_path or "<unknown>"
        detail = f" ({changed.mapping_reason})" if changed.mapping_reason else ""
        lines.append(
            f"- {changed.change_kind} {path}: {changed.mapping_status}{detail}"
        )
    lines.append(f"Changed symbols: {len(report.seeds.symbols)}")
    for symbol in report.seeds.symbols:
        lines.append(
            f"- {symbol.path}:{symbol.start_line}-{symbol.end_line} "
            f"{symbol.qualified_name or symbol.name}"
        )

    lines.extend(
        _category_lines("Affected contracts", report.impact.affected_contracts)
    )
    lines.extend(_category_lines("Affected services", report.impact.affected_services))
    lines.extend(
        _category_lines(
            "Evidence-linked test candidates", report.impact.test_candidates
        )
    )
    lines.append(
        f"Inferred test candidates: {len(report.impact.inferred_test_candidates)}"
    )
    for candidate in report.impact.inferred_test_candidates:
        lines.append(f"- {candidate.candidate.path}: {candidate.heuristic}")

    lines.append(f"Cited impact paths: {len(report.impact.paths)}")
    for index, path in enumerate(report.impact.paths, start=1):
        lines.append(
            f"- [{index}] {path.changed_seed.name} -> {path.affected_entity.name} "
            f"({_labels(path.labels)}, {len(path.relationships)} hop(s))"
        )
        for citation in path.citations:
            citation_label = _citation_label(
                citation.path,
                citation.start_line,
                citation.end_line,
            )
            lines.append(f"  Citation: {citation_label}")
        for relationship in path.relationships:
            lines.append(
                f"  Edge: {relationship.dependency.name} "
                f"-[{relationship.relation}/{_labels(relationship.labels)}]-> "
                f"{relationship.dependent.name}"
            )
        for limitation in path.freshness_limitations:
            lines.append(f"  Limitation: {limitation}")

    lines.append(f"Coverage gaps: {len(report.impact.coverage_gaps)}")
    for gap in report.impact.coverage_gaps:
        location = f" ({gap.path})" if gap.path else ""
        lines.append(f"- [{gap.code}]{location} {gap.message}")
    lines.append("Category coverage:")
    for category, coverage in sorted(report.impact.category_coverage.items()):
        detail = (
            f" - {coverage['reason']}" if coverage.get("reason") is not None else ""
        )
        lines.append(f"- {category}: {coverage.get('state', 'unknown')}{detail}")
    truncation = "truncated" if report.impact.truncated else "not truncated"
    lines.append(
        f"Traversal: max {report.max_hops} hops, "
        f"max {report.max_results} results; {truncation}."
    )
    return "\n".join(lines)


def _comparison_label(comparison: Any) -> str:
    old = comparison.old.label
    if comparison.old.commit is not None:
        old += f"@{comparison.old.commit}"
    new = comparison.new.label
    if comparison.new.commit is not None:
        new += f"@{comparison.new.commit}"
    label = f"{comparison.mode} ({old} -> {new})"
    if comparison.requested_base_ref is not None:
        label += (
            f", base {comparison.requested_base_ref} "
            f"at {comparison.resolved_base_commit}"
        )
    return label


def _category_lines(label: str, paths: tuple[ImpactPath, ...]) -> list[str]:
    lines = [f"{label}: {len(paths)}"]
    lines.extend(
        f"- {path.affected_entity.name} ({_labels(path.labels)}; cited path)"
        for path in paths
    )
    return lines


def _labels(labels: tuple[str, ...]) -> str:
    return ", ".join(labels) if labels else "unknown"


def _citation_label(path: str, start: int | None, end: int | None) -> str:
    if start is None:
        return path
    if end is None or end == start:
        return f"{path}:{start}"
    return f"{path}:{start}-{end}"


def _freshness_reason_lines(freshness: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for reason in freshness.get("coverage_reasons", ()):
        if isinstance(reason, dict):
            message = reason.get("message")
        else:
            message = reason
        if isinstance(message, str) and message:
            lines.append(f"Freshness limitation: {message}")
    return lines
