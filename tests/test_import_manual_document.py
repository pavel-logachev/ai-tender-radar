from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.pipeline import import_manual_document as importer


class FakeCursor:
    def __init__(self, rows: list[dict | None] | None = None) -> None:
        self.rows = rows or []
        self.queries: list[str] = []
        self.params: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def execute(self, query: str, params: tuple) -> None:
        self.queries.append(query)
        self.params.append(params)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_obj = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def cursor(self, *args, **kwargs) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True


class ManualDocumentImportTest(unittest.TestCase):
    def test_import_manual_document_copies_file_and_records_metadata(self) -> None:
        app_dir = Path("/app")
        target_path = app_dir / "data" / "documents" / "zakupki360_00000013" / "manual_00000013_tz.pdf"
        saved_text: list[tuple[str, str]] = []
        events: list[dict] = []

        with (
            patch.object(importer, "APP_DIR", app_dir),
            patch.object(
                importer,
                "find_tender_by_external_id",
                return_value={"id": "tender-1", "external_id": "00000013"},
            ),
            patch.object(
                importer,
                "copy_manual_document_file",
                return_value=(target_path, "manual_00000013_tz.pdf", "application/pdf", 15),
            ) as copy_file,
            patch.object(importer, "upsert_manual_document_record", return_value="doc-1") as upsert,
            patch.object(importer, "extract_manual_document_text", return_value=("extracted text", None)),
            patch.object(
                importer,
                "save_manual_extracted_text",
                side_effect=lambda document_id, text: saved_text.append((document_id, text)),
            ),
            patch.object(
                importer,
                "record_manual_document_imported_event",
                side_effect=lambda **kwargs: events.append(kwargs),
            ),
        ):
            result = importer.import_manual_document(
                external_id="00000013",
                file_path=Path("/app/data/manual_uploads/00000013_tz.pdf"),
                title="ТЗ, скачано вручную со сторонней площадки",
            )

        self.assertEqual(result.document_id, "doc-1")
        self.assertEqual(result.extracted_chars, len("extracted text"))
        self.assertEqual(saved_text, [("doc-1", "extracted text")])
        self.assertEqual(events[0]["tender_id"], "tender-1")
        self.assertEqual(events[0]["external_id"], "00000013")
        self.assertEqual(events[0]["document_id"], "doc-1")
        self.assertIn("manual_00000013", result.filename)
        self.assertIn("data", result.storage_path)
        copy_file.assert_called_once()
        upsert.assert_called_once()

    def test_upsert_manual_document_record_creates_document_row(self) -> None:
        cursor = FakeCursor(rows=[None, {"id": "doc-1"}])
        connection = FakeConnection(cursor)

        with (
            patch.object(importer, "dict_row_factory", return_value=None),
            patch.object(importer, "connect_db", return_value=connection),
        ):
            document_id = importer.upsert_manual_document_record(
                tender_id="tender-1",
                document_url="manual://document/00000013/tz.pdf",
                filename="manual_00000013_tz.pdf",
                mime_type="application/pdf",
                size_bytes=42,
                storage_path="data/documents/zakupki360_00000013/manual_00000013_tz.pdf",
            )

        self.assertEqual(document_id, "doc-1")
        self.assertTrue(connection.committed)
        self.assertIn("INSERT INTO documents", cursor.queries[1])
        self.assertEqual(cursor.params[1][0], "tender-1")
        self.assertEqual(cursor.params[1][2], "manual://document/00000013/tz.pdf")

    def test_record_manual_document_imported_event_uses_processing_event(self) -> None:
        cursor = FakeCursor()
        connection = FakeConnection(cursor)

        with patch.object(importer, "connect_db", return_value=connection):
            importer.record_manual_document_imported_event(
                tender_id="tender-1",
                external_id="00000013",
                document_id="doc-1",
                title="ТЗ",
                filename="manual_00000013_tz.pdf",
                storage_path="data/documents/zakupki360_00000013/manual_00000013_tz.pdf",
                extracted_chars=123,
                extraction_error=None,
            )

        self.assertTrue(connection.committed)
        self.assertIn("INSERT INTO processing_events", cursor.queries[0])
        self.assertEqual(cursor.params[0][1], importer.MANUAL_IMPORT_EVENT)
        payload = json.loads(cursor.params[0][2])
        self.assertEqual(payload["external_id"], "00000013")
        self.assertEqual(payload["document_id"], "doc-1")
        self.assertEqual(payload["extracted_chars"], 123)


if __name__ == "__main__":
    unittest.main()
