from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


psycopg = types.ModuleType("psycopg")
psycopg.rows = types.ModuleType("psycopg.rows")
psycopg.rows.dict_row = object()
psycopg.types = types.ModuleType("psycopg.types")
psycopg.types.json = types.ModuleType("psycopg.types.json")
psycopg.types.json.Jsonb = lambda value: value
sys.modules.setdefault("psycopg", psycopg)
sys.modules.setdefault("psycopg.rows", psycopg.rows)
sys.modules.setdefault("psycopg.types", psycopg.types)
sys.modules.setdefault("psycopg.types.json", psycopg.types.json)

config = types.ModuleType("app.config")
config.settings = SimpleNamespace(database_url="postgresql://test/test", llm_json_mode=True)
sys.modules.setdefault("app.config", config)

yaml = types.ModuleType("yaml")
yaml.safe_load = lambda text: {}
sys.modules.setdefault("yaml", yaml)

from app.document_risk_analyzer import classify_document, classify_document_details
from app.document_status import build_document_status
from app.llm import context_builder
from app.llm.spec_facts_extractor import build_spec_facts
from app.technical_spec_detector import (
    detect_technical_spec_content,
    extract_technical_spec_sections,
)


class TechnicalSpecDetectionTest(unittest.TestCase):
    def test_kd_document_with_embedded_tz_is_technical_spec_candidate(self) -> None:
        text = """
        Раздел 3. Техническое задание
        Описание объекта закупки
        Наименование товара | Количество | Ед. изм.
        Сервер | 2 | шт.
        """

        details = classify_document_details("КД материалы.docx", text)

        self.assertEqual(details["doc_type"], "technical_spec")
        self.assertEqual(details["doc_type_reason"], "technical_section_found_inside_document")
        self.assertTrue(details["technical_spec_detection"]["detected"])
        self.assertTrue(details["technical_spec_detection"]["technical_section_found"])

    def test_extracts_embedded_tz_section_from_kd(self) -> None:
        text = """
        Раздел 1. Процедура закупки
        Общие сведения о процедуре и сроках подачи предложений.

        Приложение №2. Техническое задание
        Наименование товара | Характеристики | Кол-во | Ед. изм.
        Сервер | CPU не менее 16 ядер, RAM не менее 128 ГБ, SSD | 2 | шт.

        Раздел 3. Требования к заявке
        Участник прикладывает декларацию соответствия.
        """

        result = extract_technical_spec_sections("2_КД_материалы_ОНМ_серверы пк.docx", text)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["sections"]), 1)
        section = result["sections"][0]
        self.assertIn("Техническое задание", section["title"])
        self.assertEqual(section["reason"], "technical_section_found_inside_document")
        self.assertIn("Сервер", section["text"])
        self.assertNotIn("Требования к заявке", section["text"])

    def test_extracts_object_description_section_with_equipment_rows(self) -> None:
        text = """
        Раздел 1. Общие условия закупки

        Описание объекта закупки
        Наименование оборудования | Характеристика | Количество | Ед. изм.
        Рабочая станция | CPU не менее 8 ядер, RAM 32 ГБ, SSD 1 ТБ | 5 | шт.

        Форма заявки
        Таблица для заполнения участником.
        """

        result = extract_technical_spec_sections("КД закупочная документация.docx", text)

        self.assertTrue(result["found"])
        self.assertIn("Описание объекта закупки", result["sections"][0]["title"])
        self.assertIn("Рабочая станция", result["sections"][0]["text"])
        self.assertNotIn("Форма заявки", result["sections"][0]["text"])

    def test_xlsx_nomenclature_table_is_technical_spec_candidate(self) -> None:
        text = """
        Наименование товара\tХарактеристика\tЗначение\tКоличество\tЕд. изм.
        Сервер\tПроцессор\tне менее 16 ядер\t2\tшт
        """

        detection = detect_technical_spec_content("Документация.xlsx", text)
        details = classify_document_details("Документация.xlsx", text)

        self.assertTrue(detection["detected"])
        self.assertEqual(details["doc_type"], "technical_spec")
        self.assertIn(
            details["doc_type_reason"],
            {
                "technical_section_found_inside_document",
                "content_contains_supply_table",
                "content_contains_technical_spec",
            },
        )

    def test_price_document_with_supply_table_is_technical_context(self) -> None:
        text = """
        Обоснование НМЦК
        Наименование товара | Количество | Ед. изм. | Цена
        Коммутатор | 4 | шт. | 100000
        """

        details = classify_document_details("Обоснование НМЦК.xlsx", text)

        self.assertEqual(details["doc_type"], "technical_spec")
        self.assertEqual(details["doc_type_reason"], "price_doc_contains_supply_spec")

    def test_contract_with_single_equipment_word_is_not_technical_spec(self) -> None:
        text = "Договор на поставку оборудования. Оплата производится после приемки товара."

        self.assertEqual(classify_document("Проект договора.docx", text), "contract")

    def test_contract_with_specification_references_is_not_technical_spec(self) -> None:
        text = """
        Договор на поставку оборудования.
        Поставщик обязуется поставить оборудование согласно Спецификации.
        Наименование, ассортимент, количество товара определяются в Спецификации.
        Цена товара указывается в Спецификации.
        Поставка производится по адресам, указанным в Спецификации.
        Спецификация является неотъемлемой частью Договора.
        Приложение № 1 к Договору.
        """

        detection = detect_technical_spec_content("Проект договора обор-е.docx", text)
        details = classify_document_details("Проект договора обор-е.docx", text)

        self.assertFalse(detection["detected"])
        self.assertEqual(detection["reason"], "references_specification_only")
        self.assertEqual(details["doc_type"], "contract")

        section_result = extract_technical_spec_sections("Проект договора обор-е.docx", text)
        self.assertFalse(section_result["found"])
        self.assertTrue(section_result["references_only"])

    def test_notice_with_server_pc_title_is_not_technical_spec_without_items(self) -> None:
        text = """
        Извещение о закупке
        Поставка оборудования не требующего монтажа (ОНМ): серверы ПК.
        Начальная максимальная цена договора указана в извещении.
        Место поставки и срок подачи заявок указаны в документации.
        """

        detection = detect_technical_spec_content("Извещение_ОНМ_серверы пк.docx", text)
        details = classify_document_details("Извещение_ОНМ_серверы пк.docx", text)

        self.assertFalse(detection["detected"])
        self.assertIsNone(detection["reason"])
        self.assertEqual(details["doc_type"], "other")

    def test_blank_specification_template_is_not_technical_spec(self) -> None:
        text = """
        Спецификация
        № п/п | Наименование и характеристики товара | Ед. изм. | Кол-во | Цена
        """

        detection = detect_technical_spec_content("Приложение 1 Спецификация.docx", text)
        details = classify_document_details("Приложение 1 Спецификация.docx", text)

        self.assertFalse(detection["detected"])
        self.assertEqual(detection["reason"], "blank_specification_template")
        self.assertEqual(details["doc_type"], "other")

        section_result = extract_technical_spec_sections("Приложение 1 Спецификация.docx", text)
        self.assertFalse(section_result["found"])
        self.assertTrue(section_result["blank_template"])

    def test_spec_facts_extract_supply_items_from_kd_excel_like_text(self) -> None:
        text = """
        Техническое задание
        Наименование товара\tХарактеристика\tЗначение\tКоличество\tЕд. изм.
        Сервер\tПроцессор\tне менее 16 ядер\t2\tшт
        Сервер\tОперативная память\tне менее 128 ГБ\t2\tшт
        """

        facts = build_spec_facts(
            tender_title="Поставка оборудования не требующего монтажа",
            documents=[{"filename": "КД материалы.docx", "text": text}],
        )

        self.assertEqual(len(facts["supply_items"]), 1)
        self.assertEqual(facts["supply_items"][0]["name"], "Сервер")
        self.assertEqual(facts["supply_items"][0]["quantity"], "2")
        characteristic_names = {
            item["name"] for item in facts["technical_characteristics"]
        }
        self.assertIn("Сервер - Процессор", characteristic_names)

    def test_context_builder_prioritizes_content_detected_spec_document(self) -> None:
        tender = {
            "id": "tender-1",
            "title": "Поставка оборудования не требующего монтажа",
            "external_id": "00000018",
            "raw": {"full": {}, "short": {}},
        }
        documents = [
            {
                "id": "doc-1",
                "filename": "КД материалы.docx",
                "extracted_text": (
                    "Раздел 3. Техническое задание\n"
                    "Описание объекта закупки\n"
                    "Наименование товара | Количество | Ед. изм.\n"
                    "Сервер | 2 | шт."
                ),
            }
        ]

        with patch.object(context_builder, "get_company_profile", return_value=None):
            package = context_builder.build_llm_package(
                tender,
                documents,
                max_spec_chars=5000,
                max_other_chars=1000,
            )

        self.assertEqual(len(package["technical_spec_documents"]), 1)
        self.assertEqual(
            package["technical_spec_documents"][0]["filename"],
            "КД материалы.docx",
        )
        self.assertIn(
            "Техническая часть найдена внутри закупочной документации",
            context_builder.package_to_markdown(package),
        )
        self.assertIn(
            "## ТЗ / описание объекта закупки / спецификация",
            context_builder.package_to_markdown(package),
        )
        self.assertIn(
            "### Найденный раздел: Раздел 3. Техническое задание",
            context_builder.package_to_markdown(package),
        )

    def test_context_builder_finds_section_level_tz_in_synthetic_kd_materials(self) -> None:
        tender = {
            "id": "tender-00000018",
            "title": "Поставка оборудования не требующего монтажа (ОНМ)",
            "external_id": "00000018",
            "raw": {"full": {}, "short": {}},
        }
        documents = [
            {
                "id": "doc-kd",
                "filename": "2_КД_материалы_ОНМ_серверы пк.docx",
                "extracted_text": (
                    "Приложение 2 к конкурсной документации\n"
                    "Описание предмета закупки\n"
                    "Техническое задание\n"
                    "Наименование товара | Характеристики | Количество | Ед. изм.\n"
                    "Сервисный маршрутизатор | портов Ethernet не менее 8 | 1 | шт.\n"
                    "МФУ | печать А4 | 2 | шт.\n"
                    "Сервер | CPU не менее 16 ядер, RAM не менее 128 ГБ, SSD | 2 | шт.\n"
                    "АРМ | CPU не менее 8 ядер, RAM не менее 32 ГБ | 5 | шт.\n\n"
                    "Проект договора\n"
                    "Оплата производится после приемки товара."
                ),
            }
        ]

        with patch.object(context_builder, "get_company_profile", return_value=None):
            package = context_builder.build_llm_package(
                tender,
                documents,
                max_spec_chars=5000,
                max_other_chars=1000,
            )

        markdown = context_builder.package_to_markdown(package)

        self.assertEqual(len(package["technical_spec_documents"]), 1)
        self.assertEqual(
            package["technical_spec_documents"][0]["doc_type_reason"],
            "technical_section_found_inside_document",
        )
        self.assertIn("2_КД_материалы_ОНМ_серверы пк.docx", markdown)
        self.assertIn("Найденный раздел", markdown)
        self.assertIn("Описание предмета закупки", markdown)
        self.assertIn("Техническое задание", markdown)
        self.assertIn("Сервисный маршрутизатор", package["technical_spec_documents"][0]["text"])
        self.assertNotIn("Оплата производится", package["technical_spec_documents"][0]["text"])

    def test_lead_context_contains_selected_primary_document_text(self) -> None:
        tender = {
            "id": "tender-id",
            "external_id": "lead-primary-context",
            "title": "Поставка серверного оборудования",
            "initial_price": 10_000_000,
            "raw": {"full": {}},
        }
        documents = [
            {
                "id": "contract",
                "filename": "contract.docx",
                "extracted_text": "Проект договора. Оплата производится после приемки.",
            },
            {
                "id": "tz",
                "filename": "tz.docx",
                "extracted_text": (
                    "Техническое задание. Поставка серверов: CPU 32 ядра, "
                    "RAM 512 ГБ, RAID, 4 порта 10GbE."
                ),
            },
        ]
        selection = {
            "primary_document_id": "tz",
            "primary_document_role": "technical_requirements",
            "primary_document_confidence": "high",
            "primary_document_title": "tz.docx",
            "secondary_document_ids": ["contract"],
            "excluded_document_ids": [],
            "status": "technical_spec_found",
            "reason": "Selected TZ.",
        }

        with patch.object(context_builder, "get_company_profile", return_value=None):
            package = context_builder.build_llm_package(
                tender,
                documents,
                max_spec_chars=5000,
                max_other_chars=2000,
                document_selection=selection,
            )

        package["meta"]["report_kind"] = "lead"
        package["document_status"] = build_document_status(
            document_selection=selection,
            documents_summary=package["documents_summary"],
        )
        markdown = context_builder.package_to_markdown(package)

        self.assertIn("## 4. Primary technical document text", markdown)
        self.assertIn("RAM 512", markdown)
        self.assertIn("## 5. Secondary supporting excerpts", markdown)
        self.assertIn("Оплата производится", markdown)

    def test_context_builder_uses_section_text_instead_of_whole_kd(self) -> None:
        tender = {
            "id": "tender-1",
            "title": "Поставка оборудования не требующего монтажа",
            "external_id": "00000018",
            "raw": {"full": {}, "short": {}},
        }
        documents = [
            {
                "id": "doc-1",
                "filename": "2_КД_материалы_ОНМ_серверы пк.docx",
                "extracted_text": (
                    "Раздел 1. Процедура закупки\n"
                    "Инструкция участнику закупки.\n\n"
                    "Приложение №2. Техническое задание\n"
                    "Наименование товара | Характеристики | Кол-во | Ед. изм.\n"
                    "Сервер | CPU не менее 16 ядер, RAM не менее 128 ГБ | 2 | шт.\n\n"
                    "Порядок оплаты\n"
                    "Оплата производится после приемки товара."
                ),
            }
        ]

        with patch.object(context_builder, "get_company_profile", return_value=None):
            package = context_builder.build_llm_package(
                tender,
                documents,
                max_spec_chars=5000,
                max_other_chars=1000,
            )

        markdown = context_builder.package_to_markdown(package)
        technical_block = markdown.split(
            "## ТЗ / описание объекта закупки / спецификация",
            1,
        )[1].split("## Фрагменты про сроки поставки", 1)[0]

        self.assertEqual(len(package["technical_spec_documents"]), 1)
        self.assertNotIn("Порядок оплаты", package["technical_spec_documents"][0]["text"])
        self.assertEqual(len(package["spec_facts"]["supply_items"]), 1)
        self.assertIn("Найденный раздел", technical_block)
        self.assertIn("Причина: technical_section_found_inside_document", technical_block)
        self.assertIn("Сервер", technical_block)
        self.assertNotIn("Порядок оплаты", technical_block)
        self.assertNotIn("Оплата производится", technical_block)

    def test_explicit_tz_classification_still_works(self) -> None:
        details = classify_document_details(
            "ТЗ.pdf",
            "Наименование товара | Количество | Ед. изм.\nСХД | 1 | шт.",
        )

        self.assertEqual(details["doc_type"], "technical_spec")

    def test_object_description_with_real_server_specs_is_technical_spec(self) -> None:
        text = """
        Описание объекта закупки
        Сервер: количество 2 шт, процессор не менее 16 ядер, RAM не менее 128 ГБ.
        Система хранения данных: количество 1 шт, емкость не менее 100 ТБ.
        """

        details = classify_document_details("Описание объекта закупки.docx", text)

        self.assertEqual(details["doc_type"], "technical_spec")
        self.assertIn(
            details["doc_type_reason"],
            {"filename_or_header_contains_technical_spec", "content_contains_technical_spec"},
        )

    def test_context_builder_does_not_prioritize_references_only_contract(self) -> None:
        tender = {
            "id": "tender-1",
            "title": "Поставка оборудования не требующего монтажа",
            "external_id": "00000018",
            "raw": {"full": {}, "short": {}},
        }
        documents = [
            {
                "id": "doc-1",
                "filename": "Проект договора обор-е.docx",
                "extracted_text": (
                    "Договор на поставку оборудования.\n"
                    "Поставщик обязуется поставить оборудование согласно Спецификации.\n"
                    "Количество и цена указаны в Спецификации.\n"
                    "Спецификация является неотъемлемой частью Договора."
                ),
            }
        ]

        with patch.object(context_builder, "get_company_profile", return_value=None):
            package = context_builder.build_llm_package(
                tender,
                documents,
                max_spec_chars=5000,
                max_other_chars=1000,
            )

        markdown = context_builder.package_to_markdown(package)

        self.assertEqual(package["technical_spec_documents"], [])
        self.assertNotIn("## ТЗ / описание объекта закупки / спецификация", markdown)
        self.assertIn(
            "## Техническая спецификация не найдена в доступных документах",
            markdown,
        )
        self.assertIn("Документ ссылается на спецификацию", markdown)
        self.assertIn(
            "Спецификация упоминается в документах, но сама заполненная таблица спецификации не найдена.",
            markdown,
        )

    def test_context_builder_uses_honest_heading_when_no_technical_spec(self) -> None:
        tender = {
            "id": "tender-1",
            "title": "Поставка оборудования не требующего монтажа",
            "external_id": "00000018",
            "raw": {"full": {}, "short": {}},
        }
        documents = [
            {
                "id": "doc-1",
                "filename": "Извещение_ОНМ_серверы пк.docx",
                "extracted_text": (
                    "Извещение о закупке\n"
                    "Поставка оборудования не требующего монтажа (ОНМ): серверы ПК.\n"
                    "Общие сведения о процедуре закупки."
                ),
            }
        ]

        with patch.object(context_builder, "get_company_profile", return_value=None):
            package = context_builder.build_llm_package(
                tender,
                documents,
                max_spec_chars=5000,
                max_other_chars=1000,
            )

        markdown = context_builder.package_to_markdown(package)

        self.assertEqual(package["technical_spec_documents"], [])
        self.assertNotIn("## ТЗ / описание объекта закупки / спецификация", markdown)
        self.assertIn(
            "## Техническая спецификация не найдена в доступных документах",
            markdown,
        )
        self.assertIn("Это не полноценное ТЗ.", markdown)


if __name__ == "__main__":
    unittest.main()
