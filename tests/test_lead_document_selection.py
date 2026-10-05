from __future__ import annotations

import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


config = types.ModuleType("app.config")
config.settings = SimpleNamespace(database_url="postgresql://test/test", llm_json_mode=True)
sys.modules.setdefault("app.config", config)

factory = types.ModuleType("app.llm.factory")
factory.create_llm_client = lambda **kwargs: None
sys.modules.setdefault("app.llm.factory", factory)

from app.document_quality import (
    DOCUMENT_CONFIDENCE_HIGH,
    DOCUMENT_CONFIDENCE_LOW,
    DOCUMENT_CONFIDENCE_MEDIUM,
    DOCUMENT_SOURCE_CARD_ONLY,
    DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
    DOCUMENT_SOURCE_MISSING,
    DOCUMENT_SOURCE_OBJECT_DESCRIPTION,
    DOCUMENT_SOURCE_PRICING_OR_BOM,
    DOCUMENT_SOURCE_TECHNICAL_SPEC,
)
from app.document_status import (
    TECHNICAL_SPEC_FOUND,
    build_document_status,
    document_status_quality_label,
)
from app.llm import document_selector


class FakeSelectorClient:
    provider = "routerai"
    model = "fake"

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def generate_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text=json.dumps(self.payload, ensure_ascii=False),
            provider=self.provider,
            model=self.model,
            response_id=None,
            usage=None,
        )


