"""Synthetic bundle publisher for offline tests; no marketplace connector or real data."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from agent_radar.bundle import _publish_directory, _read_json_file, open_verified_bundle
from agent_radar.snapshot import SnapshotStore


def publish(target: Path, title: str = "Поставка серверов") -> dict:
    if target.exists() or target.is_symlink():
        raise ValueError("bundle destination already exists")
    snapshot = {"schema_version": "agent-radar-snapshot-v1", "tenders": [
        {"id": "example-source:101", "title": title, "customer_name": "Учебный центр Альфа",
         "source_url": "https://tenders.example.org/101", "description": "Нужны два сервера",
         "documents": [{"id": "doc-1", "title": "ТЗ", "text": "Заказчику требуется увеличение ёмкости"}]},
        {"id": "example-source:102", "title": "Поставка СХД", "customer_name": "Учебный завод Бета",
         "source_url": "https://tenders.example.org/102", "description": "", "documents": []}]}
    snapshot_bytes = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
    manifest = {"source": "synthetic", "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
                "coverage": {"from_date": "2026-09-19T00:00:00Z", "completed_cursor": "2026-10-03T00:00:00Z"}}
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    commit = {"schema_version": "agent-radar-bundle-v1", "snapshot_sha256": manifest["snapshot_sha256"],
              "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}
    commit_bytes = json.dumps(commit, sort_keys=True).encode("utf-8")
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    try:
        if os.name == "posix":
            stage.chmod(0o700)
        for name, data in (("snapshot.json", snapshot_bytes), ("manifest.json", manifest_bytes), ("commit.json", commit_bytes)):
            descriptor = os.open(stage / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        digest = SnapshotStore(stage / "snapshot.json").list_candidates(limit=1)["snapshot_sha256"]
        _, actual_manifest = _read_json_file(stage / "manifest.json", max_bytes=10000, label="manifest")
        _, actual_commit = _read_json_file(stage / "commit.json", max_bytes=2000, label="commit")
        if digest != manifest["snapshot_sha256"] or actual_manifest != manifest_bytes or actual_commit != commit_bytes:
            raise ValueError("staged bundle verification failed")
        _publish_directory(stage, target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    open_verified_bundle(target)
    return manifest
