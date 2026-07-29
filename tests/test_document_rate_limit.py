from __future__ import annotations

import unittest

from app.collector.document_rate_limit import resolve_document_rate_limit_seconds


class DocumentRateLimitTest(unittest.TestCase):
    def test_uses_document_specific_rate_limit_when_set(self) -> None:
        self.assertEqual(resolve_document_rate_limit_seconds(8.5, 1.1), 8.5)

    def test_falls_back_to_general_rate_limit_when_unset(self) -> None:
        self.assertEqual(resolve_document_rate_limit_seconds(None, 1.1), 1.1)

    def test_falls_back_to_general_rate_limit_when_non_positive(self) -> None:
        self.assertEqual(resolve_document_rate_limit_seconds(0, 1.1), 1.1)
        self.assertEqual(resolve_document_rate_limit_seconds(-2, 1.1), 1.1)


if __name__ == "__main__":
    unittest.main()
