"""Offline, operator-supplied three-file bundle publication and reader checks."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_radar.bundle import materialize_bundle, open_verified_bundle


FIXTURE = Path(__file__).resolve().parents[1] / "agent_radar" / "fixtures" / "synthetic_source_export.json"


class BundleTest(unittest.TestCase):
    def test_new_directory_exposes_matching_snapshot_and_manifest_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            manifest = materialize_bundle(FIXTURE, target, max_search_results=10)
            self.assertEqual(set(path.name for path in target.iterdir()), {"snapshot.json", "manifest.json", "commit.json"})
            store, checked = open_verified_bundle(target)
            self.assertEqual(checked, manifest)
            feed = store.list_candidates(limit=10)
            self.assertEqual(feed["snapshot_sha256"], checked["snapshot_sha256"])
            self.assertEqual([c["id"] for c in feed["candidates"]], ["example-source:101", "example-source:102"])
            self.assertEqual(list(Path(directory).glob(".*.staging-*")), [])

    def test_cli_materializes_verified_bundle_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            result = subprocess.run([
                sys.executable, "-m", "agent_radar.cli", "materialize-bundle",
                "--source-export", str(FIXTURE), "--bundle", str(target),
                "--max-search-results", "10",
            ], cwd=FIXTURE.parents[2], capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            manifest = json.loads(result.stdout)
            self.assertEqual(open_verified_bundle(target)[1]["snapshot_sha256"], manifest["snapshot_sha256"])
            verified = subprocess.run([
                sys.executable, "-m", "agent_radar.cli", "verify-bundle", "--bundle", str(target),
            ], cwd=FIXTURE.parents[2], capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(verified.returncode, 0, msg=verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["snapshot_sha256"], manifest["snapshot_sha256"])

    def test_cli_preview_and_submit_require_verified_bundle_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "run-20260929"
            manifest = materialize_bundle(FIXTURE, target, max_search_results=10)
            preview = subprocess.run([
                sys.executable, "-m", "agent_radar.cli", "preview", "--bundle", str(target), "--limit", "10",
            ], cwd=FIXTURE.parents[2], capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(preview.returncode, 0, msg=preview.stderr)
            self.assertEqual(json.loads(preview.stdout)["snapshot_sha256"], manifest["snapshot_sha256"])
            proposal = {
                "schema_version": "agent-radar-lead-v1", "tender_id": "example-source:101",
                "snapshot_sha256": manifest["snapshot_sha256"], "decision": "lead",
                "customer_opportunity": "Обсудить развитие хранения.",
                "contact_reason": "Уточнить планы инфраструктуры.",
                "suggested_roles": ["ИТ-руководитель"], "questions": ["Каковы планы хранения?"],
                "facts": [{"claim": "Упомянуто увеличение ёмкости.", "evidence": {
                    "document_id": "z360:201", "offset": 0, "quote": "Заказчику требуется увеличение ёмкости",
                }}], "hypotheses": [], "gaps": [], "external_sources": [],
            }
            proposal_path = root / "proposal.json"
            proposal_path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
            queue_path = root / "reviews.sqlite3"
            submission = subprocess.run([
                sys.executable, "-m", "agent_radar.cli", "submit", "--bundle", str(target),
                "--proposal", str(proposal_path), "--queue", str(queue_path),
            ], cwd=FIXTURE.parents[2], capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(submission.returncode, 0, msg=submission.stderr)
            self.assertEqual(json.loads(submission.stdout)["status"], "pending_review")
            self.assertTrue(queue_path.is_file())

    def test_cli_rejects_corrupt_bundle_before_preview_or_queue_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
            manifest_path = target / "manifest.json"
            meta = json.loads(manifest_path.read_text(encoding="utf-8"))
            meta["search"]["query"] = "modified"
            manifest_path.write_text(json.dumps(meta), encoding="utf-8")
            proposal_path = root / "proposal.json"
            proposal_path.write_text("{}", encoding="utf-8")
            queue_path = root / "reviews.sqlite3"
            for args in (["preview", "--bundle", str(target)], ["submit", "--bundle", str(target),
                         "--proposal", str(proposal_path), "--queue", str(queue_path)]):
                result = subprocess.run([sys.executable, "-m", "agent_radar.cli", *args],
                                        cwd=FIXTURE.parents[2], capture_output=True, text=True,
                                        timeout=15, check=False)
                self.assertNotEqual(result.returncode, 0, msg=args)
                self.assertIn("manifest checksum mismatch", result.stderr)
            self.assertFalse(queue_path.exists())

    def test_raw_cli_read_requires_explicit_compatibility_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
            command = [sys.executable, "-m", "agent_radar.cli", "preview", "--snapshot", str(target / "snapshot.json")]
            refused = subprocess.run(command, cwd=FIXTURE.parents[2], capture_output=True, text=True,
                                     timeout=15, check=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("unverified", refused.stderr)
            allowed = subprocess.run([*command, "--allow-unverified-snapshot"], cwd=FIXTURE.parents[2],
                                     capture_output=True, text=True, timeout=15, check=False)
            self.assertEqual(allowed.returncode, 0, msg=allowed.stderr)
            self.assertEqual(len(json.loads(allowed.stdout)["candidates"]), 2)

    def test_failed_final_rename_never_exposes_pair_and_cleans_owned_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            with patch("agent_radar.bundle._publish_directory", side_effect=OSError("simulated publish failure")):
                with self.assertRaisesRegex(OSError, "simulated publish failure"):
                    materialize_bundle(FIXTURE, target, max_search_results=10)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_destination_race_does_not_replace_another_operators_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            def concurrent_publish(stage: Path, destination: Path) -> None:
                destination.mkdir()
                from agent_radar.bundle import _publish_directory
                _publish_directory(stage, destination)
            with patch("agent_radar.bundle._publish_directory", side_effect=concurrent_publish):
                with self.assertRaises((FileExistsError, ValueError)):
                    materialize_bundle(FIXTURE, target, max_search_results=10)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual(list(Path(directory).glob(".*.staging-*")), [])

    def test_existing_directory_is_not_merged_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            target.mkdir()
            (target / "owned.txt").write_text("another operator", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exists"):
                materialize_bundle(FIXTURE, target, max_search_results=10)
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
                    with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                        materialize_bundle(FIXTURE, target, max_search_results=10)
                self.assertFalse(target.exists())
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_modified_provenance_or_missing_commit_rejects_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
            meta_path = target / "manifest.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["search"]["query"] = "changed after publication"
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest checksum mismatch"):
                open_verified_bundle(target)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
            (target / "commit.json").unlink()
            with self.assertRaisesRegex(ValueError, "commit"):
                open_verified_bundle(target)

    def test_verified_store_refuses_later_manifest_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
            store, _ = open_verified_bundle(target)
            path = target / "manifest.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["search"]["query"] = "changed after verification"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest checksum mismatch"):
                store.list_candidates()

    def test_verified_store_refuses_later_snapshot_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
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
            materialize_bundle(FIXTURE, target, max_search_results=10)
            meta_path = target / "manifest.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["snapshot_sha256"] = "0" * 64
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                open_verified_bundle(target)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "run-20260929"
            materialize_bundle(FIXTURE, target, max_search_results=10)
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
