from __future__ import annotations

import re
from typing import Any


PRODUCT_KEYWORDS = [
    "сервер",
    "серверное оборудование",
    "вычислительный комплекс",
    "схд",
    "система хранения",
    "коммутатор",
    "маршрутизатор",
    "рабочая станция",
    "компьютер",
    "оборудование",
    "точка доступа",
    "межсетевой экран",
    "firewall",
    "vpn",
    "шлюз",
    "лиценз",
    "программ",
]

PRODUCT_REGEXES = [
    re.compile(r"(?<![а-яa-z0-9])пк(?![а-яa-z0-9])", re.IGNORECASE),
]

BAD_SUPPLY_WORDS = [
    "максимальный",
    "максимальное",
    "минимальный",
    "минимальное",
    "поддерживаемый",
    "поддерживаемые",
    "оперативной памяти",
    "памяти",
    "охлаждения",
    "процессор",
    "ядер",
    "поток",
    "частота",
    "накопител",
    "порт",
    "интерфейс",
    "слот",
    "raid",
    "питания",
    "мощность",
    "объем",
    "объём",
    "скорость",
    "установленных",
    "установленного",
    "резервирования",
]

ALLOWED_UNITS = {"шт", "штука", "ед", "комплект", "компл"}

META_CHARACTERISTIC_WORDS = [
    "порядок формирования цены",
    "место поставки",
    "место оказания услуг",
    "срок исполнения контракта",
    "условия поставки",
    "форма, сроки и порядок",
    "требования, предъявляемые к участникам",
    "информация о запрете",
    "информация и документы",
    "банковском сопровождении",
    "казначейском сопровождении",
    "документы, подтверждающие соответствие",
]

IMPORTANT_FIELDS = {
    "server": [
        "процессор",
        "ядер",
        "поток",
        "оператив",
        "ddr",
        "накопител",
        "ssd",
        "hdd",
        "nvme",
        "sas",
        "sata",
        "raid",
        "сетев",
        "ethernet",
        "sfp",
        "порт",
        "pcie",
        "ocp",
        "питани",
        "охлаждени",
        "bmc",
        "bios",
        "удаленного управления",
        "корпус",
        "rack",
        "гаранти",
        "поддержк",
    ],
    "network": [
        "порт",
        "ethernet",
        "sfp",
        "sfp+",
        "qsfp",
        "poe",
        "vlan",
        "l2",
        "l3",
        "стекир",
        "stack",
        "маршрутиз",
        "пропуск",
        "питани",
        "вентил",
        "лиценз",
        "поддержк",
    ],
    "storage": [
        "емкость",
        "ёмкость",
        "диск",
        "накопител",
        "ssd",
        "hdd",
        "контроллер",
        "cache",
        "кэш",
        "fc",
        "fibre channel",
        "iscsi",
        "nfs",
        "smb",
        "snapshot",
        "репликац",
        "дедупликац",
        "iops",
        "latency",
        "полк",
        "поддержк",
    ],
    "security": [
        "throughput",
        "пропуск",
        "vpn",
        "ips",
        "ids",
        "ngfw",
        "firewall",
        "межсетевой",
        "туннел",
        "лиценз",
        "фстэк",
        "фсб",
        "скзи",
        "ha",
        "порт",
        "поддержк",
    ],
    "software": [
        "лиценз",
        "пользовател",
        "сервер",
        "ядро",
        "срок",
        "подписк",
        "техподдержк",
        "обновлен",
        "версия",
    ],
    "services": [
        "работ",
        "услуг",
        "внедрен",
        "настройк",
        "монтаж",
        "сопровожд",
        "поддержк",
        "обслужив",
        "срок выполнения",
    ],
    "unknown": [],
}


def normalize(value: Any) -> str:
    return str(value or "").lower().replace("ё", "е")


def clean_line(line: str) -> str:
    return " ".join(str(line or "").split()).lstrip("- ").strip()


def contains_product_keyword(value: Any) -> bool:
    normalized = normalize(value)
    if any(keyword in normalized for keyword in PRODUCT_KEYWORDS):
        return True
    return any(regex.search(normalized) for regex in PRODUCT_REGEXES)


def split_table_cells(line: str) -> list[str]:
    raw = str(line or "").strip()
    if not raw:
        return []

    if "|" in raw:
        return [part.strip() for part in raw.split("|") if part.strip()]
    if "\t" in raw:
        return [part.strip() for part in raw.split("\t") if part.strip()]
    if ";" in raw:
        return [part.strip() for part in raw.split(";") if part.strip()]

    return [part.strip() for part in re.split(r"\s{2,}", raw) if part.strip()]


