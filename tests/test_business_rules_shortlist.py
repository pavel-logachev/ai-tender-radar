from __future__ import annotations

import importlib
import sys
import unittest

for module_name in ("app.business_profile", "app.business_rules"):
    module = sys.modules.get(module_name)
    if module is not None and not getattr(module, "__file__", None):
        sys.modules.pop(module_name, None)

business_profile = importlib.import_module("app.business_profile")
business_rules = importlib.import_module("app.business_rules")

is_full_deal_for_category = business_profile.is_full_deal_for_category
load_business_profile = business_profile.load_business_profile
match_target_category = business_profile.match_target_category

business_assessment = business_rules.business_assessment
effective_recommendation = business_rules.effective_recommendation
is_non_core_transport_security_infrastructure = (
    business_rules.is_non_core_transport_security_infrastructure
)
is_service_noise = business_rules.is_service_noise
is_target_hardware = business_rules.is_target_hardware


def tender(title: str, *, price: int = 2_000_000, positive_matches: list[str] | None = None) -> dict:
    return {
        "title": title,
        "initial_price": price,
        "recommendation": "go",
        "result": {
            "summary": title,
            "positive_matches": positive_matches or [],
        },
    }


class BusinessRulesShortlistRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        business_rules._business_profile.cache_clear()
        self.profile = load_business_profile()

    def test_modular_fap_construction_is_not_target_hardware(self) -> None:
        row = tender("Поставка модульного ФАП с оснащением серверным оборудованием")

        self.assertFalse(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["market_access"], "service_noise")
        self.assertEqual(effective_recommendation(row), "no_go")

    def test_expansion_shelf_is_low_priority_not_target_hardware(self) -> None:
        row = tender("Поставка полки расширения СХД")

        self.assertFalse(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["action"], "skip_low_priority")
        self.assertEqual(effective_recommendation(row), "no_go")

    def test_maintenance_materials_are_low_priority_not_target_hardware(self) -> None:
        row = tender(
            "Поставка материалов для технического обслуживания рабочих станций, "
            "периферийного, сетевого и серверного оборудования"
        )

        self.assertFalse(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["action"], "skip_low_priority")
        self.assertEqual(effective_recommendation(row), "no_go")

    def test_transport_weighing_maintenance_is_service_noise(self) -> None:
        row = tender(
            "Выполнение работ по техническому обслуживанию и ремонту Комплексов "
            "аппаратно-программных автоматических весогабаритного контроля "
            '"Архимед"'
        )

        self.assertTrue(is_service_noise(row))
        self.assertFalse(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["market_access"], "service_noise")
        self.assertEqual(effective_recommendation(row), "no_go")

    def test_security_hardware_pak_supply_is_not_transport_service_noise(self) -> None:
        row = tender("Поставка программно-аппаратного комплекса firewall")

        self.assertFalse(is_service_noise(row))
        self.assertTrue(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["market_access"], "target_hardware")

    def test_transport_security_bridge_infrastructure_is_non_core(self) -> None:
        row = tender(
            "Оснащение объектов дорожного хозяйства инженерно-техническими средствами "
            "обеспечения транспортной безопасности мостов",
            price=250_000_000,
        )
        row["result"]["summary"] = (
            "Мосты, СКУД, охранная сигнализация, ССОИ, электроснабжение, ДГУ, "
            "ПИР, СМР, ПНР и сертификация ТС ОТБ по ПП РФ №969."
        )

        assessment = business_assessment(row)

        self.assertTrue(is_non_core_transport_security_infrastructure(row))
        self.assertTrue(is_service_noise(row))
        self.assertFalse(is_target_hardware(row))
        self.assertEqual(
            assessment["market_access"],
            business_rules.NON_CORE_TRANSPORT_SECURITY_REASON,
        )
        self.assertEqual(assessment["action"], "no_go")
        self.assertEqual(effective_recommendation(row), "no_go")

    def test_server_supply_is_not_hidden_by_generic_security_word_in_documents(self) -> None:
        row = tender("Поставка серверов", price=8_000_000)
        row["document_risk_result"] = {
            "key_findings": [
                "В документации упоминаются требования информационной безопасности."
            ]
        }

        self.assertFalse(is_non_core_transport_security_infrastructure(row))
        self.assertTrue(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["market_access"], "target_hardware")
        self.assertNotEqual(effective_recommendation(row), "no_go")

    def test_storage_supply_is_not_hidden(self) -> None:
        row = tender("Поставка СХД", price=9_000_000)
        row["document_risk_result"] = {
            "key_findings": [
                "Средства безопасности настраиваются по требованиям заказчика."
            ]
        }

        self.assertFalse(is_non_core_transport_security_infrastructure(row))
        self.assertTrue(is_target_hardware(row))
        self.assertEqual(business_assessment(row)["market_access"], "target_hardware")
        self.assertNotEqual(effective_recommendation(row), "no_go")

    def test_generic_equipment_title_ignores_weak_rule_based_matches(self) -> None:
        row = tender("Поставка оборудования", positive_matches=["сервер", "схд"])

        category_name, category_cfg = match_target_category(row, self.profile)

        self.assertIsNone(category_name)
        self.assertIsNone(category_cfg)
        self.assertFalse(is_target_hardware(row))

    def test_generic_equipment_title_does_not_pass_on_broad_storage_hint(self) -> None:
        row = {
            "title": "Поставка оборудования",
            "initial_price": 80_000_000,
            "recommendation": "go",
            "result": {
                "summary": "Поставка оборудования",
                "positive_matches": ["схд"],
            },
            "raw": {
                "description": "СХД / системы хранения данных",
            },
        }

        category_name, category_cfg = match_target_category(row, self.profile)

        self.assertEqual(category_name, "storage")
        self.assertFalse(is_full_deal_for_category(row, self.profile, category_cfg or {}))
        self.assertEqual(business_assessment(row)["market_access"], "unknown")

    def test_generic_equipment_title_passes_with_structured_hardware_products(self) -> None:
        row = {
            "title": "Поставка оборудования",
            "initial_price": 80_000_000,
            "recommendation": "go",
            "result": {
                "summary": "Поставка оборудования",
                "positive_matches": ["схд"],
            },
            "raw": {
                "full": {
                    "products": [
                        {"name": "Сервер", "quantity": "4"},
                        {"name": "Система хранения данных", "quantity": "1"},
                        {"name": "Коммутатор", "quantity": "2"},
                        {"name": "ИБП", "quantity": "1"},
                        {"name": "Телекоммуникационный шкаф", "quantity": "1"},
                    ]
                }
            },
        }

        category_name, category_cfg = match_target_category(row, self.profile)

        self.assertEqual(category_name, "storage")
        self.assertTrue(is_full_deal_for_category(row, self.profile, category_cfg or {}))

    def test_full_server_or_storage_supply_remains_target_hardware(self) -> None:
        server = tender("Поставка серверного оборудования")
        storage = tender("Поставка системы хранения данных")

        server_category, server_cfg = match_target_category(server, self.profile)
        storage_category, storage_cfg = match_target_category(storage, self.profile)

        self.assertEqual(server_category, "servers")
        self.assertTrue(is_full_deal_for_category(server, self.profile, server_cfg or {}))
        self.assertTrue(is_target_hardware(server))
        self.assertEqual(business_assessment(server)["market_access"], "target_hardware")

        self.assertEqual(storage_category, "storage")
        self.assertTrue(is_full_deal_for_category(storage, self.profile, storage_cfg or {}))
        self.assertTrue(is_target_hardware(storage))


if __name__ == "__main__":
    unittest.main()
