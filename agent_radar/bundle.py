"""Publish snapshot, provenance manifest and commit as one visible offline bundle.

The staged directory is hidden by convention and must be ignored by consumers.
A same-parent rename publishes all three names together on a local filesystem;
this is not a cross-process lock, power-loss durability guarantee or attestation
of source completeness/licensing. The offline importer does not call a provider.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_radar.snapshot import MAX_SNAPSHOT_BYTES, SnapshotStore
from agent_radar.source_export import MAX_SOURCE_EXPORT_BYTES, _read_bounded_json, build_snapshot


class PinnedBundleStore(SnapshotStore):
    """SnapshotStore that checks all bundle-published hashes on *every* read."""

    def __init__(self, snapshot_path: Path, pinned_hash: str, manifest_hash: str) -> None:
        super().__init__(snapshot_path)
        self.pinned_hash = pinned_hash
        self.manifest_hash = manifest_hash

    def _load(self) -> tuple[list[dict[str, Any]], str]:
        commit, _ = _read_json_file(self.path.parent / "commit.json", max_bytes=2_000, label="commit")
        if (set(commit) != {"schema_version", "snapshot_sha256", "manifest_sha256"}
                or commit["schema_version"] != "agent-radar-bundle-v1"
                or commit["snapshot_sha256"] != self.pinned_hash
                or commit["manifest_sha256"] != self.manifest_hash):
            raise ValueError("bundle commit changed after verification")
        _, manifest_bytes = _read_json_file(self.path.parent / "manifest.json", max_bytes=MAX_SOURCE_EXPORT_BYTES, label="manifest")
        if hashlib.sha256(manifest_bytes).hexdigest() != self.manifest_hash:
            raise ValueError("bundle manifest checksum mismatch")
        rows, observed = super()._load()
        if observed != self.pinned_hash:
            raise ValueError("bundle snapshot changed after verification")
        return rows, observed


def _read_json_file(path: Path, *, max_bytes: int, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"bundle {label} must be a regular file")
    with path.open("rb") as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"bundle {label} too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid bundle {label}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid bundle {label}")
    return payload, raw


def open_verified_bundle(bundle_path: str | Path) -> tuple[SnapshotStore, dict[str, Any]]:
    """Refuse missing/staged bundles; verify the companion is pinned to snapshot bytes."""
    directory = Path(bundle_path)
    if directory.name.startswith(".") or not directory.is_dir() or directory.is_symlink():
        raise ValueError("published bundle must be a regular, non-staging directory")
    snapshot_path = directory / "snapshot.json"
    manifest, manifest_bytes = _read_json_file(directory / "manifest.json", max_bytes=MAX_SOURCE_EXPORT_BYTES, label="manifest")
    commit, _ = _read_json_file(directory / "commit.json", max_bytes=2_000, label="commit")
    if set(commit) != {"schema_version", "snapshot_sha256", "manifest_sha256"} or commit["schema_version"] != "agent-radar-bundle-v1":
        raise ValueError("invalid bundle commit")
    observed_manifest = hashlib.sha256(manifest_bytes).hexdigest()
    if commit["manifest_sha256"] != observed_manifest:
        raise ValueError("bundle manifest checksum mismatch")
    store = SnapshotStore(snapshot_path)
    observed = store.list_candidates(limit=1)["snapshot_sha256"]
    digest = manifest.get("snapshot_sha256")
    if not isinstance(digest, str) or digest != observed or commit["snapshot_sha256"] != observed:
        raise ValueError("bundle snapshot/manifest hash mismatch")
    return PinnedBundleStore(snapshot_path, observed, observed_manifest), manifest


def _publish_directory(stage: Path, destination: Path) -> None:
    """Publish without replacing a competitor's directory; fail closed on unknown OS."""
    if sys.platform == "win32":
        # Windows MoveFileEx/rename refuses an existing destination directory.
        stage.rename(destination)
    elif sys.platform == "linux":
        # Atomic RENAME_NOREPLACE; Python os.rename may replace an empty directory.
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 unavailable; refusing unsafe publication")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    else:
        raise OSError(errno.ENOSYS, "atomic no-replace directory rename unavailable on this OS")


def materialize_bundle(input_path: str | Path, bundle_path: str | Path, *, max_search_results: int) -> dict[str, Any]:
    """Create a new three-file bundle with a single directory-level visibility point.

    Requires a cooperating single-writer operator and a private destination parent;
    stale hidden stages left by hard termination need a separate recovery policy.
    """
    input_path = Path(input_path)
    bundle_path = Path(bundle_path)
    if not bundle_path.name or bundle_path.name.startswith(".") or bundle_path.exists() or bundle_path.is_symlink():
        raise ValueError("invalid bundle destination or destination already exists")
    parent = bundle_path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("bundle parent must exist and be a real directory")
    raw, export_sha256 = _read_bounded_json(input_path, max_bytes=MAX_SOURCE_EXPORT_BYTES)
    snapshot, manifest = build_snapshot(raw, max_search_results=max_search_results)
    snapshot_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(snapshot_bytes) > MAX_SNAPSHOT_BYTES:
        raise ValueError("materialized snapshot too large")
    manifest["snapshot_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
    manifest["source_export_sha256"] = export_sha256
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    commit = {"schema_version": "agent-radar-bundle-v1", "snapshot_sha256": manifest["snapshot_sha256"],
              "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    commit_bytes = json.dumps(commit, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stage = Path(tempfile.mkdtemp(prefix=f".{bundle_path.name}.staging-", dir=parent))
    try:
        if os.name == "posix":
            stage.chmod(0o700)
        for name, data in (("snapshot.json", snapshot_bytes), ("manifest.json", manifest_bytes), ("commit.json", commit_bytes)):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(stage / name, flags, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        # The stage is not a published bundle yet: compare the actual persisted
        # snapshot, manifest and commit bytes against the values about to publish.
        actual_hash = SnapshotStore(stage / "snapshot.json").list_candidates(limit=1)["snapshot_sha256"]
        staged_manifest, persisted_manifest = _read_json_file(stage / "manifest.json", max_bytes=MAX_SOURCE_EXPORT_BYTES, label="manifest")
        staged_commit, persisted_commit = _read_json_file(stage / "commit.json", max_bytes=2_000, label="commit")
        if actual_hash != manifest["snapshot_sha256"]:
            raise ValueError("bundle snapshot checksum mismatch before publication")
        if persisted_manifest != manifest_bytes or staged_manifest != manifest:
            raise ValueError("bundle manifest checksum mismatch before publication")
        if persisted_commit != commit_bytes or staged_commit != commit:
            raise ValueError("bundle commit checksum mismatch before publication")
        if bundle_path.exists() or bundle_path.is_symlink():
            raise ValueError("bundle destination already exists")
        _publish_directory(stage, bundle_path)
    finally:
        # An ordinary exception cleans only our own stage. An abrupt termination
        # may strand a hidden stage; it is never interpreted as a published bundle.
        if stage.exists():
            shutil.rmtree(stage)
    return manifest
