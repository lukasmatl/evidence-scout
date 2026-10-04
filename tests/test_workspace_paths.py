from __future__ import annotations

from pathlib import Path

import pytest

from code_scout.core.errors import ConfigError
from code_scout.storage import database_path, open_storage
from code_scout.workspace.config import create_workspace_config, load_workspace_config
from code_scout.workspace.paths import (
    CACHE_DIRECTORY,
    FASTCONTEXT_TRAJECTORY_DIRECTORY,
    LOCK_DIRECTORY,
    LOG_DIRECTORY,
    TRAJECTORY_DIRECTORY,
    VERIFY_TRAJECTORY_DIRECTORY,
    WORKSPACE_STATE_DIRECTORY,
    WORKSPACE_STATE_PATHS,
)
from tests.identity_boundaries import RETIRED_STATE_DIRECTORY


def test_every_canonical_workspace_state_path_is_beneath_code_scout() -> None:
    state_root = Path(WORKSPACE_STATE_DIRECTORY)

    assert state_root == Path(".code-scout")
    assert {
        CACHE_DIRECTORY,
        LOCK_DIRECTORY,
        LOG_DIRECTORY,
        TRAJECTORY_DIRECTORY,
        FASTCONTEXT_TRAJECTORY_DIRECTORY,
        VERIFY_TRAJECTORY_DIRECTORY,
    } <= set(WORKSPACE_STATE_PATHS)
    for relative_path in WORKSPACE_STATE_PATHS:
        assert not Path(relative_path).is_absolute()
        assert Path(relative_path).is_relative_to(state_root)


def test_initialization_creates_only_code_scout_workspace_state(tmp_path) -> None:
    config_path = create_workspace_config(tmp_path)
    with open_storage(tmp_path):
        pass

    state_root = tmp_path / WORKSPACE_STATE_DIRECTORY
    assert config_path == state_root / "config.yaml"
    assert database_path(tmp_path) == state_root / "code-scout.db"
    assert {path.name for path in tmp_path.iterdir()} == {WORKSPACE_STATE_DIRECTORY}
    assert all(path.is_relative_to(state_root) for path in state_root.rglob("*"))


def test_legacy_workspace_state_is_not_discovered_or_modified(tmp_path) -> None:
    legacy_config = tmp_path / RETIRED_STATE_DIRECTORY / "config.yaml"
    legacy_config.parent.mkdir()
    legacy_config.write_text("legacy sentinel\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="config not found"):
        load_workspace_config(tmp_path)

    create_workspace_config(tmp_path)
    with open_storage(tmp_path):
        pass

    assert legacy_config.read_text(encoding="utf-8") == "legacy sentinel\n"
    assert set((tmp_path / RETIRED_STATE_DIRECTORY).iterdir()) == {legacy_config}
    assert database_path(tmp_path).is_file()
