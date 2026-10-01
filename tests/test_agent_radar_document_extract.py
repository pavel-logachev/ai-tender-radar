"""Bounded offline document text extraction at the source-worker bytes boundary."""
from __future__ import annotations

import io
import math
import os
import tempfile
import unittest
import zipfile
import stat
from pathlib import Path
from unittest.mock import patch

import fitz
from docx import Document

from agent_radar.document_extract import (
    MAX_DOCUMENT_BYTES, MAX_DOCX_MEMBER_BYTES, MAX_PAGES, MAX_TEXT_CHARS,
    extract_document_text,
)


def docx_bytes(text: str = "Требуется расширение СХД") -> bytes:
    document = Document()
    document.add_paragraph(text)
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Таблица: 24 ТБ"
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def pdf_bytes(text: str = "Storage capacity 24 TB") -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    result = document.tobytes()
    document.close()
    return result


def zip_bytes(parts: dict[str, bytes], *, compress: bool = True) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return stream.getvalue()


class DocumentExtractTest(unittest.TestCase):
    def test_pdf_docx_and_utf8_text_are_extracted_without_input_files(self) -> None:
        for filename, data, expected in (
            ("SPEC.PDF", pdf_bytes(), "Storage capacity 24 TB"),
            ("задание.docx", docx_bytes(), "Требуется расширение СХД"),
            ("readme.txt", "Поставка серверов\n".encode(), "Поставка серверов"),
        ):
            with self.subTest(filename=filename):
                result = extract_document_text(data, filename)
                self.assertEqual(result.status, "extracted", result)
                self.assertIn(expected, result.text)
                if filename.endswith(".docx"):
                    self.assertIn("Таблица: 24 ТБ", result.text)
                self.assertIsNone(result.reason)

    def test_empty_text_is_not_absence_of_document(self) -> None:
        for filename, data in (("blank.pdf", pdf_bytes("")), ("empty.txt", b"  \n ")):
            with self.subTest(filename=filename):
                result = extract_document_text(data, filename)
                self.assertEqual(result.status, "empty")
                self.assertEqual(result.text, "")
                self.assertEqual(result.reason, "no_extractable_text")

    def test_rejects_unsupported_and_mismatched_formats_including_archives(self) -> None:
        for filename, data, status in (
            ("archive.zip", docx_bytes(), "unsupported"),
            ("legacy.doc", b"not a DOC", "unsupported"),
            ("wrapped.pdf", docx_bytes(), "invalid"),
            ("fake.docx", zip_bytes({"evil.txt": b"hello"}), "invalid"),
            ("fake.txt", pdf_bytes(), "invalid"),
            ("no-extension", b"hi", "unsupported"),
            ("corrupt.pdf", b"%PDF-1.7 broken", "invalid"),
        ):
            with self.subTest(filename=filename):
                result = extract_document_text(data, filename)
                self.assertEqual(result.status, status, result)
                self.assertEqual(result.text, "")
                self.assertTrue(result.reason)

    def test_bounded_input_and_utf8_decode_are_explicit_errors(self) -> None:
        self.assertEqual(extract_document_text(b"x" * (MAX_DOCUMENT_BYTES + 1), "file.txt").status, "too_large")
        self.assertEqual(extract_document_text(b"\xff", "file.txt").status, "invalid")
        self.assertEqual(extract_document_text(b"hello\x00world", "file.txt").status, "invalid")
        self.assertEqual(extract_document_text(b"text", "../fake.txt").status, "invalid")

    def test_invalid_deadline_is_rejected(self) -> None:
        for deadline in (0, -1, 31, math.nan, math.inf, True):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                extract_document_text(b"hello", "file.txt", timeout_seconds=deadline)

    def test_docx_rejects_zip_bomb_and_dangerous_members(self) -> None:
        minimal = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>valid</w:t></w:r></w:p></w:body></w:document>"}
        cases = (
            {**minimal, "word/header1.xml": b"X" * (MAX_DOCX_MEMBER_BYTES + 1)},
            {**minimal, "../escape": b"bad"},
            {**minimal, "word/embedded.zip": b"PK\x03\x04"},
        )
        for index, parts in enumerate(cases):
            with self.subTest(parts=list(parts)):
                result = extract_document_text(zip_bytes(parts), "document.docx")
                self.assertEqual(result.status, "too_large" if index == 0 else "invalid", result)
                self.assertEqual(result.text, "")

    def test_docx_rejects_zip_symlinks_and_duplicate_names(self) -> None:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<document/>")
            link = zipfile.ZipInfo("word/link.xml")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, b"/etc/passwd")
        self.assertEqual(extract_document_text(stream.getvalue(), "file.docx").status, "invalid")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<document/>")
            archive.writestr("word/document.xml", b"<document/>")
        self.assertEqual(extract_document_text(stream.getvalue(), "file.docx").status, "invalid")

    def test_docx_rejects_doctype_entities_and_missing_document(self) -> None:
        for contents in (b"<!DOCTYPE foo [<!ENTITY x 'danger'>]><foo>&x;</foo>", b"not XML"):
            result = extract_document_text(zip_bytes({"[Content_Types].xml": b"<Types/>", "word/document.xml": contents}), "file.docx")
            self.assertEqual(result.status, "invalid", result)

    def test_pdf_page_cap_and_text_cap_refuse_partial_evidence(self) -> None:
        document = fitz.open()
        for _ in range(MAX_PAGES + 1):
            document.new_page()
        over_pages = document.tobytes()
        document.close()
        self.assertEqual(extract_document_text(over_pages, "too-many.pdf").status, "too_large")
        self.assertEqual(extract_document_text(b"A" * (MAX_TEXT_CHARS + 1), "huge.txt").status, "too_large")

    def test_timeout_terminates_worker_and_leaves_no_files(self) -> None:
        # PyMuPDF itself is native code; the parent must be able to kill a stuck parser.
        # Test the deadline with a DOCX inflated enough to exceed a tiny process budget.
        with tempfile.TemporaryDirectory() as directory:
            before = set(Path(directory).iterdir())
            with patch.dict(os.environ, {"TMPDIR": directory, "TEMP": directory, "TMP": directory}):
                result = extract_document_text(docx_bytes(), "task.docx", timeout_seconds=0.001)
            self.assertEqual(result.status, "timeout", result)
            self.assertEqual(list(Path(directory).iterdir()), list(before))


if __name__ == "__main__":
    unittest.main()
