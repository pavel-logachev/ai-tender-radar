from __future__ import annotations

import re
from typing import Any


TECHNICAL_SECTION_FOUND_REASON = "technical_section_found_inside_document"
MAX_TECHNICAL_SECTION_CHARS = 180000

STRONG_TECHNICAL_SECTION_SIGNALS = (
    "техническое задание",
    "технические требования",
    "описание объекта закупки",
    "техническая спецификация",
    "техническая часть",
)

WEAK_TECHNICAL_SECTION_SIGNALS = (
    "спецификация",
    "перечень оборудования",
    "перечень товара",
    "перечень товаров",
    "ведомость",
)

TECHNICAL_SECTION_START_SIGNALS = tuple(
    dict.fromkeys(
        STRONG_TECHNICAL_SECTION_SIGNALS
        + WEAK_TECHNICAL_SECTION_SIGNALS
        + (
            "ведомость оборудования",
            "описание предмета закупки",
            "наименование и характеристики товара",
            "наименование товара",
            "наименование оборудования",
        )
    )
)

TECHNICAL_SECTION_END_SIGNALS = (
    "проект договора",
    "проект контракта",
    "требования к заявке",
    "требования к содержанию заявки",
    "инструкция участнику",
    "инструкция для участника",
    "порядок подачи заявок",
    "порядок подачи заявок на участие",
    "критерии оценки",
    "обоснование нмцк",
    "обоснование нмцд",
    "форма заявки",
    "банковское сопровождение",
    "ответственность сторон",
    "порядок оплаты",
    "порядок приемки",
    "порядок приёмки",
)

NOMENCLATURE_NAME_SIGNALS = (
    "наименование товара",
    "наименование оборудования",
    "наименование поставляемого товара",
    "наименование объекта закупки",
    "наименование",
)

QUANTITY_SIGNALS = (
    "количество",
    "кол-во",
    "кол во",
    "ед. изм",
    "ед изм",
    "единица измерения",
    "единицы измерения",
    "шт",
)

CHARACTERISTIC_SIGNALS = (
    "характеристика",
    "значение характеристики",
    "технические характеристики",
    "показатель",
    "значение показателя",
)

PRICE_SIGNALS = (
    "цена",
    "предельная цена",
    "стоимость",
    "сумма",
    "нмцк",
    "нмцд",
    "обоснование цены",
)

TABLE_HEADER_NAME_SIGNALS = (
    "наименование и характеристики",
    "наименование товара",
    "наименование оборудования",
    "наименование поставляемого товара",
    "наименование продукции",
)

PRODUCT_SIGNALS = (
    "сервер",
    "серверное оборудование",
    "система хранения данных",
    "схд",
    "полка расширения",
    "дисковая полка",
    "накопитель",
    "твердотельный накопитель",
    "жесткий диск",
    "коммутатор",
    "маршрутизатор",
    "контроллер",
    "модуль",
    "рабочая станция",
    "компьютер",
    "оборудование",
    "ssd",
    "hdd",
)

PRODUCT_REGEX_SIGNALS = (
    re.compile(r"(?<![а-яa-z0-9])пк(?![а-яa-z0-9])", re.IGNORECASE),
)

PRICE_DOC_FILENAME_SIGNALS = (
    "нмцк",
    "нмцд",
    "обоснование",
    "расчет цены",
    "расчёт цены",
)

PROCUREMENT_DOC_FILENAME_SIGNALS = (
    "кд",
    "конкурсная документация",
    "закупочная документация",
    "документация",
    "приложение",
    "материалы закупки",
    "материалы",
)

EXCEL_FILENAME_SIGNALS = (".xls", ".xlsx", ".xlsm", ".csv")

