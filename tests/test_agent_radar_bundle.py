"""Three-file bundle publication and reader checks, offline."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_radar.bundle import open_verified_bundle
from tests.radar_fixtures import publish


class BundleTest(unittest.TestCase):
    def test_new_directory_exposes_matching_snapshot_and_manifest_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            manifest = publish(target)
            self.assertEqual(set(path.name for path in target.iterdir()), {"snapshot.json", "manifest.json", "commit.json"})
            store, checked = open_verified_bundle(target)
            self.assertEqual(checked, manifest)
            feed = store.list_candidates(limit=10)
            self.assertEqual(feed["snapshot_sha256"], checked["snapshot_sha256"])
            self.assertEqual([c["id"] for c in feed["candidates"]], ["example-source:101", "example-source:102"])
            self.assertEqual(list(Path(directory).glob(".*.staging-*")), [])

    def test_failed_final_rename_never_exposes_pair_and_cleans_owned_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            with patch("tests.radar_fixtures._publish_directory", side_effect=OSError("simulated publish failure")):
                with self.assertRaisesRegex(OSError, "simulated publish failure"):
                    publish(target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_destination_race_does_not_replace_another_operators_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            def concurrent_publish(stage: Path, destination: Path) -> None:
                destination.mkdir()
                from agent_radar.bundle import _publish_directory
                _publish_directory(stage, destination)
            with patch("tests.radar_fixtures._publish_directory", side_effect=concurrent_publish):
                with self.assertRaises((FileExistsError, ValueError)):
                    publish(target)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(Path(directory).glob(".*.staging-*")), [])

    def test_existing_directory_is_not_merged_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            target.mkdir()
            (target / "owned.txt").write_text("another operator", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exists"):
                publish(target)
            self.assertEqual((target / "owned.txt").read_text(encoding="utf-8"), "another operator")
            self.assertEqual(list(Path(directory).glob(".*.staging-*")), [])

    def test_publication_refuses_stage_with_modified_manifest_or_commit(self) -> None:
        from agent_radar.snapshot import SnapshotStore
        for changed in ("manifest.json", "commit.json"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "run-20260929"
                original = SnapshotStore.list_candidates
                def alter_persisted_stage(store: SnapshotStore, *, limit: int = 20, offset: int = 0) -> dict:
                    stage_file = store.path.parent / changed
                    stage_file.write_bytes(stage_file.read_bytes() + b" ")
                    return original(store, limit=limit, offset=offset)
                with patch.object(SnapshotStore, "list_candidates", new=alter_persisted_stage):
                    with self.assertRaisesRegex(ValueError, "verification failed"):
                        publish(target)
                self.assertFalse(target.exists())
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_modified_provenance_or_missing_commit_rejects_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            publish(target)
            meta_path = target / "manifest.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["coverage"]["completed_cursor"] = "changed after publication"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest checksum mismatch"):
                open_verified_bundle(target)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            publish(target)
            (target / "commit.json").unlink()
            with self.assertRaisesRegex(ValueError, "commit"):
                open_verified_bundle(target)

    def test_verified_store_refuses_later_manifest_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            publish(target)
            store, _ = open_verified_bundle(target)
            path = target / "manifest.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["coverage"]["completed_cursor"] = "changed after verification"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest checksum mismatch"):
                store.list_candidates()

    def test_verified_store_refuses_later_snapshot_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            publish(target)
            store, _ = open_verified_bundle(target)
            path = target / "snapshot.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["tenders"][0]["title"] = "changed after verification"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                store.list_candidates()

    def test_manifest_or_snapshot_tampering_rejects_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            publish(target)
            meta_path = target / "manifest.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["snapshot_sha256"] = "0" * 64
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                open_verified_bundle(target)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            publish(target)
            snap = target / "snapshot.json"
            snap.write_bytes(snap.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                open_verified_bundle(target)

    def test_missing_or_staged_directories_are_not_readable_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            with self.assertRaises(ValueError):
                open_verified_bundle(target)
            stage = Path(directory) / ".run-20260929.staging-interrupted"
            stage.mkdir()
            (stage / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                open_verified_bundle(stage)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