def is_number(value: Any) -> bool:
    value = str(value or "").replace(",", ".").strip()
    return bool(re.fullmatch(r"\d+(?:\.0+)?", value))


def table_header_indexes(parts: list[str]) -> dict[str, int]:
    indexes: dict[str, int] = {}

    for idx, part in enumerate(parts):
        normalized = normalize(part)

        if "наименование характеристики" in normalized:
            indexes.setdefault("characteristic", idx)
            continue
        if "значение характеристики" in normalized:
            indexes.setdefault("value", idx)
            continue

        if "наименование" in normalized and any(
            word in normalized
            for word in ("товар", "оборудован", "объект", "продукц", "предмет")
        ):
            indexes.setdefault("name", idx)
            continue
        if normalized in {"наименование", "товар", "оборудование"}:
            indexes.setdefault("name", idx)
            continue

        if "характеристик" in normalized or "показател" in normalized:
            indexes.setdefault("characteristic", idx)
            continue
        if normalized in {"значение", "требуемое значение"} or "значение показател" in normalized:
            indexes.setdefault("value", idx)
            continue
        if "количество" in normalized or "кол-во" in normalized or "кол во" in normalized:
            indexes.setdefault("quantity", idx)
            continue
        if "ед. изм" in normalized or "ед изм" in normalized or "единиц" in normalized:
            indexes.setdefault("unit", idx)
            continue
        if "цена" in normalized or "стоимость" in normalized:
            indexes.setdefault("price", idx)

    return indexes


