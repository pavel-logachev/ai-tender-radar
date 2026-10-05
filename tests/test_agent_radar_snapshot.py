"""Offline acceptance tests for the bounded snapshot reader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_radar.snapshot import SnapshotStore


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tenders.json"
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": "agent-radar-snapshot-v1",
                    "tenders": [
                        {
                            "id": "z360:101",
                            "title": "Системы хранения для исследовательского центра",
                            "customer_name": "Центр Альфа",
                            "source_url": "https://example.org/101",
                            "documents": [
                                {
                                    "id": "doc-1",
                                    "title": "Техническое задание",
                                    "text": "Требуется масштабируемая система хранения данных.",
                                }
                            ],
                        },
                        {
                            "id": "z360:102",
                            "title": "Комплектующие",
                            "customer_name": "Центр Бета",
                            "documents": [],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_snapshot_rejects_unexpected_metadata_and_bad_ids(self) -> None:
        original = json.loads(self.path.read_text(encoding="utf-8"))
        original["tenders"][0]["raw"] = {"authorization": "do not expose this"}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(ValueError):
            SnapshotStore(self.path).list_candidates()
        original["tenders"][0].pop("raw")
        original["tenders"][0]["documents"][0]["id"] = "../../other-project"
        self.path.write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaises(ValueError):
            SnapshotStore(self.path).list_candidates()

    def test_snapshot_rejects_oversize_and_symlink(self) -> None:
        self.path.write_bytes(b"{" + b"x" * 2_000_001)
        with self.assertRaisesRegex(ValueError, "too large"):
            SnapshotStore(self.path).list_candidates()
        self.path.unlink()
        symlink = self.path.with_name("linked.json")
        try:
            symlink.symlink_to(self.path.with_name("target.json"))
        except OSError:
            self.skipTest("filesystem does not allow symlinks")
        with self.assertRaisesRegex(ValueError, "regular file"):
            SnapshotStore(symlink).list_candidates()

    def test_empty_documents_are_explicitly_unknown_not_a_negative_signal(self) -> None:
        store = SnapshotStore(self.path)
        digest = store.list_candidates()["snapshot_sha256"]
        listed = store.list_documents(tender_id="z360:102", snapshot_sha256=digest)
        self.assertEqual(listed["documents"], [])
        self.assertEqual(listed["document_state"], "unknown")
        modified = json.loads(self.path.read_text(encoding="utf-8"))
        modified["tenders"][1]["document_state"] = "not_retrieved"
        self.path.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")
        new_digest = store.list_candidates()["snapshot_sha256"]
        self.assertEqual(store.get_tender(tender_id="z360:102", snapshot_sha256=new_digest)["document_state"], "not_retrieved")
        modified["tenders"][1]["document_state"] = "available"
        self.path.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            store.list_candidates()

    def test_document_evidence_is_scoped_and_pinned_to_snapshot(self) -> None:
        store = SnapshotStore(self.path)
        snapshot_sha256 = store.list_candidates()["snapshot_sha256"]
        docs = store.list_documents(tender_id="z360:101", snapshot_sha256=snapshot_sha256)
        self.assertEqual([doc["id"] for doc in docs["documents"]], ["doc-1"])
        chunk = store.read_document_chunk(
            tender_id="z360:101", document_id="doc-1", snapshot_sha256=snapshot_sha256, limit=16
        )
        self.assertEqual(chunk["text"], "Требуется масшта")
        self.assertEqual(chunk["next_offset"], 16)
        self.assertEqual(chunk["content_sha256"], docs["documents"][0]["content_sha256"])
        with self.assertRaises(ValueError):
            store.read_document_chunk(tender_id="z360:102", document_id="doc-1", snapshot_sha256=snapshot_sha256)
        with self.assertRaises(ValueError):
            store.read_document_chunk(tender_id="z360:101", document_id="doc-1", snapshot_sha256=snapshot_sha256, limit=4001)
        self.path.write_text(self.path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            store.get_tender(tender_id="z360:101", snapshot_sha256=snapshot_sha256)

    def test_candidate_feed_bounds_pages(self) -> None:
        store = SnapshotStore(self.path)

        first = store.list_candidates(limit=1)
        second = store.list_candidates(limit=1, offset=first["next_offset"])

        self.assertEqual([item["id"] for item in first["candidates"]], ["z360:101"])
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual([item["id"] for item in second["candidates"]], ["z360:102"])
        self.assertIsNone(second["next_offset"])
        with self.assertRaises(ValueError):
            store.list_candidates(limit=101)


if __name__ == "__main__":
    unittest.main()
