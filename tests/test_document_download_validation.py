from __future__ import annotations

import unittest

from app.collector.download_validation import (
    detect_downloaded_file_type,
    extension_for_detected_file_type,
    validate_downloaded_document,
)


class DownloadedDocumentValidationTest(unittest.TestCase):
    def test_detects_pdf_signature(self) -> None:
        self.assertEqual(detect_downloaded_file_type(b"%PDF-1.7\nbody"), "pdf")

    def test_detects_zip_based_office_signature(self) -> None:
        self.assertEqual(detect_downloaded_file_type(b"PK\x03\x04body"), "zip")

    def test_detects_legacy_office_signature(self) -> None:
        content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1body"
        self.assertEqual(detect_downloaded_file_type(content), "ole")

    def test_detects_rar4_and_rar5_signatures(self) -> None:
        self.assertEqual(detect_downloaded_file_type(b"Rar!\x1a\x07\x00body"), "rar")
        self.assertEqual(detect_downloaded_file_type(b"Rar!\x1a\x07\x01\x00body"), "rar")

    def test_detects_7z_signature(self) -> None:
        self.assertEqual(detect_downloaded_file_type(b"7z\xbc\xaf\x27\x1cbody"), "7z")

    def test_archive_extensions_for_detected_types(self) -> None:
        self.assertEqual(extension_for_detected_file_type("rar"), ".rar")
        self.assertEqual(extension_for_detected_file_type("7z"), ".7z")

    def test_rejects_html_response_with_pdf_filename(self) -> None:
        is_valid, detected_type, reason = validate_downloaded_document(
            b"  <!DOCTYPE html><html><body>api page</body></html>",
            "notice.pdf",
            "application/pdf",
        )

        self.assertFalse(is_valid)
        self.assertEqual(detected_type, "html")
        self.assertEqual(reason, "html_response")

    def test_rejects_signature_mismatch_for_docx(self) -> None:
        is_valid, detected_type, reason = validate_downloaded_document(
            b"%PDF-1.7\nbody",
            "spec.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        self.assertFalse(is_valid)
        self.assertEqual(detected_type, "pdf")
        self.assertEqual(reason, "signature_mismatch_expected_zip_got_pdf")

    def test_accepts_real_docx_zip_signature(self) -> None:
        for filename in ("spec.docx", "price.xlsx", "archive.zip"):
            with self.subTest(filename=filename):
                is_valid, detected_type, reason = validate_downloaded_document(
                    b"PK\x03\x04body",
                    filename,
                    "text/html",
                )

                self.assertTrue(is_valid)
                self.assertEqual(detected_type, "zip")
                self.assertEqual(reason, "ok")

    def test_accepts_valid_rar_and_7z_archives(self) -> None:
        cases = [
            (b"Rar!\x1a\x07\x00body", "docs.rar", "rar"),
            (b"Rar!\x1a\x07\x01\x00body", "docs.rar", "rar"),
            (b"7z\xbc\xaf\x27\x1cbody", "docs.7z", "7z"),
        ]

        for content, filename, expected_type in cases:
            with self.subTest(filename=filename, expected_type=expected_type):
                is_valid, detected_type, reason = validate_downloaded_document(
                    content,
                    filename,
                    "application/octet-stream",
                )

                self.assertTrue(is_valid)
                self.assertEqual(detected_type, expected_type)
                self.assertEqual(reason, "ok")


if __name__ == "__main__":
    unittest.main()