def cell_at(parts: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(parts):
        return ""
    return clean_line(parts[idx])


def parse_pipe_row(line: str) -> tuple[str, str, str] | None:
    line = clean_line(line)

    parts = split_table_cells(line)
    if len(parts) < 2:
        return None

    norm = normalize(line)
    if "наименование характеристики" in norm:
        return None
    if "значение характеристики" in norm:
        return None
    if "единица измерения характеристики" in norm:
        return None
    if "источник ценовой информации" in norm:
        return None

    if re.fullmatch(r"\d+[\.\)]?", parts[0]) and len(parts) >= 3:
        name = parts[1]
        value = parts[2]
        unit = parts[3] if len(parts) >= 4 else ""
    else:
        name = parts[0]
        value = parts[1]
        unit = parts[2] if len(parts) >= 3 else ""

    if len(name) < 3:
        return None

    return name, value, unit


def detect_category(*, tender_title: str, documents: list[dict[str, Any]]) -> str:
    title = normalize(tender_title)

    title_patterns = {
        "storage": [
            "схд",
            "система хранения",
            "система хранения данных",
            "дисковая полка",
        ],
        "security": [
            "межсетевой экран",
            "firewall",
            "ngfw",
            "шлюз безопасности",
            "vpn-шлюз",
            "vpn шлюз",
        ],
        "network": [
            "коммутатор",
            "маршрутизатор",
            "точка доступа",
            "сетев",  # сетевое / сетевого / сетевые
        ],
        "server": [
            "сервер",
            "вычислительный комплекс",
        ],
        "software": [
            "лицензия",
            "лицензии",
            "программное обеспечение",
        ],
        "services": [
            "услуги сопровождения",
            "услуги технической поддержки",
            "работы по настройке",
            "работы по монтажу",
        ],
    }

    for category, patterns in title_patterns.items():
        if any(pattern in title for pattern in patterns):
            return category

    joined = "\n".join(
        [title]
        + [normalize(doc.get("filename")) for doc in documents]
        + [normalize(doc.get("text"))[:25000] for doc in documents]
    )

    keyword_groups = {
        "storage": ["схд", "система хранения", "storage", "дисковая полка", "san", "nas", "iscsi"],
        "security": ["межсетевой экран", "firewall", "ngfw", "vpn", "фстэк", "фсб", "скзи", "utm"],
        "network": ["коммутатор", "маршрутизатор", "switch", "router", "poe", "vlan", "stacking", "qsfp", "сетев"],
        "server": ["сервер", "процессор", "оперативной памяти", "ddr", "bios", "bmc", "raid", "стоечный"],
        "software": ["лиценз", "подписк", "версия", "обновлен"],
        # services намеренно не скорим по общим словам "настройка/монтаж/обслуживание",
        # потому что они часто встречаются в поставках оборудования и ломают категорию.
        "services": [],
    }

    scores = {
        category: sum(joined.count(word) for word in words)
        for category, words in keyword_groups.items()
    }

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def parse_ktru_supply_row(line: str) -> dict[str, str] | None:
    parts = split_table_cells(line)
    if len(parts) < 7:
        return None

    # Частый формат из структурированных ТЗ:
    # 1 | 26.20.14.100 | Сервер видеонаблюдения в комплекте | Сервер. Количество | ≥ 3 | Шт. | Комплект | 3
    if not re.fullmatch(r"\d+[\.\)]?", parts[0]):
        return None
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d{3}", parts[1]):
        return None

    name = parts[2]
    unit = parts[-2]
    quantity = parts[-1]

    name_norm = normalize(name)
    if any(bad in name_norm for bad in BAD_SUPPLY_WORDS):
        return None
    if not contains_product_keyword(name_norm):
        return None

    unit_norm = normalize(unit).replace(".", "").strip()
    if unit_norm not in ALLOWED_UNITS:
        return None

    if not is_number(quantity):
        return None

    return {
        "name": name,
        "unit": unit,
        "quantity": quantity,
    }


def category_from_supply_items(supply_items: list[dict[str, str]]) -> str | None:
    joined = " ".join(normalize(item.get("name")) for item in supply_items)

    if any(word in joined for word in ["сервер", "вычислительный комплекс"]):
        return "server"
    if any(word in joined for word in ["коммутатор", "маршрутизатор", "точка доступа", "сетев"]):
        return "network"
    if any(word in joined for word in ["схд", "система хранения", "дисковая полка"]):
        return "storage"
    if any(word in joined for word in ["межсетевой экран", "firewall", "ngfw", "vpn", "шлюз"]):
        return "security"
    if any(word in joined for word in ["лиценз", "программ"]):
        return "software"

    return None


def parse_supply_item_by_header(
    parts: list[str],
    header: dict[str, int],
) -> dict[str, str] | None:
    if "name" not in header or "quantity" not in header:
        return None

    name = cell_at(parts, header.get("name"))
    quantity = cell_at(parts, header.get("quantity"))
    unit = cell_at(parts, header.get("unit"))

    if not name or len(name) < 3:
        return None
    if any(bad in normalize(name) for bad in BAD_SUPPLY_WORDS):
        return None
    if not contains_product_keyword(name):
        return None
    if not is_number(quantity):
        return None

    unit_norm = normalize(unit).replace(".", "").strip()
    if unit and unit_norm not in ALLOWED_UNITS:
        return None

    return {
        "name": name,
        "unit": unit or "",
        "quantity": quantity,
    }


def extract_supply_items(documents: list[dict[str, Any]], limit: int = 20) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for doc in documents:
        text = doc.get("text") or ""
        current_header: dict[str, int] | None = None

        for raw_line in text.splitlines():
            parts = split_table_cells(raw_line)
            line = clean_line(raw_line)
            header = table_header_indexes(parts)
            if header.get("name") is not None and (
                header.get("quantity") is not None or header.get("characteristic") is not None
            ):
                current_header = header
                continue

            if current_header:
                header_item = parse_supply_item_by_header(parts, current_header)
                if header_item:
                    key = (
                        header_item["name"],
                        header_item.get("unit") or "",
                        header_item["quantity"],
                    )
                    if key not in seen:
                        result.append(
                            {
                                **header_item,
                                "source_document": doc.get("filename") or "",
                            }
                        )
                        seen.add(key)

                        if len(result) >= limit:
                            return result

                    continue

            ktru_item = parse_ktru_supply_row(line)
            if ktru_item:
                key = (ktru_item["name"], ktru_item["unit"], ktru_item["quantity"])
                if key not in seen:
                    result.append(
                        {
                            **ktru_item,
                            "source_document": doc.get("filename") or "",
                        }
                    )
                    seen.add(key)

                    if len(result) >= limit:
                        return result

                continue

            parsed = parse_pipe_row(line)
            if not parsed:
                continue

            name, unit, quantity = parsed
            name_norm = normalize(name)
            unit_norm = normalize(unit).replace(".", "").strip()

            if any(bad in name_norm for bad in BAD_SUPPLY_WORDS):
                continue

            if not contains_product_keyword(name_norm):
                continue

            if unit_norm not in ALLOWED_UNITS:
                continue

            if not is_number(quantity):
                continue

            key = (name, unit, quantity)
            if key in seen:
                continue

            result.append(
                {
                    "name": name,
                    "unit": unit,
                    "quantity": quantity,
                    "source_document": doc.get("filename") or "",
                }
            )
            seen.add(key)

            if len(result) >= limit:
                return result

    return result



def clean_ktru_characteristic_name(raw_name: str) -> str:
    name = clean_line(raw_name)

    if ". " in name:
        prefix, rest = name.split(". ", 1)
        prefix_norm = normalize(prefix)

        if prefix_norm in {
            "сервер",
            "жесткий диск",
            "накопитель",
            "коммутатор",
            "маршрутизатор",
            "схд",
            "контроллер",
        }:
            return rest.strip()

    return name


def parse_ktru_characteristic_row(line: str) -> tuple[str, str, str] | None:
    parts = split_table_cells(line)

    if len(parts) < 5:
        return None

    # Формат:
    # 1 | 26.20.14.100 | Сервер видеонаблюдения в комплекте | Сервер. Процессор | Intel ... | ...
    if re.fullmatch(r"\d+[\.\)]?", parts[0]) and len(parts) >= 5:
        maybe_code = parts[1]
        raw_name = parts[3]
        value = parts[4]
        unit = parts[5] if len(parts) >= 6 else ""
    elif re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d{3}", parts[0]) and len(parts) >= 4:
        maybe_code = parts[0]
        raw_name = parts[2]
        value = parts[3]
        unit = parts[4] if len(parts) >= 5 else ""
    else:
        return None

    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}\.\d{3}", maybe_code):
        return None

    name = clean_ktru_characteristic_name(raw_name)
    name_norm = normalize(name)
    value_norm = normalize(value)

    # Это строка количества предмета поставки, а не техническая характеристика.
    if name_norm == "количество":
        return None

    # Пустые или служебные значения не несут пользы для пресейла.
    if not value_norm or value_norm in {"-", "не указано", "нет данных"}:
        return None

    return name, value, unit



