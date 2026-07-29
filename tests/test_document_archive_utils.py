from __future__ import annotations

from pathlib import Path
import unittest

from app.document_archive_utils import (
    ArchiveMemberCandidate,
    is_safe_child_path,
    safe_archive_member_path,
    select_safe_archive_members,
)


class DocumentArchiveUtilsTest(unittest.TestCase):
    def test_safe_archive_member_path_rejects_traversal(self) -> None:
        unsafe_names = [
            "../evil.docx",
            "nested/../../evil.pdf",
            "/absolute/spec.xlsx",
            "C:/absolute/spec.xlsx",
            "docs/C:/absolute/spec.xlsx",
            r"..\evil.docx",
        ]

        for name in unsafe_names:
            with self.subTest(name=name):
                self.assertIsNone(safe_archive_member_path(name))

    def test_safe_archive_member_path_normalizes_nested_file(self) -> None:
        self.assertEqual(
            safe_archive_member_path(r"docs\spec.docx"),
            Path("docs") / "spec.docx",
        )

    def test_select_safe_archive_members_filters_unsafe_and_limits(self) -> None:
        members = select_safe_archive_members(
            [
                ArchiveMemberCandidate("docs/spec.docx", size=4),
                ArchiveMemberCandidate("../evil.pdf", size=1),
                ArchiveMemberCandidate("bin/tool.exe", size=1),
                ArchiveMemberCandidate("docs/price.xlsx", size=4),
                ArchiveMemberCandidate("docs/too-large.pdf", size=8),
            ],
            max_files=3,
            max_total_size=10,
        )

        self.assertEqual(
            [str(member.safe_path) for member in members],
            [str(Path("docs") / "spec.docx"), str(Path("docs") / "price.xlsx")],
        )

    def test_is_safe_child_path_rejects_path_escape(self) -> None:
        root = Path("C:/safe/root")
        self.assertTrue(is_safe_child_path(root, root / "docs" / "spec.docx"))
        self.assertFalse(is_safe_child_path(root, root.parent / "evil.docx"))


if __name__ == "__main__":
    unittest.main()
