from __future__ import annotations

import os
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path


def test_impact_modules_use_destination_paths_without_root_shims() -> None:
    """The complete impact workflow has one direct package boundary."""
    from code_scout.impact.git import collect_git_changes
    from code_scout.impact.mapping import map_changed_ranges_to_symbols
    from code_scout.impact.paths import trace_impact_paths
    from code_scout.impact.report import generate_impact_report

    assert collect_git_changes.__module__ == "code_scout.impact.git"
    assert map_changed_ranges_to_symbols.__module__ == "code_scout.impact.mapping"
    assert trace_impact_paths.__module__ == "code_scout.impact.paths"
    assert generate_impact_report.__module__ == "code_scout.impact.report"
    for legacy_name in (
        "code_scout.git_changes",
        "code_scout.change_mapping",
        "code_scout.impact_paths",
        "code_scout.impact_report",
    ):
        assert find_spec(legacy_name) is None


def test_evidence_modules_use_destination_paths_without_root_shims() -> None:
    """Evidence reading has one direct package boundary."""
    from code_scout.evidence.assessment import assess_legacy_evidence
    from code_scout.evidence.embeddings import LocalEmbeddingProvider
    from code_scout.evidence.pack import compile_evidence_pack
    from code_scout.evidence.query import search_semantic_evidence

    assert LocalEmbeddingProvider.__module__ == "code_scout.evidence.embeddings"
    assert assess_legacy_evidence.__module__ == "code_scout.evidence.assessment"
    assert compile_evidence_pack.__module__ == "code_scout.evidence.pack"
    assert search_semantic_evidence.__module__ == "code_scout.evidence.query"
    for legacy_name in (
        "code_scout.embeddings",
        "code_scout.evidence_assessment",
        "code_scout.evidence_pack",
        "code_scout.semantic_query",
        "code_scout.evidence.evidence_assessment",
        "code_scout.evidence.evidence_pack",
        "code_scout.evidence.semantic_query",
    ):
        assert find_spec(legacy_name) is None


def test_evidence_navigation_modules_use_direct_destination_paths() -> None:
    """Evidence tools and navigators have one direct package boundary."""
    from code_scout.evidence.navigation import CodeScoutSymbolNavigator
    from code_scout.evidence.serena import configured_symbol_navigator
    from code_scout.evidence.tools import EvidenceTools

    assert EvidenceTools.__module__ == "code_scout.evidence.tools"
    assert configured_symbol_navigator.__module__ == "code_scout.evidence.serena"
    assert CodeScoutSymbolNavigator.__module__ == "code_scout.evidence.navigation"
    for legacy_name in (
        "code_scout.evidence_tools",
        "code_scout.symbol_navigation",
        "code_scout.serena_navigation",
        "code_scout.evidence.evidence_tools",
        "code_scout.evidence.symbol_navigation",
        "code_scout.evidence.serena_navigation",
    ):
        assert find_spec(legacy_name) is None


def test_foundational_modules_use_destination_paths_without_root_shims() -> None:
    """Shared errors and workspace state paths live in their owning packages."""
    from code_scout.core.errors import ConfigError
    from code_scout.workspace.paths import workspace_state_path

    assert ConfigError("invalid configuration").message == "invalid configuration"
    assert workspace_state_path("workspace", ".code-scout/config.yaml").as_posix() == (
        "workspace/.code-scout/config.yaml"
    )


def test_workspace_configuration_and_status_use_destination_paths() -> None:
    """Workspace-local state is owned directly by the workspace package."""
    from code_scout.workspace.config import WorkspaceConfig
    from code_scout.workspace.status import WorkspaceStatusReport

    assert WorkspaceConfig().id == "workspace"
    assert WorkspaceStatusReport({}, {}, {}, {}, {}).to_dict()["schema_version"] == "1"


def test_runtime_config_uses_owning_path_without_root_shim() -> None:
    """User runtime configuration is imported from its owning module."""
    from code_scout.runtime.config import global_config_path

    assert global_config_path.__module__ == "code_scout.runtime.config"
    assert find_spec("code_scout.global_config") is None


def test_dede_provider_uses_owning_path_without_root_shim() -> None:
    """The dede adapter is imported from the engines package."""
    from code_scout.engines.dede import dede_confidence

    assert dede_confidence.__module__ == "code_scout.engines.dede"
    assert find_spec("code_scout.dede_enrichment") is None


def test_cli_and_doctor_use_commands_paths_without_root_shims() -> None:
    """Process adaptation and readiness diagnostics are command-owned."""
    from code_scout.commands.cli import app
    from code_scout.commands.doctor import doctor

    assert app.__module__ == "code_scout.commands.cli"
    assert doctor.__module__ == "code_scout.commands.doctor"
    assert find_spec("code_scout.cli") is None
    assert find_spec("code_scout.doctor") is None


def test_destination_package_initialization_is_acyclic() -> None:
    """Destination package initializers import without relying on root shims."""
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib; "
            "[importlib.import_module(name) for name in ("
            "'code_scout.evidence', 'code_scout.impact', "
            "'code_scout.workspace', 'code_scout.runtime', "
            "'code_scout.commands', 'code_scout.engines')];",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=project_root,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