def parse_product_characteristic_row(line: str) -> tuple[str, str, str] | None:
    parts = split_table_cells(line)
    if len(parts) < 3:
        return None

    # Форматы:
    # Коммутатор ... | Количество LAN портов | не менее 24 | шт
    # 4 | Коммутатор ... | Количество LAN портов | не менее 24 | шт
    if re.fullmatch(r"\d+[\.\)]?", parts[0]) and len(parts) >= 4:
        product_name = parts[1]
        characteristic = parts[2]
        value = parts[3]
        unit = parts[4] if len(parts) >= 5 else ""
    else:
        product_name = parts[0]
        characteristic = parts[1]
        value = parts[2]
        unit = parts[3] if len(parts) >= 4 else ""

    product_norm = normalize(product_name)
    characteristic_norm = normalize(characteristic)
    value_norm = normalize(value)

    if not contains_product_keyword(product_norm):
        return None

    # Это строка предмета поставки вида: товар | штука | 6
    characteristic_as_unit = characteristic_norm.replace(".", "").strip()
    if characteristic_as_unit in ALLOWED_UNITS and is_number(value):
        return None

    if characteristic_norm in {
        "наименование",
        "наименование товара",
        "ед. изм.",
        "единица измерения",
        "кол-во",
        "количество",
    }:
        return None

    if "или эквивалент" in characteristic_norm and is_number(value):
        return None

    if not value_norm or value_norm in {"-", "не указано", "нет данных"}:
        return None

    return f"{product_name} - {characteristic}", value, unit


def parse_characteristic_by_header(
    parts: list[str],
    header: dict[str, int],
) -> tuple[str, str, str] | None:
    if "characteristic" not in header or "value" not in header:
        return None

    characteristic = cell_at(parts, header.get("characteristic"))
    value = cell_at(parts, header.get("value"))
    unit = cell_at(parts, header.get("unit"))
    product_name = cell_at(parts, header.get("name"))
    if header.get("quantity") is not None and header.get("unit") is not None:
        unit = ""

    if not characteristic or not value:
        return None

    characteristic_norm = normalize(characteristic)
    if characteristic_norm in {
        "характеристика",
        "наименование характеристики",
        "показатель",
    }:
        return None

    if product_name and contains_product_keyword(product_name):
        characteristic = f"{product_name} - {characteristic}"

    return characteristic, value, unit


