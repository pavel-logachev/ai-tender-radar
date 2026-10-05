from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.document_quality import (
    DOCUMENT_CONFIDENCE_HIGH,
    DOCUMENT_CONFIDENCE_LOW,
    DOCUMENT_CONFIDENCE_MEDIUM,
    DOCUMENT_SOURCE_CARD_ONLY,
    DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
    DOCUMENT_SOURCE_PRICING_OR_BOM,
    DOCUMENT_SOURCE_TECHNICAL_SPEC,
)
from app.pipeline import audit_document_selection


class AuditDocumentSelectionTest(unittest.TestCase):
    def test_suggested_action_examples(self) -> None:
        self.assertEqual(
            audit_document_selection.suggested_action_for_status(
                {
                    "docs_count": 1,
                    "docs_with_text_count": 1,
                    "primary_document_confidence": DOCUMENT_CONFIDENCE_HIGH,
                    "primary_document_source_kind": DOCUMENT_SOURCE_TECHNICAL_SPEC,
                    "selector_status": "technical_spec_found",
                }
            ),
            "ok_high_confidence",
        )
        self.assertEqual(
            audit_document_selection.suggested_action_for_status(
                {
                    "docs_count": 1,
                    "docs_with_text_count": 1,
                    "primary_document_confidence": DOCUMENT_CONFIDENCE_MEDIUM,
                    "primary_document_source_kind": DOCUMENT_SOURCE_PRICING_OR_BOM,
                    "selector_status": "technical_spec_found",
                }
            ),
            "ok_medium_confidence_review",
        )
        self.assertEqual(
            audit_document_selection.suggested_action_for_status(
                {
                    "docs_count": 0,
                    "docs_with_text_count": 0,
                    "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
                    "primary_document_source_kind": DOCUMENT_SOURCE_CARD_ONLY,
                }
            ),
            "missing_docs",
        )
        self.assertEqual(
            audit_document_selection.suggested_action_for_status(
                {
                    "docs_count": 0,
                    "docs_with_text_count": 0,
                    "source_documents_count": 2,
                    "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
                    "primary_document_source_kind": DOCUMENT_SOURCE_CARD_ONLY,
                }
            ),
            "source_docs_not_downloaded",
        )
        self.assertEqual(
            audit_document_selection.suggested_action_for_status(
                {
                    "docs_count": 1,
                    "docs_with_text_count": 1,
                    "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
                    "primary_document_source_kind": DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
                    "selector_status": "technical_spec_found",
                }
            ),
            "selector_may_need_review",
        )
        self.assertEqual(
            audit_document_selection.suggested_action_for_status(
                {
                    "docs_count": 1,
                    "docs_with_text_count": 1,
                    "primary_document_confidence": DOCUMENT_CONFIDENCE_MEDIUM,
                    "primary_document_source_kind": DOCUMENT_SOURCE_PRICING_OR_BOM,
                    "primary_document_suspicious": True,
                    "selector_status": "technical_spec_found",
                }
            ),
            "suspicious_primary_doc",
        )

    def test_audit_output_row_marks_nmck_primary_as_medium_warning(self) -> None:
        config = types.ModuleType("app.config")
        config.settings = SimpleNamespace(database_url="postgresql://test/test", llm_json_mode=True)
        factory = types.ModuleType("app.llm.factory")
        factory.create_llm_client = lambda **kwargs: None
        row = {
            "external_id": "00000039",
            "title": "Поставка серверов",
            "customer_name": "Заказчик",
            "initial_price": 10_000_000,
            "llm_customer_lead_report_result": {
                "meta": {
                    "document_selection": {
                        "primary_document_id": "1",
                        "primary_document_title": "Обоснование НМЦК.docx",
                        "primary_section_hint": "Таблица товаров и цены",
                        "selector_status": "technical_spec_found",
                    }
                }
            },
            "docs_count": 1,
            "docs_with_text_count": 1,
            "documents": [
                {
                    "id": "1",
                    "filename": "Обоснование НМЦК.docx",
                    "extracted_text": (
                        "Обоснование НМЦК\n"
                        "Наименование товара | Характеристики | Количество | Цена\n"
                        "Сервер | CPU 32 ядра, RAM 256 ГБ | 2 шт. | 1000000\n"
                    ),
                }
            ],
        }

        with patch.dict(sys.modules, {"app.config": config, "app.llm.factory": factory}):
            output = audit_document_selection.audit_output_row(row)

        self.assertEqual(output["primary_document_confidence"], DOCUMENT_CONFIDENCE_MEDIUM)
        self.assertEqual(output["primary_document_source_kind"], DOCUMENT_SOURCE_PRICING_OR_BOM)
        self.assertEqual(output["suggested_action"], "suspicious_primary_doc")
        self.assertIn("not a full technical specification", output["readiness_warning"])

    def test_audit_output_row_distinguishes_source_inventory_not_downloaded(self) -> None:
        config = types.ModuleType("app.config")
        config.settings = SimpleNamespace(database_url="postgresql://test/test", llm_json_mode=True)
        factory = types.ModuleType("app.llm.factory")
        factory.create_llm_client = lambda **kwargs: None
        row = {
            "external_id": "00000034",
            "title": "Server supply",
            "customer_name": "Customer",
            "initial_price": 1_000_000,
            "raw": {
                "full": {
                    "documents": [
                        {"id": "doc-1", "title": "Technical task.pdf"},
                        {"id": "doc-2", "title": "Contract.pdf"},
                    ]
                }
            },
            "llm_customer_lead_report_result": {
                "meta": {
                    "document_preparation": {
                        "preparation_status": "no_valid_documents",
                        "documents_found": 2,
                        "documents_selected": 2,
                        "documents_failed": 2,
                    }
                }
            },
            "docs_count": 0,
            "docs_with_text_count": 0,
            "documents": [],
        }

        with patch.dict(sys.modules, {"app.config": config, "app.llm.factory": factory}):
            output = audit_document_selection.audit_output_row(row)

        self.assertEqual(output["source_documents_count"], "2")
        self.assertEqual(
            output["document_inventory_state"],
            "source_inventory_not_downloaded",
        )
        self.assertEqual(output["document_preparation_status"], "no_valid_documents")
        self.assertEqual(output["documents_found"], "2")
        self.assertEqual(output["documents_selected"], "2")
        self.assertEqual(output["documents_failed"], "2")
        self.assertEqual(output["suggested_action"], "source_docs_not_downloaded")

    def test_audit_output_row_marks_stale_card_only_refresh_candidate(self) -> None:
        config = types.ModuleType("app.config")
        config.settings = SimpleNamespace(database_url="postgresql://test/test", llm_json_mode=True)
        factory = types.ModuleType("app.llm.factory")
        factory.create_llm_client = lambda **kwargs: None
        row = {
            "external_id": "00000022",
            "title": "Server supply",
            "customer_name": "Customer",
            "initial_price": 10_000_000,
            "llm_customer_lead_report_result": {
                "report": {
                    "document_status": {
                        "code": "documents_missing_card_only",
                        "analysis_basis": "lead_card_only",
                    }
                },
                "meta": {
                    "lead_context_mode": "card_only",
                    "lead_documents_missing_allowed": True,
                },
            },
            "docs_count": 1,
            "docs_with_text_count": 1,
            "documents": [
                {
                    "id": "1",
                    "filename": "Technical specification.docx",
                    "extracted_text": "Technical specification: server CPU RAM storage.",
                }
            ],
        }

        with patch.dict(sys.modules, {"app.config": config, "app.llm.factory": factory}):
            output = audit_document_selection.audit_output_row(row)

        self.assertEqual(output["has_full_lead_report"], "true")
        self.assertEqual(output["latest_report_card_only"], "true")
        self.assertEqual(output["stale_card_only_refresh_candidate"], "true")
        self.assertEqual(output["suggested_action"], "stale_card_only_refresh_candidate")

    def test_latest_report_card_only_recognizes_legacy_meta_marker(self) -> None:
        row = {
            "llm_customer_lead_report_result": {
                "report": {"summary": "legacy report"},
                "meta": {"context_source": "card_only"},
            }
        }

        self.assertTrue(audit_document_selection.latest_report_is_card_only(row))


if __name__ == "__main__":
    unittest.main()
