"""Offline source-to-agent boundary: no API access, network calls, or legacy scoring."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_radar.source_export import build_snapshot, materialize_source_export
from agent_radar.snapshot import SnapshotStore
from agent_radar.review_queue import ReviewQueue
from agent_radar.lead import validate_lead

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None


class SourceExportTest(unittest.TestCase):
    def export(self) -> dict:
        return {
            "schema_version": "agent-radar-source-export-v1",
            "source": "example-source",
            "search": {
                "publish_date_from": "2026-09-28", "publish_date_to": "2026-09-29",
                "query": "", "operator_declared_complete": True,
                "results": [
                    {"orderId": 102, "name": "Продление принтеров", "maxPrice": 0},
                    {"orderId": 101, "name": "Расширение СХД", "maxPrice": 100},
                ],
            },
            "details": [
                {"order_id": 101, "order": {
                    "name": "Расширение СХД", "placerOrganizationName": "Учебный центр",
                    "contactPerson": "PRIVATE - must not be in snapshot",
                    "tenderUrl": "https://example.org/101", "documents": [
                        {"id": 9, "title": "ТЗ", "url": "https://example.org/private-file"}
                    ]}, "extracted_documents": [{"document_id": 9, "text": "Требуется увеличить ёмкость СХД."}]},
                {"order_id": 102, "order": {"name": "Продление принтеров", "documents": []}, "extracted_documents": []},
            ],
        }

    def test_all_search_results_become_candidates_without_price_or_scoring_gate(self) -> None:
        snapshot, manifest = build_snapshot(self.export(), max_search_results=10)
        self.assertEqual([row["id"] for row in snapshot["tenders"]], ["example-source:101", "example-source:102"])
        self.assertEqual(snapshot["tenders"][0]["documents"][0]["text"], "Требуется увеличить ёмкость СХД.")
        self.assertEqual(snapshot["tenders"][1]["document_state"], "source_reports_none")
        self.assertEqual(manifest["search_result_count"], 2)
        self.assertNotIn("PRIVATE", json.dumps(snapshot, ensure_ascii=False))
        self.assertNotIn("https://example.org/private-file", json.dumps(snapshot, ensure_ascii=False))

    def test_no_document_order_preserves_only_bounded_source_description_as_quote_evidence(self) -> None:
        source = self.export()
        source["details"][1]["order"].update({
            "deliveryTerm": "ИТ-поддержка на 12 месяцев",
            "deliveryPlace": "Санкт-Петербург",
            "contactPerson": "PRIVATE-CONTACT",
            "contactEMail": "PRIVATE@EXAMPLE.ORG",
        })
        snapshot, _ = build_snapshot(source, max_search_results=10)
        card = snapshot["tenders"][1]
        self.assertEqual(card["description"], "Срок поставки: ИТ-поддержка на 12 месяцев\nМесто поставки: Санкт-Петербург")
        self.assertEqual(card["document_state"], "source_reports_none")
        self.assertNotIn("PRIVATE", json.dumps(snapshot, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            store = SnapshotStore(path)
            digest = store.list_candidates(limit=1)["snapshot_sha256"]
            proposal = {
                "schema_version": "agent-radar-lead-v1", "tender_id": "example-source:102",
                "snapshot_sha256": digest, "decision": "lead",
                "customer_opportunity": "Уточнить потребность заказчика в ИТ-поддержке.",
                "contact_reason": "Обсудить срок оказания поддержки.",
                "suggested_roles": ["ИТ-руководитель"], "questions": ["Какие системы входят в поддержку?"],
                "facts": [{"claim": "В карточке указан срок поддержки.", "evidence": {
                    "document_id": "card", "offset": len(card["title"]) + len(card.get("customer_name", "")) + 2 + len("Срок поставки: "),
                    "quote": "ИТ-поддержка на 12 месяцев"}}],
                "hypotheses": [], "gaps": ["Нужна проверка смысла и актуальности."], "external_sources": [],
            }
            self.assertEqual(validate_lead(proposal, store)["decision"], "lead")

    def test_bad_date_and_invalid_document_reference_stop_publication(self) -> None:
        source = self.export()
        source["search"]["publish_date_from"] = "2026-02-30"
        with self.assertRaises(ValueError):
            build_snapshot(source, max_search_results=10)
        source = self.export()
        source["details"][0]["extracted_documents"][0]["document_id"] = 999
        with self.assertRaisesRegex(ValueError, "unrequested"):
            build_snapshot(source, max_search_results=10)

    def test_incomplete_or_saturated_search_never_publishes_a_partial_snapshot(self) -> None:
        source = self.export()
        source["search"]["operator_declared_complete"] = False
        with self.assertRaisesRegex(ValueError, "incomplete"):
            build_snapshot(source, max_search_results=10)
        source["search"]["operator_declared_complete"] = True
        with self.assertRaisesRegex(ValueError, "saturated"):
            build_snapshot(source, max_search_results=2)
        source["details"].pop()
        with self.assertRaisesRegex(ValueError, "missing detail"):
            build_snapshot(source, max_search_results=10)

    def test_null_document_list_stays_unknown_instead_of_claiming_none(self) -> None:
        source = self.export()
        source["details"][1]["order"]["documents"] = None
        snapshot, _ = build_snapshot(source, max_search_results=10)
        self.assertEqual(snapshot["tenders"][1]["document_state"], "unknown")

    def test_missing_document_text_is_an_explicit_gap_not_source_reports_none(self) -> None:
        source = self.export()
        source["details"][0]["extracted_documents"] = []
        snapshot, manifest = build_snapshot(source, max_search_results=10)
        self.assertEqual(snapshot["tenders"][0]["document_state"], "not_retrieved")
        self.assertEqual(manifest["document_gaps"], ["example-source:101:9"])
        source["details"][0]["order"]["documents"].append({"id": 10, "title": "Спецификация"})
        source["details"][0]["extracted_documents"] = [{"document_id": 9, "text": "СХД."}]
        snapshot, manifest = build_snapshot(source, max_search_results=10)
        self.assertEqual(snapshot["tenders"][0]["document_state"], "retrieved_partial")
        self.assertEqual(manifest["document_gaps"], ["example-source:101:10"])

    def test_missing_snapshot_directory_does_not_leave_orphan_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.json"
            missing_snapshot_parent = Path(directory) / "missing" / "snapshot.json"
            manifest_path = Path(directory) / "manifest.json"
            input_path.write_text(json.dumps(self.export(), ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(OSError):
                materialize_source_export(input_path, missing_snapshot_parent, manifest_path, max_search_results=10)
            self.assertFalse(manifest_path.exists())

    def test_snapshot_rename_failure_does_not_leave_orphan_manifest(self) -> None:
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.json"
            snapshot_path = Path(directory) / "snapshot.json"
            manifest_path = Path(directory) / "manifest.json"
            input_path.write_text(json.dumps(self.export(), ensure_ascii=False), encoding="utf-8")
            with patch.object(Path, "rename", side_effect=OSError("simulated rename failure")):
                with self.assertRaisesRegex(OSError, "simulated rename failure"):
                    materialize_source_export(input_path, snapshot_path, manifest_path, max_search_results=10)
            self.assertFalse(snapshot_path.exists())
            self.assertFalse(manifest_path.exists())

    def test_manifest_write_failure_leaves_no_partial_published_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.json"
            output_path = Path(directory) / "snapshot.json"
            missing_parent = Path(directory) / "nonexistent" / "manifest.json"
            input_path.write_text(json.dumps(self.export(), ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(OSError):
                materialize_source_export(input_path, output_path, missing_parent, max_search_results=10)
            self.assertFalse(output_path.exists())

    @unittest.skipIf(ClientSession is None, "MCP SDK required for real end-to-end check")
    def test_export_to_mcp_evidence_to_review_queue_without_model(self) -> None:
        async def research_with_real_stdio(snapshot_path: Path) -> dict:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "agent_radar.mcp_server", "--snapshot", str(snapshot_path), "--allow-unverified-snapshot"],
                env={**os.environ, "AGENT_RADAR_ENABLE_LOCAL_SNAPSHOT": "1"},
            )
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    page = json.loads((await session.call_tool("list_candidates", {"limit": 10})).content[0].text)
                    self.assertEqual([c["id"] for c in page["candidates"]], ["example-source:101", "example-source:102"])
                    digest = page["snapshot_sha256"]
                    docs = json.loads((await session.call_tool("list_documents", {
                        "tender_id": "example-source:101", "snapshot_sha256": digest
                    })).content[0].text)
                    doc_id = docs["documents"][0]["id"]
                    evidence = json.loads((await session.call_tool("read_document_chunk", {
                        "tender_id": "example-source:101", "document_id": doc_id,
                        "snapshot_sha256": digest, "limit": 90
                    })).content[0].text)
                    self.assertTrue(evidence["text"].startswith("Требуется увеличить"))
                    return {"tender_id": "example-source:101", "snapshot_sha256": digest,
                            "doc_id": doc_id, "quote": evidence["text"]}

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.json"
            snapshot_path = Path(directory) / "snapshot.json"
            manifest_path = Path(directory) / "manifest.json"
            input_path.write_text(json.dumps(self.export(), ensure_ascii=False), encoding="utf-8")
            materialize_source_export(input_path, snapshot_path, manifest_path, max_search_results=10)
            evidence = asyncio.run(research_with_real_stdio(snapshot_path))
            proposal = {
                "schema_version": "agent-radar-lead-v1", "tender_id": evidence["tender_id"],
                "snapshot_sha256": evidence["snapshot_sha256"], "decision": "lead",
                "customer_opportunity": "Обсудить развитие хранения с Учебным центром.",
                "contact_reason": "Уточнить инфраструктурные планы.",
                "suggested_roles": ["ИТ-руководитель"], "questions": ["Какие планы по СХД?"],
                "facts": [{"claim": "Локальный экспорт содержит запрос увеличения СХД.", "evidence": {
                    "document_id": evidence["doc_id"], "offset": 0, "quote": evidence["quote"]
                }}], "hypotheses": [], "gaps": ["Подлинность и контекст требуют ручной проверки."],
                "external_sources": [],
            }
            queue = ReviewQueue(Path(directory) / "review.sqlite3")
            receipt = queue.submit(proposal, SnapshotStore(snapshot_path))
            self.assertEqual(receipt["status"], "pending_review")
            self.assertEqual(len(queue.list_pending()), 1)

    def test_existing_manifest_is_not_deleted_on_exclusive_create_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.json"
            snapshot_path = Path(directory) / "snapshot.json"
            manifest_path = Path(directory) / "manifest.json"
            input_path.write_text(json.dumps(self.export(), ensure_ascii=False), encoding="utf-8")
            manifest_path.write_text("owned by another operator", encoding="utf-8")
            with self.assertRaises(ValueError):
                materialize_source_export(input_path, snapshot_path, manifest_path, max_search_results=10)
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), "owned by another operator")
            self.assertFalse(snapshot_path.exists())

    def test_validated_snapshot_and_sidecar_are_published_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "source.json"
            output_path = Path(directory) / "snapshot.json"
            manifest_path = Path(directory) / "manifest.json"
            input_path.write_text(json.dumps(self.export(), ensure_ascii=False), encoding="utf-8")
            result = materialize_source_export(input_path, output_path, manifest_path, max_search_results=10)
            store = SnapshotStore(output_path)
            self.assertEqual(len(store.list_candidates()["candidates"]), 2)
            self.assertEqual(result["snapshot_sha256"], store.list_candidates()["snapshot_sha256"])
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["snapshot_sha256"], result["snapshot_sha256"])
            with self.assertRaisesRegex(ValueError, "already exists"):
                materialize_source_export(input_path, output_path, manifest_path, max_search_results=10)


if __name__ == "__main__":
    unittest.main()