SPEC_REFERENCE_REGEXES = (
    re.compile(r"согласно\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"определя(?:ет|ют|ется|ются)\s+в\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"указан[аоы]?\s+в\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"приложени[ея]\s*№?\s*1\s+к\s+договор", re.IGNORECASE),
    re.compile(r"спецификаци[ия]\s+является\s+неотъемлем", re.IGNORECASE),
    re.compile(r"условия\s+поставки\s+по\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"цен[аы]\s+.*\s+в\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"количеств[оа]\s+.*\s+в\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"адрес[аы]?\s+.*\s+в\s+спецификаци[ия]", re.IGNORECASE),
    re.compile(r"спецификаци[ия]\s*\(приложени[ея]\s*№?\s*1", re.IGNORECASE),
)

REFERENCE_ONLY_WORDS = (
    "согласно",
    "определяется",
    "определяются",
    "указано",
    "указана",
    "указаны",
    "является",
    "неотъемлемой частью",
    "к договору",
)

NEGATIVE_TECHNICAL_SECTION_CONTEXTS = (
    "техническое задание отсутств",
    "техническое задание не найден",
    "тз отсутств",
    "тз не найден",
    "спецификация отсутств",
    "спецификация не найден",
)

TECHNICAL_DETAIL_SIGNALS = (
    "артикул",
    "модель",
    "процессор",
    "ядр",
    "поток",
    "оператив",
    "ram",
    "память",
    "ddr",
    "накопител",
    "ssd",
    "hdd",
    "nvme",
    "raid",
    "порт",
    "ethernet",
    "sfp",
    "гб",
    "тб",
    "частот",
    "производительност",
    "пропускн",
    "гарант",
)

UNIT_REGEX = re.compile(
    r"(?<![а-яa-z0-9])(?:шт|ед|комплект|компл)\.?(?![а-яa-z0-9])",
    re.IGNORECASE,
)
NUMBER_REGEX = re.compile(r"(?<![a-zа-я0-9])\d+(?:[,.]\d+)?(?![a-zа-я0-9])", re.IGNORECASE)
MONEY_VALUE_REGEX = re.compile(
    r"(?<![a-zа-я0-9])\d{1,3}(?:[\s\u00a0]\d{3})+(?:[,.]\d{2})?(?![a-zа-я0-9])"
    r"|(?<![a-zа-я0-9])\d+[,.]\d{2}(?![a-zа-я0-9])",
    re.IGNORECASE,
)
MODEL_TOKEN_REGEX = re.compile(
    r"(?<![a-zа-я0-9])(?:[a-z]{1,8}[-_/]?\d{2,}[a-z0-9-_/]*|\d{2,}[a-z]{1,8}[a-z0-9-_/]*)(?![a-zа-я0-9])",
    re.IGNORECASE,
)
TECHNICAL_APPENDIX_HEADING_REGEX = re.compile(
    r"^\s*приложени[ея]\s*(?:№|n|no)?\s*\d+(?:[\.)])?\s+"
    r"(?:к\s+)?(?:техническому\s+заданию|техническим\s+требованиям|описанию\s+объекта\s+закупки|описанию\s+предмета\s+закупки|тз)\b",
    re.IGNORECASE,
)
PLACEHOLDER_ROW_SIGNALS = (
    "заполняется",
    "будет заполн",
    "для заполнения",
    "указать",
    "указывается",
    "не заполняется",
    "при наличии",
)


def normalize_text(value: Any) -> str:
    return str(value or "").casefold().replace("ё", "е")


def _clean_line(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _contains_any(corpus: str, signals: tuple[str, ...]) -> list[str]:
    return [signal for signal in signals if signal in corpus]


def _contains_product_signal(corpus: str) -> list[str]:
    matched = _contains_any(corpus, PRODUCT_SIGNALS)
    if any(regex.search(corpus) for regex in PRODUCT_REGEX_SIGNALS):
        matched.append("пк")
    return list(dict.fromkeys(matched))


def _split_table_cells(line: str) -> list[str]:
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


def _line_is_spec_reference(line: str) -> bool:
    normalized = normalize_text(line)
    if any(regex.search(normalized) for regex in SPEC_REFERENCE_REGEXES):
        return True
    return "спецификаци" in normalized and any(
        word in normalized for word in REFERENCE_ONLY_WORDS
    )


def _find_spec_references(text: str, limit: int = 8) -> list[str]:
    references: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        if _line_is_spec_reference(line):
            references.append(line[:260])
            if len(references) >= limit:
                return references
    return references


def _line_has_table_header(line: str) -> bool:
    normalized = normalize_text(line)
    parts = _split_table_cells(line)
    has_table_shape = len(parts) >= 3 or "|" in line or "\t" in line
    has_name = any(signal in normalized for signal in TABLE_HEADER_NAME_SIGNALS) or (
        "наименование" in normalized
        and any(word in normalized for word in ("товар", "оборудован", "характеристик"))
    )
    has_quantity_or_price = bool(
        _contains_any(normalized, QUANTITY_SIGNALS)
        or _contains_any(normalized, PRICE_SIGNALS)
    )
    has_characteristic = bool(_contains_any(normalized, CHARACTERISTIC_SIGNALS))
    return has_table_shape and has_name and (has_quantity_or_price or has_characteristic)


def _line_has_quantity_value(line: str) -> bool:
    normalized = normalize_text(line)
    parts = _split_table_cells(line)

    if re.search(r"\d+(?:[,.]\d+)?\s*(?:шт|ед|комплект|компл)\.?", normalized):
        return True

    has_number = any(NUMBER_REGEX.search(part) for part in parts) or bool(
        NUMBER_REGEX.search(normalized)
    )
    has_unit = any(UNIT_REGEX.search(part) for part in parts) or bool(
        UNIT_REGEX.search(normalized)
    )
    return has_number and has_unit


def _line_has_quantity_cell(line: str) -> bool:
    parts = _split_table_cells(line)
    for part in (parts[1:] if len(parts) > 1 else parts):
        normalized = normalize_text(part).strip(" .,:;")
        if re.fullmatch(r"\d{1,5}(?:[,.]\d+)?", normalized):
            return True
        if re.search(r"\d+(?:[,.]\d+)?\s*(?:шт|ед|комплект|компл)\.?", normalized):
            return True
    return False


def _line_has_price_value(line: str) -> bool:
    normalized = normalize_text(line)
    return bool(MONEY_VALUE_REGEX.search(normalized))


def _line_has_model_or_article(line: str) -> bool:
    normalized = normalize_text(line)
    return bool(
        "артикул" in normalized
        or "модель" in normalized
        or MODEL_TOKEN_REGEX.search(normalized)
    )


def _line_is_placeholder_row(line: str) -> bool:
    normalized = normalize_text(line)
    return any(signal in normalized for signal in PLACEHOLDER_ROW_SIGNALS)


def _line_has_technical_detail(line: str) -> bool:
    normalized = normalize_text(line)
    if not any(signal in normalized for signal in TECHNICAL_DETAIL_SIGNALS):
        return False
    return bool(
        NUMBER_REGEX.search(normalized)
        or "не менее" in normalized
        or "не хуже" in normalized
        or "миним" in normalized
        or ">=" in normalized
    )


def _line_has_real_product_row(line: str, *, table_context: bool = False) -> bool:
    normalized = normalize_text(line)
    parts = _split_table_cells(line)
    has_table_shape = len(parts) >= 3 or "|" in line or "\t" in line
    if not normalized or not has_table_shape:
        return False
    if _line_is_spec_reference(normalized) or _line_has_table_header(line):
        return False
    if _line_is_placeholder_row(normalized):
        return False

    has_name_cell = any(
        len(normalize_text(part)) >= 4 and re.search(r"[a-zа-я]", normalize_text(part))
        for part in parts[:2]
    )
    has_quantity = _line_has_quantity_value(line) or _line_has_quantity_cell(line)
    has_price = _line_has_price_value(line)
    has_model_or_article = _line_has_model_or_article(line)
    has_product_signal = bool(_contains_product_signal(normalized))
    has_detail = _line_has_technical_detail(line)

    evidence_score = sum(
        bool(value)
        for value in (
            has_name_cell,
            has_quantity,
            has_price,
            has_model_or_article,
            has_product_signal,
            has_detail,
        )
    )
    if table_context:
        return has_name_cell and evidence_score >= 3 and (
            has_quantity or has_price or has_model_or_article or has_detail
        )
    return evidence_score >= 4 and (
        has_product_signal or has_model_or_article or has_detail
    )


def _line_has_filled_item(line: str, *, table_context: bool = False) -> bool:
    normalized = normalize_text(line)
    if not normalized:
        return False
    if _line_is_spec_reference(normalized):
        return False
    if _line_has_table_header(line):
        return False
    if _line_has_real_product_row(line, table_context=table_context):
        return True
    if not _contains_product_signal(normalized):
        return False
    return (
        _line_has_quantity_value(line)
        or _line_has_quantity_cell(line)
        or _line_has_technical_detail(line)
    )


def _find_table_evidence(text: str, *, max_rows_after_header: int = 35) -> dict[str, Any]:
    raw_lines = list(text.splitlines())
    headers: list[str] = []
    filled_rows: list[str] = []

    for index, raw_line in enumerate(raw_lines):
        line = _clean_line(raw_line)
        if not line:
            continue
        if not _line_has_table_header(raw_line):
            continue

        headers.append(line[:260])
        for raw_row in raw_lines[index + 1 : index + 1 + max_rows_after_header]:
            row = _clean_line(raw_row)
            if not row:
                continue
            if _line_has_table_header(raw_row):
                break
            if _line_has_filled_item(raw_row, table_context=True):
                filled_rows.append(row[:260])
                if len(filled_rows) >= 6:
                    break

    return {
        "headers": headers[:6],
        "filled_rows": filled_rows[:6],
        "has_filled_table": bool(filled_rows),
        "blank_template": bool(headers) and not filled_rows,
    }


def _find_filled_item_lines(text: str, limit: int = 8) -> list[str]:
    rows: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if _line_has_filled_item(line):
            rows.append(line[:260])
            if len(rows) >= limit:
                return rows
    return rows


def _line_offsets(text: str) -> list[dict[str, Any]]:
    offsets: list[dict[str, Any]] = []
    cursor = 0
    for raw_line in str(text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        start = cursor
        end = cursor + len(line)
        offsets.append({"line": line, "start": start, "end": end})
        cursor += len(raw_line)

    if text and not offsets:
        offsets.append({"line": text, "start": 0, "end": len(text)})

    return offsets


def _technical_section_title_from_line(line: str) -> str | None:
    clean = _clean_line(line)
    if not clean:
        return None
    if _line_is_spec_reference(clean):
        return None
    if TECHNICAL_APPENDIX_HEADING_REGEX.search(normalize_text(clean)):
        return clean[:180]

    for signal in TECHNICAL_SECTION_START_SIGNALS:
        if _line_looks_like_section_heading(clean, signal):
            return clean[:180]

    if _line_has_table_header(clean):
        return clean[:180]

    return None


def _line_is_numbered_or_named_section_heading(line: str) -> bool:
    normalized = normalize_text(line)
    if not normalized:
        return False
    return bool(
        re.match(r"^\s*раздел\s+\d+(?:[\.\)]|\s)", normalized)
        or re.match(r"^\s*приложение\s*№?\s*\d+(?:[\.\)]|\s|$)", normalized)
    )


def _line_looks_like_section_end(line: str) -> bool:
    clean = _clean_line(line)
    if not clean:
        return False

    normalized = normalize_text(clean)
    if _technical_section_title_from_line(clean):
        return False
    if _line_has_filled_item(clean):
        return False

    if _line_is_numbered_or_named_section_heading(clean):
        return True

    if any(signal in normalized for signal in TECHNICAL_SECTION_END_SIGNALS):
        return len(normalized) <= 180 or _line_is_numbered_or_named_section_heading(clean)

    return False


def _technical_section_end_offset(
    text: str,
    line_offsets: list[dict[str, Any]],
    start_index: int,
    *,
    max_section_chars: int,
) -> tuple[int, str]:
    start_offset = int(line_offsets[start_index]["start"])
    limit_offset = min(len(text), start_offset + max_section_chars)

    for item in line_offsets[start_index + 1 :]:
        item_start = int(item["start"])
        if item_start >= limit_offset:
            return limit_offset, "char_limit"
        if _line_looks_like_section_end(str(item["line"])):
            return item_start, "next_section_heading"

    if limit_offset < len(text):
        return limit_offset, "char_limit"
    return len(text), "document_end"


def _section_evidence(text: str) -> dict[str, Any]:
    table_evidence = _find_table_evidence(text)
    filled_item_lines = _find_filled_item_lines(text)
    references = _find_spec_references(text)

    detail_lines: list[str] = []
    product_lines: list[str] = []
    quantity_lines: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line or _line_has_table_header(line) or _line_is_spec_reference(line):
            continue
        normalized = normalize_text(line)
        is_real_product_row = _line_has_real_product_row(line, table_context=True)
        if _line_has_technical_detail(line) or _line_has_model_or_article(line):
            detail_lines.append(line[:260])
        if _contains_product_signal(normalized) or is_real_product_row:
            product_lines.append(line[:260])
        if _line_has_quantity_value(line) or _line_has_quantity_cell(line):
            quantity_lines.append(line[:260])

    has_filled_table = bool(table_evidence.get("has_filled_table"))
    has_filled_items = bool(filled_item_lines)
    has_product_characteristics = bool(product_lines and detail_lines)
    has_product_quantities = bool(product_lines and quantity_lines)
    blank_template = bool(table_evidence.get("blank_template")) and not (
        has_filled_items or has_product_characteristics or has_product_quantities
    )

    found = bool(
        has_filled_table
        or has_filled_items
        or has_product_characteristics
        or has_product_quantities
    )

    if has_filled_table:
        evidence_reason = "filled_nomenclature_table"
        confidence = "high"
    elif has_filled_items:
        evidence_reason = "filled_item_rows"
        confidence = "high" if len(filled_item_lines) > 1 else "medium"
    elif has_product_characteristics:
        evidence_reason = "product_characteristics"
        confidence = "medium"
    elif has_product_quantities:
        evidence_reason = "product_quantities"
        confidence = "medium"
    else:
        evidence_reason = None
        confidence = "low"

    return {
        "found": found,
        "reason": evidence_reason,
        "confidence": confidence,
        "references_only": bool(references and not found),
        "blank_template": blank_template,
        "matched": {
            "table_headers": table_evidence.get("headers") or [],
            "filled_item_rows": (table_evidence.get("filled_rows") or filled_item_lines)[:6],
            "product_lines": product_lines[:6],
            "detail_lines": detail_lines[:6],
            "quantity_lines": quantity_lines[:6],
            "specification_references": references[:6],
        },
    }


def extract_technical_spec_sections(
    filename: str | None = None,
    text: str | None = None,
    *,
    max_section_chars: int = MAX_TECHNICAL_SECTION_CHARS,
    max_sections: int = 5,
) -> dict[str, Any]:
    """Extract technical spec sections from a larger KD/documentation text."""

    source_text = text or ""
    line_offsets = _line_offsets(source_text)
    sections: list[dict[str, Any]] = []
    covered_until = -1
    references_found = bool(_find_spec_references(source_text))
    table_evidence = _find_table_evidence(source_text)
    blank_template_found = bool(table_evidence.get("blank_template"))

    for index, item in enumerate(line_offsets):
        start_offset = int(item["start"])
        if start_offset < covered_until:
            continue

        title = _technical_section_title_from_line(str(item["line"]))
        if not title:
            continue

        end_offset, end_reason = _technical_section_end_offset(
            source_text,
            line_offsets,
            index,
            max_section_chars=max_section_chars,
        )
        section_text = source_text[start_offset:end_offset].strip()
        evidence = _section_evidence(section_text)
        blank_template_found = blank_template_found or bool(evidence.get("blank_template"))

        if not evidence.get("found"):
            references_found = references_found or bool(evidence.get("references_only"))
            continue

        sections.append(
            {
                "title": title,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "text": section_text,
                "reason": TECHNICAL_SECTION_FOUND_REASON,
                "confidence": evidence.get("confidence") or "medium",
                "evidence": evidence.get("reason"),
                "end_reason": end_reason,
                "matched_signals": evidence.get("matched") or {},
            }
        )
        covered_until = end_offset

        if len(sections) >= max_sections:
            break

    return {
        "found": bool(sections),
        "sections": sections,
        "references_only": bool(references_found and not sections),
        "blank_template": bool(blank_template_found and not sections),
        "source_hint": _source_hint(filename or ""),
    }


def _section_has_real_content(text: str, signals: tuple[str, ...]) -> bool:
    lines = [_clean_line(line) for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line:
            continue
        if not any(_line_looks_like_section_heading(line, signal) for signal in signals):
            continue

        window = "\n".join(lines[index + 1 : index + 80])
        if _find_table_evidence(window).get("has_filled_table"):
            return True
        if _find_filled_item_lines(window, limit=1):
            return True
    return False


def _line_looks_like_section_heading(line: str, signal: str) -> bool:
    normalized = normalize_text(line)
    if signal not in normalized:
        return False
    if any(negative in normalized for negative in NEGATIVE_TECHNICAL_SECTION_CONTEXTS):
        return False
    if _line_is_spec_reference(normalized):
        return False
    if re.search(
        rf"^\s*(?:раздел|часть|таблица|приложение(?:\s*№?\s*\d+[\.)]?)?|[0-9]+(?:\.[0-9]+)*[\.)]?)?\s*"
        rf"{re.escape(signal)}(?:\s|\.|:|/|$)",
        normalized,
    ):
        return True
    if len(normalized) <= 80 and not re.search(
        r"\b(согласно|соответствии|предоставляется|прикладывается|является)\b",
        normalized,
    ):
        return True
    return False


def _find_section_headings(text: str, signals: tuple[str, ...]) -> list[str]:
    headings: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_line(raw_line)
        if not line:
            continue
        for signal in signals:
            if signal in headings:
                continue
            if _line_looks_like_section_heading(line, signal):
                headings.append(signal)
    return headings


def _table_signal_counts(corpus: str) -> dict[str, list[str]]:
    return {
        "name": _contains_any(corpus, NOMENCLATURE_NAME_SIGNALS),
        "quantity": _contains_any(corpus, QUANTITY_SIGNALS),
        "characteristic": _contains_any(corpus, CHARACTERISTIC_SIGNALS),
        "price": _contains_any(corpus, PRICE_SIGNALS),
        "product": _contains_product_signal(corpus),
    }


def _is_price_document(filename: str, corpus: str) -> bool:
    filename_norm = normalize_text(filename)
    return bool(
        _contains_any(filename_norm, PRICE_DOC_FILENAME_SIGNALS)
        or _contains_any(corpus[:8000], ("нмцк", "нмцд", "обоснование цены"))
    )


def _source_hint(filename: str) -> str | None:
    filename_norm = normalize_text(filename)
    if any(signal in filename_norm for signal in EXCEL_FILENAME_SIGNALS):
        return "excel"
    if any(signal in filename_norm for signal in PRICE_DOC_FILENAME_SIGNALS):
        return "price_document"
    if any(signal in filename_norm for signal in PROCUREMENT_DOC_FILENAME_SIGNALS):
        return "procurement_documentation"
    return None


def detect_technical_spec_content(
    filename: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Detect whether extracted text contains a technical spec or nomenclature table."""

    source_text = text or ""
    corpus = normalize_text(source_text[:180000])
    filename_text = filename or ""

    strong_headings = _find_section_headings(source_text, STRONG_TECHNICAL_SECTION_SIGNALS)
    weak_headings = _find_section_headings(source_text, WEAK_TECHNICAL_SECTION_SIGNALS)
    table_signals = _table_signal_counts(corpus)
    table_evidence = _find_table_evidence(source_text)
    filled_item_lines = _find_filled_item_lines(source_text)
    spec_references = _find_spec_references(source_text)
    section_extraction = extract_technical_spec_sections(filename_text, source_text)

    section_has_real_spec = (
        bool(section_extraction.get("found"))
        or _section_has_real_content(source_text, STRONG_TECHNICAL_SECTION_SIGNALS)
        or _section_has_real_content(source_text, WEAK_TECHNICAL_SECTION_SIGNALS)
    )
    table_has_nomenclature = bool(table_evidence.get("has_filled_table"))
    has_product_table = table_has_nomenclature or bool(
        filled_item_lines and (strong_headings or weak_headings or spec_references)
    )
    if not section_has_real_spec and has_product_table and (
        strong_headings or weak_headings or spec_references
    ):
        section_has_real_spec = True

    detected = False
    reason: str | None = None

    if section_extraction.get("found"):
        detected = True
        reason = TECHNICAL_SECTION_FOUND_REASON
    elif section_has_real_spec:
        detected = True
        reason = "content_contains_technical_spec"
    elif table_has_nomenclature:
        detected = True
        reason = "content_contains_supply_table"
    elif table_evidence.get("blank_template"):
        reason = "blank_specification_template"
    elif spec_references:
        reason = "references_specification_only"

    if detected and _is_price_document(filename_text, corpus):
        reason = "price_doc_contains_supply_spec"

    matched_signals = {
        key: values[:6]
        for key, values in table_signals.items()
        if values
    }
    if strong_headings:
        matched_signals["strong_sections"] = strong_headings[:6]
    if weak_headings:
        matched_signals["weak_sections"] = weak_headings[:6]
    if table_evidence.get("headers"):
        matched_signals["table_headers"] = table_evidence["headers"]
    if table_evidence.get("filled_rows"):
        matched_signals["filled_item_rows"] = table_evidence["filled_rows"]
    elif filled_item_lines:
        matched_signals["filled_item_rows"] = filled_item_lines[:6]
    if spec_references:
        matched_signals["specification_references"] = spec_references[:6]

    return {
        "detected": detected,
        "reason": reason,
        "source_hint": _source_hint(filename_text),
        "strong_headings": strong_headings,
        "weak_headings": weak_headings,
        "table_has_nomenclature": table_has_nomenclature,
        "has_product_table": has_product_table,
        "section_has_real_spec": section_has_real_spec,
        "technical_section_found": bool(section_extraction.get("found")),
        "technical_sections": section_extraction.get("sections") or [],
        "blank_template": bool(
            (table_evidence.get("blank_template") or section_extraction.get("blank_template"))
            and not has_product_table
            and not section_has_real_spec
        ),
        "references_only": bool(
            (spec_references or section_extraction.get("references_only")) and not detected
        ),
        "matched_signals": matched_signals,
    }


def technical_spec_found_by_content(filename: str | None, text: str | None) -> bool:
    return bool(detect_technical_spec_content(filename, text).get("detected"))