class LeadDocumentSelectionTest(unittest.TestCase):
    def test_deterministic_single_tz_skips_llm_selector(self) -> None:
        candidates = [
            {
                "id": "tz",
                "title": "Техническое задание",
                "filename": "tz.docx",
                "doc_type": "technical_spec",
                "doc_type_reason": "filename_or_header_contains_technical_spec",
                "technical_spec_detection": {"detected": True, "technical_sections_count": 1},
                "text_len": 2000,
                "excerpt": "Технические требования: сервер, CPU, RAM, RAID.",
            }
        ]

        with (
            patch.object(document_selector, "build_document_candidates", return_value=candidates),
            patch.object(document_selector, "create_llm_client") as create_client,
        ):
            result = document_selector.select_documents_for_lead_report(
                {"title": "Поставка серверов"},
                [{"id": "ignored"}],
            )

        create_client.assert_not_called()
        self.assertEqual(result["primary_document_id"], "tz")
        self.assertEqual(result["status"], TECHNICAL_SPEC_FOUND)
        self.assertFalse(result["selector_used"])

    def test_llm_selector_can_choose_spec_over_contract_forms_protocol_and_nmck(self) -> None:
        candidates = [
            {
                "id": "contract",
                "title": "Проект договора",
                "filename": "contract.docx",
                "doc_type": "contract",
                "doc_type_reason": "contract",
                "technical_spec_detection": {"detected": False},
                "text_len": 1000,
                "excerpt": "Условия договора, оплата, приемка.",
            },
            {
                "id": "form",
                "title": "Форма заявки",
                "filename": "form.docx",
                "doc_type": "application_requirements",
                "doc_type_reason": "application",
                "technical_spec_detection": {"detected": False},
                "text_len": 700,
                "excerpt": "Форма заявки участника.",
            },
            {
                "id": "protocol",
                "title": "Протокол",
                "filename": "protocol.docx",
                "doc_type": "other",
                "doc_type_reason": "protocol",
                "technical_spec_detection": {"detected": False},
                "text_len": 500,
                "excerpt": "Протокол комиссии.",
            },
            {
                "id": "nmck",
                "title": "Обоснование НМЦК",
                "filename": "nmck.xlsx",
                "doc_type": "price_calculation",
                "doc_type_reason": "price",
                "technical_spec_detection": {"detected": False},
                "text_len": 900,
                "excerpt": "Расчет начальной цены.",
            },
            {
                "id": "spec",
                "title": "Приложение с характеристиками",
                "filename": "appendix.docx",
                "doc_type": "other",
                "doc_type_reason": "other",
                "technical_spec_detection": {"detected": True},
                "text_len": 3000,
                "excerpt": "Характеристики серверов: CPU, RAM, RAID, SSD, Ethernet.",
            },
        ]
        client = FakeSelectorClient(
            {
                "primary_document_id": "spec",
                "primary_document_role": "technical_requirements",
                "primary_document_confidence": "high",
                "secondary_document_ids": ["contract", "nmck"],
                "excluded_document_ids": [
                    {"id": "form", "reason": "application form"},
                    {"id": "protocol", "reason": "protocol"},
                ],
                "status": "technical_spec_found",
                "reason": "The appendix contains real technical characteristics.",
            }
        )

        with (
            patch.object(document_selector, "build_document_candidates", return_value=candidates),
            patch.object(document_selector, "create_llm_client", return_value=client),
        ):
            result = document_selector.select_documents_for_lead_report(
                {"title": "Поставка серверного оборудования"},
                [{"id": "ignored"}],
            )

        self.assertEqual(result["primary_document_id"], "spec")
        self.assertEqual(result["secondary_document_ids"], ["contract", "nmck"])
        self.assertEqual(result["status"], TECHNICAL_SPEC_FOUND)
        self.assertTrue(result["selector_used"])
        self.assertFalse(result["selector_failed"])
        self.assertEqual(len(client.calls), 1)

    def test_selector_failure_uses_deterministic_fallback(self) -> None:
        candidates = [
            {
                "id": "tz1",
                "title": "Техническое задание",
                "filename": "tz1.docx",
                "doc_type": "technical_spec",
                "doc_type_reason": "filename_or_header_contains_technical_spec",
                "technical_spec_detection": {"detected": True},
                "text_len": 2000,
                "excerpt": "Технические требования к серверу.",
            },
            {
                "id": "tz2",
                "title": "Описание объекта закупки",
                "filename": "tz2.docx",
                "doc_type": "technical_spec",
                "doc_type_reason": "content_contains_technical_spec",
                "technical_spec_detection": {"detected": True},
                "text_len": 1800,
                "excerpt": "Описание объекта закупки.",
            },
        ]

        with (
            patch.object(document_selector, "build_document_candidates", return_value=candidates),
            patch.object(document_selector, "create_llm_client", side_effect=RuntimeError("boom")),
        ):
            result = document_selector.select_documents_for_lead_report(
                {"title": "Поставка серверов"},
                [{"id": "ignored"}],
            )

        self.assertEqual(result["status"], "selector_failed")
        self.assertTrue(result["selector_failed"])
        self.assertEqual(result["primary_document_id"], "tz1")

        status = build_document_status(document_selection=result, documents_summary=[])
        self.assertEqual(status["code"], TECHNICAL_SPEC_FOUND)
        self.assertEqual(status["selector_status"], "selector_failed")

    def test_llm_selector_strict_router_schema_keeps_section_hint(self) -> None:
        candidates = [
            {
                "id": "general-doc",
                "title": "Извещение и документация о закупке.docx",
                "filename": "notice.docx",
                "doc_type": "other",
                "doc_type_reason": "procurement_documentation",
                "technical_spec_detection": {
                    "detected": True,
                    "technical_sections_count": 1,
                    "technical_section_titles": ["Раздел 3. Техническое задание"],
                },
                "text_len": 6000,
                "section_hints": ["Раздел 3. Техническое задание", "Спецификация"],
                "excerpt": "Раздел 3. Техническое задание. Спецификация с позициями.",
            }
        ]
        client = FakeSelectorClient(
            {
                "primary_document_id": "general-doc",
                "primary_document_title": "Извещение и документация о закупке.docx",
                "primary_section_hint": "Раздел 3. Техническое задание / Спецификация",
                "primary_document_role": "documentation",
                "primary_document_confidence": "medium",
                "secondary_document_ids": [],
                "selector_status": "technical_spec_found",
                "reason": "General documentation contains the technical section.",
            }
        )

        with (
            patch.object(document_selector, "build_document_candidates", return_value=candidates),
            patch.object(document_selector, "create_llm_client", return_value=client),
        ):
            result = document_selector.select_documents_for_lead_report(
                {"title": "Поставка серверного оборудования"},
                [{"id": "ignored"}],
            )

        self.assertEqual(result["primary_document_id"], "general-doc")
        self.assertEqual(result["primary_document_role"], "documentation")
        self.assertEqual(result["selector_status"], "technical_spec_found")
        self.assertEqual(result["status"], "technical_spec_found")
        self.assertIn("Спецификация", result["primary_section_hint"])

        status = build_document_status(document_selection=result, documents_summary=[])
        self.assertEqual(status["primary_section_hint"], result["primary_section_hint"])

    def test_embedded_technical_requirements_selected_as_primary(self) -> None:
        text = (
            "Раздел 1. Общие сведения\n"
            + ("Порядок подачи заявок.\n" * 120)
            + "Раздел 3. Техническое задание\n"
            + "Приложение №1 к техническому заданию\n"
            + "Спецификация\n"
            + "Наименование товара | Характеристики | Количество | Ед. изм.\n"
            + "Система хранения STORAGE-9000 | дисковое пространство не менее 100 ТБ | 1 | шт.\n"
        )
        documents = [
            {
                "id": "notice-docs",
                "filename": "Извещение и документация о закупке.docx",
                "extracted_text": text,
            }
        ]

        result = document_selector.select_documents_for_lead_report(
            {"title": "Поставка системы хранения STORAGE-9000"},
            documents,
        )

        self.assertEqual(result["status"], "technical_spec_found")
        self.assertEqual(result["primary_document_id"], "notice-docs")
        self.assertIn("Техническое задание", result["primary_section_hint"])
        self.assertEqual(
            result["primary_document_title"],
            "Извещение и документация о закупке.docx",
        )
        smart_excerpt_text = "\n".join(
            item["text"]
            for item in result["candidates"][0]["smart_excerpts"]
        )
        self.assertIn("STORAGE-9000", smart_excerpt_text)

    def test_late_product_table_occurrence_is_included_in_smart_excerpts(self) -> None:
        text = (
            ("Техническое задание\nОбщие требования к поставке.\n" + ("общий текст\n" * 80))
            * 8
            + "Спецификация\n"
            + "Наименование товара | Характеристики | Количество\n"
            + "Storage MODEL-9000 | RAID, SSD, 100 TB | 1 шт.\n"
        )
        candidates = document_selector.build_document_candidates(
            [
                {
                    "id": "late-table",
                    "filename": "Документация.docx",
                    "extracted_text": text,
                }
            ],
            tender={"title": "Поставка Storage MODEL-9000"},
        )

        excerpt_text = "\n".join(
            item["text"] for item in candidates[0]["smart_excerpts"]
        )
        self.assertIn("MODEL-9000", excerpt_text)

    def test_embedded_technical_section_and_late_product_table_are_linked(self) -> None:
        text = (
            "Раздел 1. Общие сведения\n"
            + ("Порядок подачи заявок и общие условия закупки.\n" * 300)
            + "Раздел 3. Техническое задание\n"
            + (
                "Требования к количеству, ассортименту, сведения о сроках и месте поставки "
                "приведены в настоящем техническом задании, включая спецификацию "
                "(приложение № 1 к техническому заданию).\n"
            )
            + ("Промежуточные условия документации.\n" * 1200)
            + "Приложение № 1 к техническому заданию\n"
            + "Спецификация\n"
            + "№ п/п | Наименование товара | Предельная цена единицы товара (руб.) | Кол-во, шт. | Назначение | Место поставки\n"
            + "Полка расширения, Артикул ABC-123 | 800 000,00 | 1 | расширение емкости | Москва\n"
            + "Твердотельный накопитель, модель SSD-960 | 200 000,00 | 24 | накопитель | Москва\n"
        )

        result = document_selector.select_documents_for_lead_report(
            {"title": "Поставка оборудования хранения данных"},
            [
                {
                    "id": "embedded-general-doc",
                    "filename": "000000108_Извещение и документация о закупке.docx",
                    "extracted_text": text,
                }
            ],
            use_llm_selector=False,
        )

        self.assertEqual(result["status"], "technical_spec_found")
        self.assertEqual(result["selector_status"], "technical_spec_found")
        self.assertEqual(result["primary_document_id"], "embedded-general-doc")
        self.assertEqual(
            result["primary_document_title"],
            "000000108_Извещение и документация о закупке.docx",
        )
        self.assertIn("Раздел 3. Техническое задание", result["primary_section_hint"])
        self.assertIn("Приложение № 1 к техническому заданию", result["primary_section_hint"])
        self.assertIn("Спецификация", result["primary_section_hint"])

        detection = result["candidates"][0]["technical_spec_detection"]
        self.assertTrue(detection["detected"])
        self.assertGreater(detection["technical_sections_count"], 0)
        self.assertTrue(detection["section_has_real_spec"])
        self.assertTrue(detection["has_product_table"])
        self.assertFalse(detection["blank_template"])

        smart_excerpt_text = "\n".join(
            item["text"] for item in result["candidates"][0]["smart_excerpts"]
        )
        self.assertIn("Раздел 3. Техническое задание", smart_excerpt_text)
        self.assertIn("Приложение № 1 к техническому заданию", smart_excerpt_text)
        self.assertIn("Спецификация", smart_excerpt_text)
        self.assertIn("Полка расширения", smart_excerpt_text)
        self.assertIn("Твердотельный накопитель", smart_excerpt_text)

    def test_standalone_technical_assignment_pdf_remains_selected(self) -> None:
        result = document_selector.select_documents_for_lead_report(
            {"title": "Поставка серверного оборудования"},
            [
                {
                    "id": "standalone-tz",
                    "filename": "000000107_ТехЗадание.pdf",
                    "extracted_text": (
                        "Техническое задание\n"
                        "Наименование товара | Характеристики | Количество | Ед. изм.\n"
                        "Сервер SERVER-1000 | CPU не менее 16 ядер, RAM 128 ГБ, SSD | 2 | шт.\n"
                    ),
                }
            ],
            use_llm_selector=False,
        )

        self.assertEqual(result["status"], "technical_spec_found")
        self.assertEqual(result["selector_status"], "technical_spec_found")
        self.assertEqual(result["primary_document_id"], "standalone-tz")
        self.assertEqual(result["primary_document_title"], "000000107_ТехЗадание.pdf")
        self.assertEqual(result["primary_document_confidence"], DOCUMENT_CONFIDENCE_HIGH)
        self.assertEqual(result["primary_document_source_kind"], DOCUMENT_SOURCE_TECHNICAL_SPEC)
        self.assertTrue(result["primary_document_is_full_technical_spec"])

    def test_object_description_is_high_confidence_source(self) -> None:
        result = document_selector.select_documents_for_lead_report(
            {"title": "Поставка серверного оборудования"},
            [
                {
                    "id": "ooz",
                    "filename": "Описание объекта закупки.docx",
                    "extracted_text": (
                        "Описание объекта закупки\n"
                        "Наименование товара | Характеристики | Количество\n"
                        "Сервер | CPU 32 ядра, RAM 256 ГБ, SSD NVMe | 2 шт.\n"
                    ),
                }
            ],
            use_llm_selector=False,
        )

        self.assertEqual(result["primary_document_confidence"], DOCUMENT_CONFIDENCE_HIGH)
        self.assertEqual(result["primary_document_source_kind"], DOCUMENT_SOURCE_OBJECT_DESCRIPTION)
        self.assertTrue(result["primary_document_is_full_technical_spec"])

    def test_specification_with_technical_characteristics_is_not_low_confidence(self) -> None:
        result = document_selector.select_documents_for_lead_report(
            {"title": "Поставка СХД"},
            [
                {
                    "id": "spec",
                    "filename": "Спецификация.docx",
                    "extracted_text": (
                        "Спецификация\n"
                        "Наименование товара | Характеристики | Количество\n"
                        "СХД STORAGE-9000 | емкость 100 ТБ, SSD, RAID | 1 шт.\n"
                    ),
                }
            ],
            use_llm_selector=False,
        )

        self.assertIn(
            result["primary_document_confidence"],
            {DOCUMENT_CONFIDENCE_HIGH, DOCUMENT_CONFIDENCE_MEDIUM},
        )
        self.assertNotEqual(result["primary_document_confidence"], DOCUMENT_CONFIDENCE_LOW)

    def test_nmck_with_item_table_is_medium_pricing_source_not_full_tz(self) -> None:
        candidates = document_selector.build_document_candidates(
            [
                {
                    "id": "nmck",
                    "filename": "Обоснование НМЦК.docx",
                    "extracted_text": (
                        "Обоснование НМЦК\n"
                        "Наименование товара | Характеристики | Количество | Цена\n"
                        "Сервер SERVER-1000 | CPU 32 ядра, RAM 256 ГБ | 2 шт. | 1000000\n"
                    ),
                }
            ],
            tender={"title": "Поставка серверов"},
        )
        client = FakeSelectorClient(
            {
                "primary_document_id": "nmck",
                "primary_document_role": "nmck",
                "primary_document_confidence": "high",
                "selector_status": "technical_spec_found",
                "reason": "Contains an item table.",
            }
        )

        with (
            patch.object(document_selector, "build_document_candidates", return_value=candidates),
            patch.object(document_selector, "create_llm_client", return_value=client),
        ):
            result = document_selector.select_documents_for_lead_report(
                {"title": "Поставка серверов"},
                [{"id": "ignored"}],
            )

        self.assertEqual(result["selector_status"], "technical_spec_found")
        self.assertEqual(result["primary_document_confidence"], DOCUMENT_CONFIDENCE_MEDIUM)
        self.assertEqual(result["primary_document_source_kind"], DOCUMENT_SOURCE_PRICING_OR_BOM)
        self.assertFalse(result["primary_document_is_full_technical_spec"])
        self.assertTrue(result["primary_document_suspicious"])
        status = build_document_status(document_selection=result, documents_summary=[])
        self.assertIn("средняя уверенность", document_status_quality_label(status))
        self.assertIn("not a full technical specification", status["warning"])

    def test_nmck_price_only_is_not_high_confidence(self) -> None:
        candidates = document_selector.build_document_candidates(
            [
                {
                    "id": "nmck",
                    "filename": "Расчет НМЦК.xlsx",
                    "extracted_text": (
                        "Расчет НМЦК\n"
                        "Наименование | Количество | Цена | Сумма\n"
                        "Позиция 1 | 1 | 1000000 | 1000000\n"
                    ),
                }
            ],
            tender={"title": "Поставка оборудования"},
        )
        selection = {
            "primary_document_id": "nmck",
            "primary_document_title": "Расчет НМЦК.xlsx",
            "selector_status": "technical_spec_found",
        }
        quality = document_selector._selection_with_quality(selection, candidates)

        self.assertNotEqual(quality["primary_document_confidence"], DOCUMENT_CONFIDENCE_HIGH)
        self.assertEqual(quality["primary_document_source_kind"], DOCUMENT_SOURCE_PRICING_OR_BOM)

    def test_notice_protocol_or_contract_only_is_low_confidence(self) -> None:
        candidates = document_selector.build_document_candidates(
            [
                {
                    "id": "contract",
                    "filename": "Проект договора.docx",
                    "extracted_text": "Проект договора. Порядок оплаты и приемки.",
                }
            ],
            tender={"title": "Поставка серверов"},
        )
        selection = {
            "primary_document_id": "contract",
            "primary_document_title": "Проект договора.docx",
            "selector_status": "technical_spec_found",
        }
        quality = document_selector._selection_with_quality(selection, candidates)

        self.assertEqual(quality["primary_document_confidence"], DOCUMENT_CONFIDENCE_LOW)
        self.assertEqual(quality["primary_document_source_kind"], DOCUMENT_SOURCE_CONTRACT_OR_NOTICE)
        self.assertFalse(quality["primary_document_is_full_technical_spec"])

    def test_no_documents_is_low_missing_confidence(self) -> None:
        result = document_selector.select_documents_for_lead_report(
            {"title": "Поставка серверов"},
            [],
            use_llm_selector=False,
        )

        self.assertEqual(result["primary_document_confidence"], DOCUMENT_CONFIDENCE_LOW)
        self.assertEqual(result["primary_document_source_kind"], DOCUMENT_SOURCE_MISSING)
        status = build_document_status(document_selection=result, documents_summary=[])
        self.assertEqual(status["primary_document_source_kind"], DOCUMENT_SOURCE_MISSING)

    def test_documents_without_text_are_low_card_only_confidence(self) -> None:
        selection = {
            "primary_document_id": "doc",
            "primary_document_title": "Техническое задание.docx",
            "selector_status": "technical_spec_found",
        }
        quality = document_selector._selection_with_quality(
            selection,
            [{"id": "doc", "title": "Техническое задание.docx", "text_len": 0}],
        )

        self.assertEqual(quality["primary_document_confidence"], DOCUMENT_CONFIDENCE_LOW)
        self.assertEqual(quality["primary_document_source_kind"], DOCUMENT_SOURCE_CARD_ONLY)

    def test_blank_spec_template_not_selected(self) -> None:
        documents = [
            {
                "id": "blank-spec",
                "filename": "Спецификация.docx",
                "extracted_text": (
                    "Спецификация\n"
                    "№ п/п | Наименование и характеристики товара | Ед. изм. | Кол-во | Цена\n"
                    "Спецификация будет заполнена победителем."
                ),
            }
        ]
        client = FakeSelectorClient(
            {
                "primary_document_id": "blank-spec",
                "primary_document_role": "specification",
                "primary_document_confidence": "high",
                "secondary_document_ids": [],
                "excluded_document_ids": [],
                "status": "technical_spec_found",
                "reason": "Looks like a specification.",
            }
        )

        with patch.object(document_selector, "create_llm_client", return_value=client):
            result = document_selector.select_documents_for_lead_report(
                {"title": "Поставка серверов"},
                documents,
            )

        self.assertEqual(result["status"], "documents_present_no_primary_spec")
        self.assertIsNone(result["primary_document_id"])
        self.assertTrue(result["selector_used"])


if __name__ == "__main__":
    unittest.main()