def extract_characteristics(
    documents: list[dict[str, Any]],
    *,
    category: str,
    limit: int = 80,
) -> list[dict[str, str]]:
    important = IMPORTANT_FIELDS.get(category) or []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for doc in documents:
        text = doc.get("text") or ""
        current_header: dict[str, int] | None = None

        for raw_line in text.splitlines():
            parts = split_table_cells(raw_line)
            line = clean_line(raw_line)
            header = table_header_indexes(parts)
            if header.get("characteristic") is not None and header.get("value") is not None:
                current_header = header
                continue

            header_parsed = (
                parse_characteristic_by_header(parts, current_header)
                if current_header
                else None
            )
            product_parsed = parse_product_characteristic_row(line)
            ktru_parsed = parse_ktru_characteristic_row(line)
            parsed = header_parsed or product_parsed or ktru_parsed or parse_pipe_row(line)
            if not parsed:
                continue

            name, value, unit = parsed
            name_norm = normalize(name)
            value_norm = normalize(value)
            full_norm = normalize(f"{name} {value} {unit}")

            if any(meta in full_norm for meta in META_CHARACTERISTIC_WORDS):
                continue

            if "закупка товара осуществляется" in full_norm:
                continue
            if "антитеррористической защищенности" in full_norm:
                continue
            if "пропускного режима" in full_norm:
                continue
            if "гарантийных обязательств" in full_norm:
                continue
            if "обеспечение заявки" in full_norm:
                continue
            if "обеспечения заявки" in full_norm:
                continue
            if "обеспечение исполнения" in full_norm:
                continue
            if "обеспечения исполнения" in full_norm:
                continue
            if value_norm == "установлено" and ("44-фз" in full_norm or "обеспеч" in full_norm):
                continue

            if "наименование объекта закупки" in full_norm:
                continue
            if "требования к участникам" in full_norm:
                continue
            if "обеспечение заявки" in full_norm:
                continue
            if "обеспечение исполнения" in full_norm:
                continue
            if "порядок предоставления обеспечения" in full_norm:
                continue
            if "порядок внесения денежных средств" in full_norm:
                continue

            # Если это обычная строка, где name является товаром, не считаем её характеристикой.
            # Для KTRU и product-characteristic строк название уже очищено или преобразовано.
            if not ktru_parsed and not product_parsed and not header_parsed and contains_product_keyword(name_norm):
                continue

            if not value_norm or value_norm in {"-", "не указано", "нет данных"}:
                continue

            # Важные технические признаки ищем в названии характеристики,
            # а не во всей строке. Иначе мета-строки извещения могут пролезать
            # через слова в описании, значении или контексте закупки.
            signal_norm = name_norm

            # Обычные строки без product/KTRU-структуры не должны иметь длинное
            # описательное название. Это отсекает пункты извещения: цель закупки,
            # порядок оплаты, обеспечение, требования к участникам и т.п.
            if not product_parsed and not ktru_parsed and not header_parsed and len(name) > 180:
                continue

            if important and not any(keyword in signal_norm for keyword in important):
                continue

            key = (name, value, unit)
            if key in seen:
                continue

            result.append(
                {
                    "name": name,
                    "value": value,
                    "unit": unit,
                    "source_document": doc.get("filename") or "",
                }
            )
            seen.add(key)

            if len(result) >= limit:
                return result

    return result


def build_spec_facts(
    *,
    tender_title: str,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    detected_category = detect_category(tender_title=tender_title, documents=documents)
    supply_items = extract_supply_items(documents)

    supply_category = category_from_supply_items(supply_items)
    category = supply_category or detected_category

    characteristics = extract_characteristics(documents, category=category)

    return {
        "category": category,
        "detected_category_from_text": detected_category,
        "category_from_supply_items": supply_category,
        "supply_items": supply_items,
        "technical_characteristics": characteristics,
    }


def render_spec_facts_markdown(spec_facts: dict[str, Any]) -> str:
    lines = [
        "## Приоритетная техническая выжимка из ТЗ",
        "",
        "Этот блок автоматически собран из таблиц ТЗ. Используй его как источник фактов, но не подменяй им анализ рисков.",
        "",
        f"Категория: {spec_facts.get('category') or 'unknown'}",
        "",
    ]

    supply_items = spec_facts.get("supply_items") or []
    if supply_items:
        lines.extend(["### Предмет поставки", ""])
        for item in supply_items:
            lines.append(
                f"- {item.get('name')}; ед. изм.: {item.get('unit')}; количество: {item.get('quantity')}"
            )
        lines.append("")

    characteristics = spec_facts.get("technical_characteristics") or []
    if characteristics:
        lines.extend(["### Ключевые технические характеристики", ""])
        for item in characteristics:
            unit = item.get("unit")
            if unit:
                lines.append(f"- {item.get('name')}: {item.get('value')} {unit}")
            else:
                lines.append(f"- {item.get('name')}: {item.get('value')}")
        lines.append("")

    return "\n".join(lines).strip()
