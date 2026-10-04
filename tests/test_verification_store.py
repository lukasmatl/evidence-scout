from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from code_scout.verification import (
    CapturedFile,
    CapturedMember,
    MemberRole,
    RunEvidenceError,
    RunEvidenceStore,
    StreamingRedactor,
    VerificationLimits,
    WorkspaceCheckpoint,
)


def _checkpoint(source: bytes = b"baseline") -> WorkspaceCheckpoint:
    digest = hashlib.sha256(source).hexdigest()
    return WorkspaceCheckpoint(
        id="checkpoint-test",
        workspace_digest="candidate-digest",
        members=(
            CapturedMember(
                id="repo",
                role=MemberRole.EDITABLE,
                worktree_identity="git-dir",
                files=(CapturedFile("input.py", digest, 0o644, len(source)),),
                input_scope=("input.py",),
                content_digest="member-digest",
            ),
        ),
    )


def _store(tmp_path: Path, *, payload: int = 1024) -> RunEvidenceStore:
    return RunEvidenceStore(
        tmp_path,
        VerificationLimits(run_payload_bytes=payload, stdout_bytes=8, stderr_bytes=8),
    )


def _baseline(tmp_path: Path, source: bytes = b"baseline") -> None:
    digest = hashlib.sha256(source).hexdigest()
    path = tmp_path / ".code-scout/verification/blobs" / digest[:2] / digest[2:]
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_bytes(source)


def test_publishes_owner_only_run_with_digests_and_checkpoint_references(
    tmp_path: Path,
) -> None:
    _baseline(tmp_path)
    store = _store(tmp_path)
    writer = store.begin("run-one", _checkpoint())
    artifact = writer.write_bytes("gate.json", b'{"outcome":"PASS"}')
    manifest = writer.publish({"artifacts": [artifact], "outcome": "VERIFIED"})

    assert manifest["checkpoint_blobs"]
    assert store.inspect("run-one")["outcome"] == "VERIFIED"
    assert (
        tmp_path / ".code-scout/verification/runs/run-one"
    ).stat().st_mode & 0o077 == 0
    assert (
        tmp_path / ".code-scout/verification/runs/run-one/gate.json"
    ).stat().st_mode & 0o077 == 0


def test_streaming_redaction_and_truncation_cross_chunk_boundaries(
    tmp_path: Path,
) -> None:
    _baseline(tmp_path)
    writer = _store(tmp_path).begin("run-stream", _checkpoint())
    record = writer.write_stream(
        "stdout.log",
        [b"token=se", b"cret; 123456789"],
        stream_limit=8,
        secrets=("secret",),
    )
    assert record["truncated"] is True
    assert b"secret" not in (writer._temporary / "stdout.log").read_bytes()


def test_redactor_keeps_partial_secret_until_next_chunk() -> None:
    redactor = StreamingRedactor(("secret",))
    assert redactor.feed(b"before se") == b"before "
    assert redactor.feed(b"cret after") == b"[REDACTED] after"
    assert redactor.finish() == b""


def test_publish_rejects_missing_or_corrupt_baseline_blob(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RunEvidenceError, match="missing content-addressed"):
        store.begin("run-missing", _checkpoint()).publish({"artifacts": []})
    _baseline(tmp_path)
    digest = hashlib.sha256(b"baseline").hexdigest()
    blob = tmp_path / ".code-scout/verification/blobs" / digest[:2] / digest[2:]
    blob.write_bytes(b"corrupt")
    with pytest.raises(RunEvidenceError, match="corrupt content-addressed"):
        store.begin("run-corrupt", _checkpoint()).publish({"artifacts": []})


def test_rejects_unsafe_paths_and_attempted_overwrite(tmp_path: Path) -> None:
    _baseline(tmp_path)
    store = _store(tmp_path)
    writer = store.begin("run-safe", _checkpoint())
    with pytest.raises(RunEvidenceError, match="unsafe artifact path"):
        writer.write_bytes("../escape", b"no")
    (writer._temporary / "linked").symlink_to(tmp_path)
    with pytest.raises(RunEvidenceError, match="unsafe artifact path"):
        writer.write_bytes("linked/escape", b"no")
    artifact = writer.write_bytes("gate.log", b"first")
    with pytest.raises(RunEvidenceError, match="already exists"):
        writer.write_bytes("gate.log", b"second")
    writer.publish({"artifacts": [artifact]})
    with pytest.raises(RunEvidenceError, match="already exists"):
        store.begin("run-safe", _checkpoint()).publish({"artifacts": []})


def test_interrupted_or_failed_write_leaves_no_published_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _baseline(tmp_path)
    store = _store(tmp_path)
    writer = store.begin("run-interrupted", _checkpoint())
    original = os.fsync

    def fail_fsync(descriptor: int) -> None:
        raise OSError("disk interrupted")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="disk interrupted"):
        writer.write_bytes("gate.log", b"output")
    monkeypatch.setattr(os, "fsync", original)
    writer.discard()
    assert not (tmp_path / ".code-scout/verification/runs/run-interrupted").exists()
