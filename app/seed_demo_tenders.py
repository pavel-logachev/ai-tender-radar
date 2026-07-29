from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from app.config import settings


def main() -> None:
    now = datetime.now(timezone.utc)

    tenders = [
        {
            "source": "demo",
            "external_id": "demo-001",
            "title": "Поставка серверного оборудования с настройкой виртуализации",
            "customer_name": "ГБУ Информационные системы",
            "initial_price": 18500000,
            "law": "44-ФЗ",
            "procedure_type": "Электронный аукцион",
            "region": "Москва",
            "published_at": now,
            "deadline_at": now + timedelta(days=9),
            "url": "https://example.com/tender/demo-001",
            "raw": {
                "description": "Серверы, виртуализация, пусконаладочные работы, настройка и сопровождение."
            },
        },
        {
            "source": "demo",
            "external_id": "demo-002",
            "title": "Поставка картриджей для офисной техники",
            "customer_name": "Муниципальное учреждение",
            "initial_price": 700000,
            "law": "44-ФЗ",
            "procedure_type": "Запрос котировок",
            "region": "Москва",
            "published_at": now,
            "deadline_at": now + timedelta(days=6),
            "url": "https://example.com/tender/demo-002",
            "raw": {
                "description": "Картриджи, расходные материалы, офисная техника."
            },
        },
        {
            "source": "demo",
            "external_id": "demo-003",
            "title": "Внедрение системы резервного копирования и мониторинга ИТ-инфраструктуры",
            "customer_name": "АО Промышленная компания",
            "initial_price": 32000000,
            "law": "223-ФЗ",
            "procedure_type": "Конкурс",
            "region": "Московская область",
            "published_at": now,
            "deadline_at": now + timedelta(days=14),
            "url": "https://example.com/tender/demo-003",
            "raw": {
                "description": "Внедрение, настройка, резервное копирование, мониторинг, сопровождение."
            },
        },
        {
            "source": "demo",
            "external_id": "demo-004",
            "title": "Поставка мебели для административного здания",
            "customer_name": "ФГБУ",
            "initial_price": 4500000,
            "law": "44-ФЗ",
            "procedure_type": "Электронный аукцион",
            "region": "Центральный федеральный округ",
            "published_at": now,
            "deadline_at": now + timedelta(days=8),
            "url": "https://example.com/tender/demo-004",
            "raw": {
                "description": "Офисная мебель, столы, шкафы, кресла."
            },
        },
        {
            "source": "demo",
            "external_id": "demo-005",
            "title": "Поставка межсетевого экрана и VPN-шлюза с настройкой",
            "customer_name": "ГАУ Центр цифровизации",
            "initial_price": 9500000,
            "law": "44-ФЗ",
            "procedure_type": "Электронный аукцион",
            "region": "вся Россия",
            "published_at": now,
            "deadline_at": now + timedelta(days=2),
            "url": "https://example.com/tender/demo-005",
            "raw": {
                "description": "Информационная безопасность, VPN, межсетевой экран, настройка."
            },
        },
    ]

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            for t in tenders:
                cur.execute(
                    """
                    INSERT INTO tenders (
                        source, external_id, title, customer_name, initial_price,
                        law, procedure_type, region, published_at, deadline_at,
                        url, raw, status
                    )
                    VALUES (
                        %(source)s, %(external_id)s, %(title)s, %(customer_name)s,
                        %(initial_price)s, %(law)s, %(procedure_type)s, %(region)s,
                        %(published_at)s, %(deadline_at)s, %(url)s, %(raw)s, 'new'
                    )
                    ON CONFLICT (source, external_id)
                    DO UPDATE SET
                        title = EXCLUDED.title,
                        customer_name = EXCLUDED.customer_name,
                        initial_price = EXCLUDED.initial_price,
                        law = EXCLUDED.law,
                        procedure_type = EXCLUDED.procedure_type,
                        region = EXCLUDED.region,
                        published_at = EXCLUDED.published_at,
                        deadline_at = EXCLUDED.deadline_at,
                        url = EXCLUDED.url,
                        raw = EXCLUDED.raw,
                        status = 'new',
                        updated_at = now();
                    """,
                    {**t, "raw": Jsonb(t["raw"])},
                )
            conn.commit()

    print(f"Seeded {len(tenders)} demo tenders")


if __name__ == "__main__":
    main()
