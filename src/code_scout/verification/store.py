"""Append-only, local storage for immutable verification run receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from code_scout.core.errors import StorageError
from code_scout.verification.checkpoints import ContentAddressedBlobStore
from code_scout.verification.contracts import WorkspaceCheckpoint
from code_scout.verification.policy import VerificationLimits

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class RunEvidenceError(StorageError):
    """A run receipt cannot be safely persisted or inspected."""


class StreamingRedactor:
    """Redact literal secret values even when a match straddles stream chunks."""

    def __init__(self, secrets: tuple[str | bytes, ...] = ()) -> None:
        values = [
            item.encode() if isinstance(item, str) else item for item in secrets if item
        ]
        self._secrets = tuple(sorted(set(values), key=len, reverse=True))
        self._tail = b""

    def feed(self, chunk: bytes) -> bytes:
        if not self._secrets:
            return chunk
        self._tail += chunk
        return self._consume(final=False)

    def finish(self) -> bytes:
        return self._consume(final=True)

    def _consume(self, *, final: bool) -> bytes:
        output = bytearray()
        index = 0
        while index < len(self._tail):
            remaining = self._tail[index:]
            match = next(
                (secret for secret in self._secrets if remaining.startswith(secret)),
                None,
            )
            if match is not None:
                output.extend(b"[REDACTED]")
                index += len(match)
                continue
            if not final and any(
                secret.startswith(remaining) for secret in self._secrets
            ):
                break
            output.append(self._tail[index])
            index += 1
        self._tail = self._tail[index:]
        return bytes(output)


class RunArtifactWriter:
    """An unpublished run directory. It becomes visible only through ``publish``."""

    def __init__(
        self, store: RunEvidenceStore, run_id: str, checkpoint: WorkspaceCheckpoint
    ) -> None:
        _safe_name(run_id, "run ID")
        self._store = store
        self.run_id = run_id
        self.checkpoint = checkpoint
        self._payload_bytes = 0
        self._closed = False
        self._temporary = Path(
            tempfile.mkdtemp(prefix=f".{run_id}-", dir=store._temporary_root)
        )
        os.chmod(self._temporary, 0o700)

    def write_bytes(
        self, name: str, source: bytes, *, essential: bool = True
    ) -> dict[str, Any]:
        self._ensure_open()
        target = self._safe_target(name)
        if len(source) + self._payload_bytes > self._store.limits.run_payload_bytes:
            if essential:
                raise RunEvidenceError("run artifact payload exceeds frozen limit")
            return {"name": name, "omitted": True, "reason": "payload_limit"}
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        self._payload_bytes += len(source)
        return {
            "name": name,
            "sha256": hashlib.sha256(source).hexdigest(),
            "size_bytes": len(source),
        }

    def write_stream(
        self,
        name: str,
        chunks: list[bytes] | tuple[bytes, ...],
        *,
        stream_limit: int,
        secrets: tuple[str | bytes, ...] = (),
    ) -> dict[str, Any]:
        """Persist bounded, redacted output and retain original byte accounting."""

        redactor = StreamingRedactor(secrets)
        total = 0
        retained = bytearray()
        truncated = False
        for chunk in chunks:
            total += len(chunk)
            safe = redactor.feed(chunk)
            remaining = stream_limit - len(retained)
            if remaining > 0:
                retained.extend(safe[:remaining])
            if len(safe) > remaining:
                truncated = True
        safe = redactor.finish()
        remaining = stream_limit - len(retained)
        if remaining > 0:
            retained.extend(safe[:remaining])
        if len(safe) > remaining or total > stream_limit:
            truncated = True
        artifact = self.write_bytes(name, bytes(retained))
        return {**artifact, "original_size_bytes": total, "truncated": truncated}

    def publish(self, manifest: dict[str, Any]) -> dict[str, Any]:
        self._ensure_open()
        if self._store._run_root(self.run_id).exists():
            raise RunEvidenceError(f"run ID already exists: {self.run_id}")
        references = _checkpoint_references(self.checkpoint)
        self._store._validate_blobs(references)
        final_manifest = {
            "schema_version": 1,
            "run_id": self.run_id,
            "checkpoint_id": self.checkpoint.id,
            "candidate_digest": self.checkpoint.workspace_digest,
            "checkpoint_blobs": references,
            "payload_bytes": self._payload_bytes,
            **manifest,
        }
        manifest_bytes = _canonical_json(final_manifest)
        self.write_bytes("manifest.json", manifest_bytes)
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        self.write_bytes("manifest.sha256", manifest_digest.encode("ascii"))
        # ``rename`` is atomic on one filesystem; an existing target is rejected
        # above and never replaced by a subsequently-created receipt.
        try:
            os.rename(self._temporary, self._store._run_root(self.run_id))
            _fsync_directory(self._store.runs_root)
        except OSError as error:
            raise RunEvidenceError(
                f"could not publish run evidence: {error}"
            ) from error
        self._closed = True
        return {
            **final_manifest,
            "manifest_sha256": manifest_digest,
        }

    def discard(self) -> None:
        if not self._closed:
            shutil.rmtree(self._temporary, ignore_errors=True)
            self._closed = True

    def _safe_target(self, name: str) -> Path:
        target = _artifact_path(self._temporary, name)
        if target.exists() or target.is_symlink():
            raise RunEvidenceError(f"artifact already exists: {name}")
        return target

    def _ensure_open(self) -> None:
        if self._closed:
            raise RunEvidenceError("run artifact writer is closed")


class RunEvidenceStore:
    """Owner-only append-only run evidence rooted in ``.code-scout/verification``."""

    def __init__(self, workspace_root: Path, limits: VerificationLimits) -> None:
        self.root = workspace_root.resolve() / ".code-scout/verification"
        self.limits = limits
        self.runs_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.runs_root, 0o700)
        os.chmod(self._temporary_root, 0o700)

    @property
    def runs_root(self) -> Path:
        return self.root / "runs"

    @property
    def _temporary_root(self) -> Path:
        return self.root / "tmp"

    def begin(self, run_id: str, checkpoint: WorkspaceCheckpoint) -> RunArtifactWriter:
        return RunArtifactWriter(self, run_id, checkpoint)

    def inspect(self, run_id: str) -> dict[str, Any]:
        _safe_name(run_id, "run ID")
        root = self._run_root(run_id)
        manifest_path = root / "manifest.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError) as error:
            raise RunEvidenceError(f"could not read run evidence {run_id}") from error
        if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
            raise RunEvidenceError(f"invalid run manifest: {run_id}")
        try:
            expected_manifest_digest = (root / "manifest.sha256").read_text().strip()
        except OSError as error:
            raise RunEvidenceError(f"missing manifest digest: {run_id}") from error
        if hashlib.sha256(manifest_bytes).hexdigest() != expected_manifest_digest:
            raise RunEvidenceError(f"corrupt run manifest: {run_id}")
        self._validate_blobs(manifest.get("checkpoint_blobs"))
        for artifact in manifest.get("artifacts", []):
            if not isinstance(artifact, dict):
                raise RunEvidenceError("invalid run artifact record")
            name, digest = artifact.get("name"), artifact.get("sha256")
            if not isinstance(name, str) or not isinstance(digest, str):
                raise RunEvidenceError("invalid run artifact record")
            path = _artifact_path(root, name)
            if path.is_symlink() or not path.is_file():
                raise RunEvidenceError(f"missing artifact: {name}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RunEvidenceError(f"corrupt artifact: {name}")
        return manifest

    def _run_root(self, run_id: str) -> Path:
        return self.runs_root / run_id

    def _validate_blobs(self, references: object) -> None:
        if not isinstance(references, list):
            raise RunEvidenceError("invalid checkpoint blob references")
        blobs = ContentAddressedBlobStore(self.root / "blobs")
        for digest in references:
            if not isinstance(digest, str):
                raise RunEvidenceError("invalid checkpoint blob reference")
            try:
                blobs.get(digest)
            except Exception as error:
                raise RunEvidenceError(str(error)) from error


def _checkpoint_references(checkpoint: WorkspaceCheckpoint) -> list[str]:
    return sorted(
        {
            entry.content_sha256
            for member in checkpoint.members
            for entry in member.files
        }
        | {entry.content_sha256 for entry in checkpoint.workspace_files}
    )


def _safe_name(value: str, description: str) -> None:
    if not _SAFE_NAME.fullmatch(value):
        raise RunEvidenceError(f"unsafe {description}: {value}")


def _artifact_path(root: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(not _SAFE_NAME.fullmatch(part) for part in path.parts)
    ):
        raise RunEvidenceError(f"unsafe artifact path: {name}")
    current = root
    for part in path.parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise RunEvidenceError(f"unsafe artifact path: {name}")
    return root.joinpath(*path.parts)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
