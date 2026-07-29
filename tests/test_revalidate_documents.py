from __future__ import annotations

from pathlib import Path
import unittest
import zipfile

from app.collector.revalidate_documents import (
    classify_document_bytes,
    classify_document_file,
    resolve_document_path,
)


class RevalidateDocumentsTest(unittest.TestCase):
    def test_classifies_html_pseudofile(self) -> None:
        issue = classify_document_bytes(
            "spec.docx",
            b"<!DOCTYPE html><html><body>api</body></html>",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

        self.assertIsNotNone(issue)
        self.assertEqual(issue.problem, "html_response")
        self.assertEqual(issue.detected_type, "html")

    def test_classifies_invalid_docx_like(self) -> None:
        issue = classify_document_bytes(
            "spec.docx",
            b"not a zip",
            "application/octet-stream",
        )

        self.assertIsNotNone(issue)
        self.assertEqual(issue.problem, "invalid_docx_like")

    def test_validates_real_zip_container_for_docx_like_file(self) -> None:
        app_dir = Path.cwd()
        file_path = app_dir / "tests" / "_tmp_spec.docx"
        try:
            with zipfile.ZipFile(file_path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types />")

            _path, issue = classify_document_file(
                {
                    "filename": "spec.docx",
                    "mime_type": "application/octet-stream",
                    "storage_path": "tests/_tmp_spec.docx",
                },
                app_dir=app_dir,
            )
            self.assertIsNone(issue)
        finally:
            file_path.unlink(missing_ok=True)

    def test_detects_broken_zip_container_for_docx_like_file(self) -> None:
        app_dir = Path.cwd()
        file_path = app_dir / "tests" / "_tmp_broken.docx"
        try:
            file_path.write_bytes(b"PKbroken")

            _path, issue = classify_document_file(
                {
                    "filename": "broken.docx",
                    "mime_type": "application/octet-stream",
                    "storage_path": "tests/_tmp_broken.docx",
                },
                app_dir=app_dir,
            )

            self.assertIsNotNone(issue)
            self.assertEqual(issue.problem, "invalid_docx_like")
            self.assertEqual(issue.reason, "zip_container_invalid")
        finally:
            file_path.unlink(missing_ok=True)

    def test_resolve_document_path_rejects_absolute_storage_path(self) -> None:
        file_path, problem = resolve_document_path(
            Path("C:/safe/root"),
            "C:/outside/file.pdf",
        )

        self.assertEqual(problem, "unsafe_storage_path")
        self.assertEqual(file_path, Path("C:/outside/file.pdf"))

    def test_resolve_document_path_rejects_posix_absolute_path_on_windows(self) -> None:
        file_path, problem = resolve_document_path(
            Path("C:/safe/root"),
            "/etc/passwd",
        )

        self.assertEqual(problem, "unsafe_storage_path")
        self.assertEqual(file_path, Path("/etc/passwd"))


if __name__ == "__main__":
    unittest.main()
