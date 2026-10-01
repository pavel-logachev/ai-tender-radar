"""User's business is server and storage supplies, not generic software IT."""
from __future__ import annotations
import unittest


class HardwareProfileTest(unittest.TestCase):
    def test_any_brand_server_storage_supplies_but_not_software_maintenance_or_furniture(self):
        from agent_radar.hardware_profile import hardware_signal, budget_decision
        for title,description in (
            ("Поставка серверов любого производителя",""),
            ("Поставка СХД",""),("Дисковый массив SAN","Закупка"),
            ("Поставка оборудования","Dell PowerEdge R760; HPE ProLiant"),
            ("Поставка серверного оборудования и внедрение",""),
            ("Расширение системы хранения данных","Поставка контроллеров и дисковых полок")):
            with self.subTest(title=title):self.assertIsNotNone(hardware_signal({"title":title,"description":description}))
        for title,description in (
            ("Интеграция 1С:ERP и ELMA",""),("Закупка лицензий SQL Server",""),
            ("Обслуживание серверов","Техническая поддержка без поставки оборудования"),
            ("Поставка мебели в серверную","Шкафы и столы"),
            ("Разработка серверной части приложения",""),
            ("Поставка ПО","SQL Server 2022"),
            ("Поставка оборудования","Виртуальный сервер в облаке"),
            ("Поставка сетевого оборудования","Aquarius коммутатор"),
            ("Аренда выделенного GPU-сервера для задач искусственного интеллекта","GPU сервер"),
            ("Конкурс на сервисные выезды по ТСБ/СКС","Сервисные работы в серверной, оборудование"),
            ("Выбор исполнителя на поставку и настройку программного обеспечения для контактного центра","Сервер в текущей инфраструктуре заказчика")):
            with self.subTest(title=title):self.assertIsNone(hardware_signal({"title":title,"description":description}))
        self.assertEqual(budget_decision({"amount":5000000,"currency":"RUB","basis":"procedure_total"})["decision"],"include")
        self.assertEqual(budget_decision({"amount":4999999,"currency":"RUB","basis":"procedure_total"})["decision"],"below_threshold")
        self.assertEqual(budget_decision(None)["decision"],"unknown_include")
        self.assertEqual(budget_decision({"amount":10,"currency":"RUB","basis":"unit_price"})["decision"],"unknown_include")
        self.assertEqual(budget_decision({"amount":100000,"currency":"USD","basis":"procedure_total"})["decision"],"unknown_include")


if __name__=="__main__":unittest.main()
