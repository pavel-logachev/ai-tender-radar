"""Offline acceptance tests for the separate tender-radar agent boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_radar.snapshot import SnapshotStore
from agent_radar.lead import validate_lead
from agent_radar.review_queue import ReviewQueue
from agent_radar.mcp_server import build_server


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
                            "legacy_score": "no_go",
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

    def test_candidate_feed_includes_legacy_no_go_and_bounds_pages(self) -> None:
        store = SnapshotStore(self.path)

        first = store.list_candidates(limit=1)
        second = store.list_candidates(limit=1, offset=first["next_offset"])

        self.assertEqual([item["id"] for item in first["candidates"]], ["z360:101"])
        self.assertEqual(first["candidates"][0]["legacy_score"], "no_go")
        self.assertEqual(first["next_offset"], 1)
        self.assertEqual([item["id"] for item in second["candidates"]], ["z360:102"])
        self.assertIsNone(second["next_offset"])
        with self.assertRaises(ValueError):
            store.list_candidates(limit=101)


class MCPBoundaryTest(unittest.TestCase):
    def test_read_only_server_exports_only_scoped_snapshot_tools(self) -> None:
        class FakeMCP:
            def __init__(self, name: str):
                self.name = name
                self.functions = {}

            def tool(self, *, annotations=None):
                self.annotations = annotations
                def decorate(function):
                    self.functions[function.__name__] = function
                    return function
                return decorate

        with patch("agent_radar.mcp_server.Server", FakeMCP):
            server = build_server(Path("agent_radar/fixtures/synthetic_tenders.json"))
        self.assertEqual(set(server.functions), {
            "list_candidates", "get_tender", "list_documents", "read_document_chunk"
        })
        page = server.functions["list_candidates"](limit=2)
        self.assertEqual(len(page["candidates"]), 2)
        self.assertEqual(page["candidates"][0]["legacy_score"], "no_go")


class LeadValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tenders.json"
        self.path.write_text(
            json.dumps({"schema_version": "agent-radar-snapshot-v1", "tenders": [
                {"id": "z360:101", "title": "СХД", "customer_name": "Центр Альфа",
                 "description": "Расширение хранилища", "documents": [
                     {"id": "doc-1", "title": "ТЗ", "text": "Необходимо расширить существующую СХД."}
                 ]}
            ]}, ensure_ascii=False), encoding="utf-8"
        )
        self.store = SnapshotStore(self.path)
        self.snapshot_sha256 = self.store.list_candidates()["snapshot_sha256"]

    def example(self) -> dict:
        return {
            "schema_version": "agent-radar-lead-v1",
            "tender_id": "z360:101",
            "snapshot_sha256": self.snapshot_sha256,
            "decision": "lead",
            "customer_opportunity": "Обсудить развитие хранения данных у Центра Альфа.",
            "contact_reason": "Проверить, планируется ли расширение инфраструктуры хранения.",
            "suggested_roles": ["ИТ-руководитель"],
            "questions": ["Каковы планы по развитию СХД?"],
            "facts": [{"claim": "Требуется расширить СХД.", "evidence": {
                "document_id": "doc-1", "offset": 0, "quote": "Необходимо расширить существующую СХД."
            }}],
            "hypotheses": ["Может понадобиться дополнительная инфраструктура."],
            "gaps": ["Неизвестен ответственный за инфраструктуру."],
            "external_sources": [],
        }

    def test_accepts_verified_document_quote(self) -> None:
        result = validate_lead(self.example(), self.store)
        self.assertEqual(result["decision"], "lead")
        self.assertEqual(result["facts"][0]["evidence"]["document_id"], "doc-1")

    def test_document_card_quote_and_tender_binding(self) -> None:
        card_fact = self.example()
        card_fact["facts"] = [{"claim": "Название закупки — СХД.", "evidence": {
            "document_id": "card", "offset": 0, "quote": "СХД"
        }}]
        with self.assertRaisesRegex(ValueError, "document evidence"):
            validate_lead(card_fact, self.store)
        wrong = self.example()
        wrong["tender_id"] = "another-tender"
        with self.assertRaises(ValueError):
            validate_lead(wrong, self.store)

    def test_documentless_lead_requires_substantive_description_quote(self) -> None:
        source = json.loads(self.path.read_text(encoding="utf-8"))
        source["tenders"][0]["documents"] = []
        source["tenders"][0]["document_state"] = "not_retrieved"
        self.path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        store = SnapshotStore(self.path)
        hash_value = store.list_candidates()["snapshot_sha256"]
        proposal = self.example()
        proposal["snapshot_sha256"] = hash_value
        proposal["facts"] = [{"claim": "Указано СХД", "evidence": {
            "document_id": "card", "offset": 0, "quote": "СХД"}}]
        with self.assertRaisesRegex(ValueError, "description evidence"):
            validate_lead(proposal, store)
        proposal["facts"] = [{"claim": "Указано расширение", "evidence": {
            "document_id": "card", "offset": len("СХД\nЦентр Альфа\n"), "quote": "Расширение хранилища"}}]
        self.assertEqual(validate_lead(proposal, store)["decision"], "lead")

    def test_review_can_record_uncertainty_without_document_or_description(self) -> None:
        source = json.loads(self.path.read_text(encoding="utf-8"))
        source["tenders"][0].pop("description")
        source["tenders"][0]["documents"] = []
        source["tenders"][0]["document_state"] = "unknown"
        self.path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        store = SnapshotStore(self.path)
        proposal = self.example()
        proposal["snapshot_sha256"] = store.list_candidates()["snapshot_sha256"]
        proposal["facts"] = []
        for decision in ("review", "pass"):
            proposal["decision"] = decision
            self.assertEqual(validate_lead(proposal, store)["decision"], decision)
        proposal["decision"] = "lead"
        proposal["facts"] = [{"claim": "Название СХД", "evidence": {
            "document_id": "card", "offset": 0, "quote": "СХД"}}]
        with self.assertRaisesRegex(ValueError, "description evidence"):
            validate_lead(proposal, store)

    def test_card_quote_cannot_span_artificial_field_boundary(self) -> None:
        invalid = self.example()
        invalid["facts"] = [{"claim": "Текст пересекает два поля.", "evidence": {
            "document_id": "card", "offset": 0, "quote": "СХД\nЦентр"
        }}]
        with self.assertRaisesRegex(ValueError, "single field"):
            validate_lead(invalid, self.store)

    def test_pending_is_read_only_and_does_not_create_missing_queue(self) -> None:
        queue_path = Path(self.temp.name) / "missing.sqlite3"
        queue = ReviewQueue(queue_path)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            queue.list_pending()
        self.assertFalse(queue_path.exists())

    def test_review_queue_records_once_and_never_sends(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        payload = self.example()
        first = queue.submit(payload, self.store)
        second = queue.submit(payload, self.store)
        self.assertEqual(first["status"], "pending_review")
        self.assertEqual(first["review_id"], second["review_id"])
        self.assertEqual(len(queue.list_pending()), 1)
        changed = self.example()
        changed["contact_reason"] = "Изменённое предложение."
        with self.assertRaisesRegex(ValueError, "different suggestion"):
            queue.submit(changed, self.store)

    def test_approved_feed_excludes_pending_rejected_and_non_leads(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        lead = queue.submit(self.example(), self.store)
        self.assertEqual(queue.list_approved(limit=10, snapshot_sha256=self.snapshot_sha256), [])
        queue.record_decision(review_id=lead["review_id"], verdict="approved", reviewer="manual:tester",
                              reason="Проверен повод", decided_at="2026-09-29T12:00:00Z")
        self.assertEqual([item["review_id"] for item in queue.list_approved(limit=10, snapshot_sha256=self.snapshot_sha256,
                                                                              reviewer_prefix=None)], [lead["review_id"]])
        self.assertEqual(queue.list_approved(limit=10, snapshot_sha256=self.snapshot_sha256, reviewer_prefix=None)[0]["decision"]["verdict"], "approved")
        self.assertEqual(queue.list_approved(limit=10, snapshot_sha256="0" * 64), [])
        with self.assertRaises(ValueError):
            queue.list_approved(limit=0, snapshot_sha256=self.snapshot_sha256)

    def test_approved_feed_filters_non_leads_before_limit_and_sorts_actual_utc_time(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        # Distinct tender IDs in one snapshot exercise the top-five filter.
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        base = rows["tenders"][0]
        rows["tenders"] = [{**base, "id": f"z360:{n}"} for n in range(101, 108)]
        self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        store = SnapshotStore(self.path)
        snapshot_hash = store.list_candidates()["snapshot_sha256"]
        for index, kind in enumerate(("lead", "review", "review", "review", "review", "review", "lead")):
            proposal = self.example()
            proposal["tender_id"] = f"z360:{101 + index}"
            proposal["snapshot_sha256"] = snapshot_hash
            proposal["decision"] = kind
            if kind != "lead":
                proposal["facts"] = []
            receipt = queue.submit(proposal, store)
            time = "2026-09-29T12:00:00Z" if index == 0 else "2026-09-29T12:00:00.100000Z" if index == 6 else f"2026-09-29T12:00:0{index}Z"
            queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester", reason="offline",
                                  decided_at=time)
        approved = queue.list_approved(limit=1, snapshot_sha256=snapshot_hash, reviewer_prefix=None)
        self.assertEqual(approved[0]["suggestion"]["tender_id"], "z360:107")
        all_leads = queue.list_approved(limit=5, snapshot_sha256=snapshot_hash, reviewer_prefix=None)
        self.assertEqual({item["suggestion"]["tender_id"] for item in all_leads}, {"z360:101", "z360:107"})

    def test_pending_feed_filters_old_snapshot_and_non_leads_before_limit(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        base = rows["tenders"][0]
        rows["tenders"] = [{**base, "id": f"z360:{n}"} for n in range(101, 108)]
        self.path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        store = SnapshotStore(self.path)
        snapshot_hash = store.list_candidates()["snapshot_sha256"]
        for index, decision in enumerate(["review"] * 6 + ["lead"]):
            proposal = self.example()
            proposal["tender_id"] = f"z360:{101 + index}"
            proposal["snapshot_sha256"] = snapshot_hash
            proposal["decision"] = decision
            if decision != "lead":
                proposal["facts"] = []
            queue.submit(proposal, store)
        current = queue.list_pending(limit=1, snapshot_sha256=snapshot_hash, leads_only=True)
        self.assertEqual([row["suggestion"]["tender_id"] for row in current], ["z360:107"])
        self.assertEqual(queue.list_pending(limit=5, snapshot_sha256="0" * 64, leads_only=True), [])

    def test_manual_cli_cannot_claim_telegram_review_identity(self) -> None:
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch
        from agent_radar.cli import main
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        receipt = queue.submit(self.example(), self.store)
        argv = ["agent_radar.cli", "review", "--queue", str(queue.database_path), "--id", receipt["review_id"],
                "--verdict", "approved", "--reviewer", "telegram:user:200", "--reason", "pretend",
                "--decided-at", "2026-09-29T12:00:00Z"]
        with patch("sys.argv", argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main()
        self.assertEqual(queue.get_review(receipt["review_id"])["status"], "pending_review")

    def test_public_approved_feed_requires_current_reviewer_allowlist(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        receipt = queue.submit(self.example(), self.store)
        queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="telegram:user:200",
                              reason="operator checked", decided_at="2026-09-29T12:00:00Z")
        self.assertEqual(queue.list_approved(snapshot_sha256=self.snapshot_sha256, reviewer_user_ids=set()), [])
        self.assertEqual(queue.list_approved(snapshot_sha256=self.snapshot_sha256, reviewer_user_ids={201}), [])
        self.assertEqual([row["review_id"] for row in queue.list_approved(snapshot_sha256=self.snapshot_sha256,
                                                                            reviewer_user_ids={200})], [receipt["review_id"]])

    def test_public_approved_feed_ignores_unverified_cli_decisions(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        receipt = queue.submit(self.example(), self.store)
        queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester",
                              reason="operator claim", decided_at="2026-09-29T12:00:00Z")
        self.assertEqual(queue.list_approved(snapshot_sha256=self.snapshot_sha256), [])
        self.assertEqual([r["review_id"] for r in queue.list_approved(snapshot_sha256=self.snapshot_sha256,
                                                                       reviewer_prefix=None)], [receipt["review_id"]])

    def test_approved_feed_rejects_changed_copy_after_manual_decision(self) -> None:
        import sqlite3
        from contextlib import closing
        path = Path(self.temp.name) / "reviews.sqlite3"
        queue = ReviewQueue(path)
        receipt = queue.submit(self.example(), self.store)
        queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester",
                              reason="offline", decided_at="2026-09-29T12:00:00Z")
        with closing(sqlite3.connect(path)) as connection, connection:
            payload = json.loads(connection.execute("SELECT payload FROM suggestions WHERE review_id = ?", (receipt["review_id"],)).fetchone()[0])
            payload["contact_reason"] = "Подменённый после review текст без изменения цитаты."
            connection.execute("UPDATE suggestions SET payload = ? WHERE review_id = ?",
                               (json.dumps(payload, ensure_ascii=False), receipt["review_id"]))
        with self.assertRaisesRegex(ValueError, "payload checksum mismatch"):
            queue.list_approved(snapshot_sha256=self.snapshot_sha256, reviewer_prefix=None)

    def test_human_review_decision_is_explicit_audited_and_idempotent(self) -> None:
        queue = ReviewQueue(Path(self.temp.name) / "reviews.sqlite3")
        receipt = queue.submit(self.example(), self.store)
        reviewed = queue.record_decision(
            review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester",
            reason="Сверены текст и задача клиента.", decided_at="2026-09-29T12:00:00Z",
        )
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(queue.list_pending(), [])
        self.assertEqual(queue.get_review(receipt["review_id"])["decision"], {
            "verdict": "approved", "reviewer": "manual:tester",
            "reason": "Сверены текст и задача клиента.", "decided_at": "2026-09-29T12:00:00Z",
        })
        self.assertEqual(queue.record_decision(
            review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester",
            reason="Сверены текст и задача клиента.", decided_at="2026-09-29T12:00:00Z",
        )["status"], "approved")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            queue.record_decision(
                review_id=receipt["review_id"], verdict="rejected", reviewer="manual:tester",
                reason="Передумал", decided_at="2026-09-29T12:05:00Z",
            )
        self.assertEqual(queue.submit(self.example(), self.store)["status"], "approved")

    def test_human_review_rejects_bad_identity_and_does_not_create_missing_queue(self) -> None:
        queue_path = Path(self.temp.name) / "reviews.sqlite3"
        queue = ReviewQueue(queue_path)
        with self.assertRaises(ValueError):
            queue.record_decision(review_id="0" * 64, verdict="approved", reviewer="manual:tester", reason="ok", decided_at="2026-09-29T12:00:00Z")
        self.assertFalse(queue_path.exists())
        receipt = queue.submit(self.example(), self.store)
        with self.assertRaises(ValueError):
            queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="", reason="ok", decided_at="2026-09-29T12:00:00Z")
        with self.assertRaises(ValueError):
            queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester", reason="ok", decided_at="yesterday")
        with self.assertRaises(ValueError):
            queue.record_decision(review_id=receipt["review_id"], verdict="approved", reviewer="manual:tester", reason="ok", decided_at="2026-09-29T12:00:00")
        self.assertEqual(len(queue.list_pending()), 1)

    def test_rejects_nonverbatim_quote_even_when_trimmed_text_matches(self) -> None:
        invalid = self.example()
        invalid["facts"][0]["evidence"]["quote"] = "  Необходимо расширить существующую СХД.  "
        with self.assertRaisesRegex(ValueError, "quote"):
            validate_lead(invalid, self.store)

    def test_offline_shadow_rejects_unverified_external_research(self) -> None:
        invalid = self.example()
        invalid["external_sources"] = [{"url": "https://example.org", "title": "Unverified", "accessed_at": "now", "claim": "claim"}]
        with self.assertRaisesRegex(ValueError, "external research"):
            validate_lead(invalid, self.store)

    def test_rejects_invented_evidence_and_empty_lead(self) -> None:
        invalid = self.example()
        invalid["facts"][0]["evidence"]["quote"] = "Поставить серверы."
        with self.assertRaisesRegex(ValueError, "quote"):
            validate_lead(invalid, self.store)
        invalid = self.example()
        invalid["customer_opportunity"] = ""
        with self.assertRaises(ValueError):
            validate_lead(invalid, self.store)


if __name__ == "__main__":
    unittest.main()
