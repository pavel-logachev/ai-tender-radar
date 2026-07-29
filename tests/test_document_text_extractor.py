from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
import zipfile
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://test/test")


def install_extractor_dependency_stubs() -> None:
    fitz = types.ModuleType("fitz")
    fitz.open = None
    sys.modules.setdefault("fitz", fitz)

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = None
    sys.modules.setdefault("psycopg", psycopg)

    psycopg_rows = types.ModuleType("psycopg.rows")
    psycopg_rows.dict_row = object()
    sys.modules.setdefault("psycopg.rows", psycopg_rows)

    xlrd = types.ModuleType("xlrd")
    xlrd.open_workbook = None
    sys.modules.setdefault("xlrd", xlrd)

    docx = types.ModuleType("docx")
    docx.Document = None
    sys.modules.setdefault("docx", docx)

    openpyxl = types.ModuleType("openpyxl")
    openpyxl.load_workbook = None
    sys.modules.setdefault("openpyxl", openpyxl)

    app_config = types.ModuleType("app.config")
    app_config.settings = types.SimpleNamespace(database_url="")
    sys.modules.setdefault("app.config", app_config)


try:
    extractor_module = importlib.import_module("app.document_text_extractor")
except ModuleNotFoundError:
    sys.modules.pop("app.document_text_extractor", None)
    install_extractor_dependency_stubs()
    extractor_module = importlib.import_module("app.document_text_extractor")

extract_doc_legacy = extractor_module.extract_doc_legacy
extract_docx = extractor_module.extract_docx
extract_file = extractor_module.extract_file
extract_xlsx = extractor_module.extract_xlsx


class DocumentTextExtractorTest(unittest.TestCase):
    def test_doc_extension_routes_to_legacy_extractor(self) -> None:
        with patch(
            "app.document_text_extractor.extract_doc_legacy",
            return_value="legacy text",
        ) as extractor:
            text = extract_file(Path("spec.DOC"))

        self.assertEqual(text, "legacy text")
        extractor.assert_called_once_with(Path("spec.DOC"))

    def test_legacy_doc_extracts_text_with_antiword(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["antiword"],
            returncode=0,
            stdout="  Tender   terms\n\nPrice table  ",
            stderr="",
        )

        with (
            patch(
                "app.document_text_extractor.shutil.which",
                return_value="/usr/bin/antiword",
            ),
            patch.object(Path, "exists", return_value=True),
            patch(
                "app.document_text_extractor.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            text = extract_doc_legacy(Path("spec.doc"))

        self.assertEqual(text, "Tender terms\nPrice table")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/antiword", "-m", "UTF-8.txt", "spec.doc"],
        )

    def test_legacy_doc_falls_back_to_default_mapping(self) -> None:
        failed = subprocess.CompletedProcess(
            args=["antiword"],
            returncode=1,
            stdout=b"",
            stderr=b"mapping file not found",
        )
        succeeded = subprocess.CompletedProcess(
            args=["antiword"],
            returncode=0,
            stdout=b"Fallback text",
            stderr=b"",
        )

        with (
            patch(
                "app.document_text_extractor.shutil.which",
                return_value="/usr/bin/antiword",
            ),
            patch.object(Path, "exists", return_value=True),
            patch(
                "app.document_text_extractor.subprocess.run",
                side_effect=[failed, succeeded],
            ) as run,
        ):
            text = extract_doc_legacy(Path("spec.doc"))

        self.assertEqual(text, "Fallback text")
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/usr/bin/antiword", "-m", "UTF-8.txt", "spec.doc"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["/usr/bin/antiword", "spec.doc"],
        )

    def test_legacy_doc_missing_file_logs_specific_reason(self) -> None:
        with self.assertLogs("app.document_text_extractor", level="WARNING") as logs:
            text = extract_doc_legacy(Path("missing.doc"))

        self.assertEqual(text, "")
        self.assertIn("file_not_found", "\n".join(logs.output))

    def test_legacy_doc_missing_tool_logs_warning_and_returns_empty(self) -> None:
        with (
            patch("app.document_text_extractor.shutil.which", return_value=None),
            patch.object(Path, "exists", return_value=True),
            self.assertLogs("app.document_text_extractor", level="WARNING") as logs,
        ):
            text = extract_doc_legacy(Path("spec.doc"))

        self.assertEqual(text, "")
        self.assertIn("Legacy DOC extraction failed", "\n".join(logs.output))
        self.assertIn("antiword_not_found", "\n".join(logs.output))

    def test_legacy_doc_tool_failure_logs_warning_and_returns_empty(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["antiword"],
            returncode=1,
            stdout="",
            stderr="not a Word document",
        )

        with (
            patch(
                "app.document_text_extractor.shutil.which",
                return_value="/usr/bin/antiword",
            ),
            patch.object(Path, "exists", return_value=True),
            patch("app.document_text_extractor.subprocess.run", return_value=completed),
            self.assertLogs("app.document_text_extractor", level="WARNING") as logs,
        ):
            text = extract_doc_legacy(Path("broken.doc"))

        self.assertEqual(text, "")
        output = "\n".join(logs.output)
        self.assertIn("Legacy DOC extraction failed", output)
        self.assertIn("antiword_exit_1", output)
        self.assertIn("not a Word document", output)

    def test_docx_xml_fallback_extracts_body_and_table_text(self) -> None:
        document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Tender terms</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Price</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""

        fake_doc = types.SimpleNamespace(paragraphs=[], tables=[], sections=[])

        path = Path(__file__).with_name("_tmp_docx_xml_fallback.docx")
        try:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)

            with patch("app.document_text_extractor.Document", return_value=fake_doc):
                text = extract_docx(path)
        finally:
            path.unlink(missing_ok=True)

        self.assertIn("Tender terms", text)
        self.assertIn("Item", text)
        self.assertIn("Price", text)

    def test_xlsx_serializes_cells_and_falls_back_to_formula_text(self) -> None:
        class FakeCell:
            def __init__(self, coordinate: str, value: object) -> None:
                self.coordinate = coordinate
                self.value = value

        class FakeSheet:
            title = "Price"

            def __init__(self, rows: list[list[FakeCell]]) -> None:
                self._rows = rows

            def iter_rows(self):
                return iter(self._rows)

        class FakeWorkbook:
            def __init__(self, sheets: list[FakeSheet]) -> None:
                self.worksheets = sheets
                self.closed = False

            def close(self) -> None:
                self.closed = True

        calls = []

        def fake_load_workbook(path: Path, *, read_only: bool, data_only: bool):
            calls.append(data_only)
            if data_only:
                return FakeWorkbook([FakeSheet([[FakeCell("A1", None)]])])
            return FakeWorkbook(
                [
                    FakeSheet(
                        [
                            [
                                FakeCell("A1", "=SUM(B1:B2)"),
                                FakeCell("B1", "Cost"),
                            ]
                        ]
                    )
                ]
            )

        with patch(
            "app.document_text_extractor.load_workbook",
            side_effect=fake_load_workbook,
        ):
            text = extract_xlsx(Path("price.xlsx"))

        self.assertEqual(calls, [True, False])
        self.assertIn("Sheet: Price", text)
        self.assertIn("A1: =SUM(B1:B2)", text)
        self.assertIn("B1: Cost", text)


if __name__ == "__main__":
    unittest.main()
