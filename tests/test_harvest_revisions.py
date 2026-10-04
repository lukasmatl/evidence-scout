from __future__ import annotations

from pathlib import Path

import pytest

from code_scout.core.errors import ConfigError
from code_scout.discovery.files import DiscoveredFile
from code_scout.harvest.revisions import (
    bind_harvested_revisions,
    capture_sources_before_harvest,
)
from code_scout.models.facts import ActiveFact, FactEvidence
from code_scout.storage import get_source_blob, open_storage
from code_scout.workspace.config import create_workspace_config, load_workspace_config


def _discovered(path: Path) -> DiscoveredFile:
    return DiscoveredFile(
        repository_id="repo",
        path="source.py",
        absolute_path=str(path),
        kind="source",
        extension=".py",
        content_hash="ignored",
        modified_at="ignored",
    )


def _fact(*, extractor: str = "tree_sitter") -> ActiveFact:
    return ActiveFact(
        id="fact:source",
        extractor=extractor,
        kind="symbol",
        source_file="source.py",
        identity={"entity_type": "symbol", "entity_id": "symbol:source"},
        properties={},
        evidence=(
            FactEvidence(
                file_path="source.py",
                extractor_name=extractor,
                confidence=1.0,
            ),
        ),
        confidence=1.0,
    )


def test_binds_unchanged_local_text_to_retained_revision(tmp_path) -> None:
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    captures = capture_sources_before_harvest((_discovered(source),), max_bytes=1024)

    with open_storage(tmp_path) as connection:
        bound = bind_harvested_revisions(
            connection, (_fact(),), before=captures, max_bytes=1024
        )
        metadata = bound[0].evidence[0].metadata
        assert metadata["repository_id"] == "repo"
        assert metadata["content_sha256"]
        assert (
            get_source_blob(connection, metadata["content_sha256"]).bytes
            == b"value = 1\n"
        )


def test_marks_racing_and_unavailable_sources_without_retaining_bytes(tmp_path) -> None:
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    captures = capture_sources_before_harvest((_discovered(source),), max_bytes=1024)
    source.write_text("value = 2\n", encoding="utf-8")

    with open_storage(tmp_path) as connection:
        racing = bind_harvested_revisions(
            connection, (_fact(),), before=captures, max_bytes=1024
        )
        assert (
            racing[0].evidence[0].metadata["source_revision_unavailable_reason"]
            == "source_changed_during_extraction"
        )
        assert (
            connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0]
            == 0
        )

    source.write_bytes(b"\0binary")
    captures = capture_sources_before_harvest((_discovered(source),), max_bytes=1024)
    with open_storage(tmp_path) as connection:
        unavailable = bind_harvested_revisions(
            connection, (_fact(),), before=captures, max_bytes=1024
        )
        assert (
            unavailable[0].evidence[0].metadata["source_revision_unavailable_reason"]
            == "source_binary"
        )


def test_external_provider_evidence_never_borrows_later_file_bytes(tmp_path) -> None:
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    captures = capture_sources_before_harvest((_discovered(source),), max_bytes=1024)

    with open_storage(tmp_path) as connection:
        bound = bind_harvested_revisions(
            connection, (_fact(extractor="graphify"),), before=captures, max_bytes=1024
        )
        assert (
            bound[0].evidence[0].metadata["source_revision_unavailable_reason"]
            == "external_provider_input_unbound"
        )
        assert (
            connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0]
            == 1
        )


def test_retention_limit_is_configured_and_oversized_sources_are_unavailable(
    tmp_path,
) -> None:
    create_workspace_config(tmp_path)
    config_path = tmp_path / ".code-scout" / "config.yaml"
    config_path.write_text(config_path.read_text().replace("1048576", "4"))
    assert load_workspace_config(tmp_path).source_retention.max_bytes == 4

    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    capture = capture_sources_before_harvest((_discovered(source),), max_bytes=4)[0]
    assert capture.unavailable_reason == "source_too_large"

    config_path.write_text(
        config_path.read_text().replace("max_bytes: 4", "max_bytes: 0")
    )
    with pytest.raises(ConfigError):
        load_workspace_config(tmp_path)
