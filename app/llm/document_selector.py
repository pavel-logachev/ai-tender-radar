from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.document_status import (
    DOCUMENTS_PRESENT_NO_PRIMARY_SPEC,
    TECHNICAL_SPEC_FOUND,
)
from app.document_quality import classify_primary_document_quality
from app.llm.factory import create_llm_client
from app.technical_spec_detector import detect_technical_spec_content


SELECTOR_ROLES = {
    "technical_requirements",
    "specification",
    "object_description",
    "documentation",
    "contract",
    "nmck",
    "application_form",
    "protocol",
    "other",
    "unknown",
}
SELECTOR_CONFIDENCES = {"high", "medium", "low"}
SELECTOR_STATUSES = {
    TECHNICAL_SPEC_FOUND,
    DOCUMENTS_PRESENT_NO_PRIMARY_SPEC,
    "documents_missing",
    "selector_failed",
}
PRIMARY_ROLES = {
    "technical_requirements",
    "specification",
    "object_description",
    "documentation",
}
SECONDARY_DOC_TYPES = {"contract", "price_calculation", "application_requirements"}

TECHNICAL_TITLE_MARKERS = (
    "\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u043e\u0435 \u0437\u0430\u0434\u0430\u043d\u0438\u0435",
    "\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u0441\u043f\u0435\u0446\u0438\u0444\u0438\u043a\u0430\u0446\u0438\u044f",
    "\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f",
    "\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u043e\u0431\u044a\u0435\u043a\u0442\u0430 \u0437\u0430\u043a\u0443\u043f\u043a\u0438",
    "\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u043f\u0440\u0435\u0434\u043c\u0435\u0442\u0430 \u0437\u0430\u043a\u0443\u043f\u043a\u0438",
    "\u0441\u043f\u0435\u0446\u0438\u0444\u0438\u043a\u0430\u0446",
    "\u0432\u0435\u0434\u043e\u043c\u043e\u0441\u0442\u044c \u043f\u043e\u0441\u0442\u0430\u0432\u043a\u0438",
    "\u043f\u0435\u0440\u0435\u0447\u0435\u043d\u044c \u043e\u0431\u043e\u0440\u0443\u0434\u043e\u0432\u0430\u043d\u0438\u044f",
    "specification",
    "technical requirements",
)
SMART_EXCERPT_MARKERS = (
    "техническое задание",
    "техзадание",
    "тех. задание",
    "технические требования",
    "спецификация",
    "приложение",
    "приложение №1",
    "приложение 1",
    "характеристики",
    "комплект поставки",
    "наименование товара",
    "количество",
    "кол-во",
    "артикул",
    "модель",
    "назначение",
    "место поставки",
    "предмет закупки",
    "описание объекта закупки",
    "описание предмета закупки",
)
SMART_EXCERPT_REGEXES = (
    re.compile(
        r"(?im)^\s*(?:раздел|часть)\s+\d+(?:[\.)])?\s*"
        r"(?:техническое\s+задание|технические\s+требования|описание\s+объекта\s+закупки|описание\s+предмета\s+закупки)\b.*$"
    ),
    re.compile(
        r"(?im)^\s*приложени[ея]\s*(?:№|n|no)?\s*\d+(?:[\.)])?\s+"
        r"(?:к\s+)?(?:техническому\s+заданию|техническим\s+требованиям|описанию\s+объекта\s+закупки|описанию\s+предмета\s+закупки|тз)\b.*$"
    ),
    re.compile(r"(?im)^\s*(?:техническое\s+задание|технические\s+требования)\b.*$"),
    re.compile(r"(?im)^\s*спецификация\b.*$"),
    re.compile(
        r"(?im)^.*наименование\s+товара.*"
        r"(?:кол-?во|количество|предельная\s+цена|цена|сумма|ед\.?\s*изм).*$"
    ),
)
SECTION_HINT_TRIGGERS = {
    "техническое задание",
    "техзадание",
    "тех. задание",
    "технические требования",
    "спецификация",
    "приложение",
    "приложение №1",
    "приложение 1",
    "описание объекта закупки",
    "описание предмета закупки",
}
SECTION_HINT_LABELS = {
    "техническое задание": "Техническое задание",
    "техзадание": "Техзадание",
    "тех. задание": "Тех. задание",
    "технические требования": "Технические требования",
    "спецификация": "Спецификация",
    "приложение": "Приложение",
    "приложение №1": "Приложение №1",
    "приложение 1": "Приложение 1",
    "описание объекта закупки": "Описание объекта закупки",
    "описание предмета закупки": "Описание предмета закупки",
}
CATEGORY_EXCERPT_TERMS = (
    "сервер",
    "схд",
    "система хранения",
    "дисковое пространство",
    "накопитель",
    "коммутатор",
    "маршрутизатор",
    "storage",
    "server",
    "switch",
    "router",
)
TITLE_TERM_STOPWORDS = {
    "поставка",
    "закупка",
    "оборудование",
    "услуги",
    "товар",
    "работы",
    "для",
    "оказание",
    "выполнение",
    "приобретение",
    "поставки",
    "закупке",
    "объекта",
    "предмета",
    "система",
    "системы",
    "комплект",
    "комплекта",
    "and",
    "the",
}
BLANK_SPEC_TEMPLATE_MARKERS = (
    "спецификация будет заполнена",
    "спецификация заполняется",
    "будет заполнена победителем",
    "заполняется победителем",
    "для заполнения победителем",
)
LOW_EVIDENCE_TEMPLATE_TRIGGERS = {
    "спецификация",
    "приложение",
    "приложение №1",
    "приложение 1",
}
NON_PRIMARY_TITLE_MARKERS = (
    "\u043f\u0440\u043e\u0442\u043e\u043a\u043e\u043b",
    "\u0444\u043e\u0440\u043c\u0430 \u0437\u0430\u044f\u0432\u043a",
    "\u0437\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u0443\u0447\u0430\u0441\u0442\u0438\u0435",
    "\u043f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440",
    "\u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442",
    "\u043d\u043c\u0446\u043a",
    "\u043d\u043c\u0446\u0434",
    "\u043e\u0431\u043e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435 \u0446\u0435\u043d",
)
TZ_RE = re.compile(r"(?<![\w\u0430-\u044f])\u0442\u0437(?![\w\u0430-\u044f])", re.IGNORECASE)


def _doc_type_text(doc_type: str) -> str:
    try:
        from app.document_risk_analyzer import doc_type_text

        return doc_type_text(doc_type)
    except Exception:
        return {
            "technical_spec": "\u0422\u0417 / \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u043e\u0431\u044a\u0435\u043a\u0442\u0430 \u0437\u0430\u043a\u0443\u043f\u043a\u0438",
            "contract": "\u043f\u0440\u043e\u0435\u043a\u0442 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442\u0430",
            "application_requirements": "\u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f \u043a \u0437\u0430\u044f\u0432\u043a\u0435",
            "price_calculation": "\u041d\u041c\u0426\u041a / \u0440\u0430\u0441\u0447\u0435\u0442 \u0446\u0435\u043d\u044b",
            "other": "\u043f\u0440\u043e\u0447\u0438\u0439 \u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442",
        }.get(doc_type, doc_type)


def _classify_document_details(filename: str | None, text: str | None) -> dict[str, Any]:
    try:
        from app.document_risk_analyzer import classify_document_details

        return classify_document_details(filename, text)
    except Exception:
        detection = detect_technical_spec_content(filename, text)
        filename_src = _text(filename).casefold().replace("\u0451", "\u0435")
        text_head = _text(text)[:3000].casefold().replace("\u0451", "\u0435")
        src = f"{filename_src}\n{text_head}"
        non_real_spec_detection = bool(
            detection.get("blank_template")
            or detection.get("references_only")
            or detection.get("reason")
            in {"blank_specification_template", "references_specification_only"}
        )
        if (
            detection.get("detected")
            or (
                not non_real_spec_detection
                and (
                    TZ_RE.search(filename_src)
                    or any(marker in filename_src for marker in TECHNICAL_TITLE_MARKERS)
                )
            )
        ):
            return {
                "doc_type": "technical_spec",
                "doc_type_reason": detection.get("reason")
                or "filename_or_content_contains_technical_spec",
                "technical_spec_detection": detection,
            }
        if "\u043f\u0440\u043e\u0435\u043a\u0442 \u0434\u043e\u0433\u043e\u0432\u043e\u0440" in src or "\u043f\u0440\u043e\u0435\u043a\u0442 \u043a\u043e\u043d\u0442\u0440\u0430\u043a\u0442" in src:
            return {
                "doc_type": "contract",
                "doc_type_reason": "filename_or_header_contains_contract",
                "technical_spec_detection": detection,
            }
        if "\u0444\u043e\u0440\u043c\u0430 \u0437\u0430\u044f\u0432\u043a" in src or "\u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f \u043a \u0437\u0430\u044f\u0432\u043a" in src:
            return {
                "doc_type": "application_requirements",
                "doc_type_reason": "content_contains_application_requirements",
                "technical_spec_detection": detection,
            }
        if "\u043d\u043c\u0446\u043a" in src or "\u043d\u043c\u0446\u0434" in src or "\u043e\u0431\u043e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435" in src:
            return {
                "doc_type": "price_calculation",
                "doc_type_reason": "filename_or_header_contains_price_calculation",
                "technical_spec_detection": detection,
            }
        return {
            "doc_type": "other",
            "doc_type_reason": "no_specific_document_type_detected",
            "technical_spec_detection": detection,
        }


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _normalize_text(value: Any) -> str:
    return _text(value).casefold().replace("\u0451", "\u0435")


def _window_bounds(text: str, start: int, end: int, *, radius: int = 700) -> tuple[int, int]:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return left, right


def _snippet(text: str, start: int, end: int, *, radius: int = 700) -> str:
    left, right = _window_bounds(text, start, end, radius=radius)
    return _compact_text(text[left:right], limit=1500)


def _window_overlaps(
    interval: tuple[int, int],
    intervals: list[tuple[int, int]],
    *,
    threshold: float = 0.55,
) -> bool:
    start, end = interval
    length = max(1, end - start)
    for existing_start, existing_end in intervals:
        overlap = min(end, existing_end) - max(start, existing_start)
        if overlap <= 0:
            continue
        existing_length = max(1, existing_end - existing_start)
        if overlap / min(length, existing_length) >= threshold:
            return True
    return False


def _literal_windows(
    text: str,
    term: str,
    *,
    limit: int,
    seen: set[str],
    intervals: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    if not term:
        return []
    normalized = _normalize_text(text)
    needle = _normalize_text(term)
    if not needle:
        return []

    windows: list[dict[str, Any]] = []
    intervals = intervals if intervals is not None else []
    start = 0
    while len(windows) < limit:
        idx = normalized.find(needle, start)
        if idx < 0:
            break
        end = idx + len(needle)
        left, right = _window_bounds(text, idx, end)
        if _window_overlaps((left, right), intervals):
            start = end
            continue
        excerpt = _compact_text(text[left:right], limit=1500)
        key = _normalize_text(excerpt)[:500]
        if key and key not in seen:
            windows.append(
                {
                    "trigger": term,
                    "text": excerpt,
                    "start": left,
                    "end": right,
                }
            )
            seen.add(key)
            intervals.append((left, right))
        start = end
    return windows


def _regex_windows(
    text: str,
    pattern: re.Pattern[str],
    *,
    limit: int,
    seen: set[str],
    intervals: list[tuple[int, int]] | None = None,
    radius: int = 900,
    allow_overlap: bool = False,
) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    intervals = intervals if intervals is not None else []
    for match in pattern.finditer(text):
        if len(windows) >= limit:
            break
        start, end = match.span()
        left, right = _window_bounds(text, start, end, radius=radius)
        if not allow_overlap and _window_overlaps((left, right), intervals, threshold=0.85):
            continue
        trigger = _compact_text(match.group(0), limit=160)
        excerpt = _compact_text(text[left:right], limit=1500)
        key = f"{_normalize_text(trigger)}:{_normalize_text(excerpt)[:500]}"
        if key and key not in seen:
            windows.append(
                {
                    "trigger": trigger,
                    "text": excerpt,
                    "start": left,
                    "end": right,
                }
            )
            seen.add(key)
            intervals.append((left, right))
    return windows


def _add_keyword_term(terms: list[str], term: str) -> None:
    value = re.sub(r"^[^\wА-Яа-яЁё]+|[^\wА-Яа-яЁё]+$", "", str(term or "")).strip()
    if len(value) < 3 or len(value) > 80:
        return
    normalized = value.casefold()
    if normalized in TITLE_TERM_STOPWORDS:
        return
    if any(existing.casefold() == normalized for existing in terms):
        return
    terms.append(value)


def _tender_keyword_terms(tender: dict[str, Any] | None) -> list[str]:
    tender = tender or {}
    chunks: list[str] = []
    for key in (
        "title",
        "category",
        "target_category",
        "business_category",
        "detected_category",
        "_llm_category",
        "_llm_category_label",
    ):
        value = tender.get(key)
        if value:
            chunks.append(str(value))

    result = tender.get("result")
    if isinstance(result, dict):
        for key in ("summary", "category", "detected_category", "positive_matches"):
            value = result.get(key)
            if isinstance(value, list):
                chunks.extend(str(item) for item in value)
            elif value:
                chunks.append(str(value))

    raw = tender.get("raw")
    full = raw.get("full") if isinstance(raw, dict) else {}
    if isinstance(full, dict):
        for key in ("orderName", "purchaseName", "subject"):
            value = full.get(key)
            if value:
                chunks.append(str(value))
        for doc in full.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            for key in ("title", "filename", "fileName", "name"):
                if doc.get(key):
                    chunks.append(str(doc.get(key)))

    source = "\n".join(chunks)
    terms: list[str] = []
    token_matches = list(
        re.finditer(r"\b[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё._/-]{2,}\b", source)
    )
    clean_tokens: list[str] = []
    for match in token_matches:
        term = match.group(0)
        normalized = term.casefold()
        if normalized in TITLE_TERM_STOPWORDS:
            continue
        clean_tokens.append(term)
        if re.search(r"[A-Za-z]", term) or re.search(r"\d", term):
            _add_keyword_term(terms, term)
        elif len(term) >= 5:
            _add_keyword_term(terms, term)

    for first, second in zip(clean_tokens, clean_tokens[1:]):
        if first.casefold() in TITLE_TERM_STOPWORDS or second.casefold() in TITLE_TERM_STOPWORDS:
            continue
        if len(first) >= 4 and len(second) >= 4:
            _add_keyword_term(terms, f"{first} {second}")

    for term in CATEGORY_EXCERPT_TERMS:
        _add_keyword_term(terms, term)
    return terms[:32]


def _smart_excerpts(
    text: str,
    *,
    tender: dict[str, Any] | None,
    max_windows: int = 10,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    intervals: list[tuple[int, int]] = []
    windows: list[dict[str, Any]] = []
    for pattern in SMART_EXCERPT_REGEXES:
        if len(windows) >= max_windows:
            break
        windows.extend(
            _regex_windows(
                text,
                pattern,
                limit=1,
                seen=seen,
                intervals=intervals,
                allow_overlap=True,
            )
        )

    dynamic_terms = _tender_keyword_terms(tender)
    groups = (
        (SMART_EXCERPT_MARKERS, max(3, max_windows // 2)),
        (tuple(dynamic_terms), max(3, max_windows // 3)),
        (CATEGORY_EXCERPT_TERMS, max(2, max_windows // 4)),
    )
    for terms, group_limit in groups:
        added = 0
        for term in terms:
            if len(windows) >= max_windows or added >= group_limit:
                break
            new_windows = _literal_windows(
                text,
                term,
                limit=max(1, min(2, group_limit - added, max_windows - len(windows))),
                seen=seen,
                intervals=intervals,
            )
            windows.extend(new_windows)
            added += len(new_windows)

    if len(windows) < max_windows:
        all_terms = list(dict.fromkeys(SMART_EXCERPT_MARKERS + tuple(dynamic_terms) + CATEGORY_EXCERPT_TERMS))
        for term in all_terms:
            if len(windows) >= max_windows:
                break
            windows.extend(
                _literal_windows(
                    text,
                    term,
                    limit=max(1, max_windows - len(windows)),
                    seen=seen,
                    intervals=intervals,
                )
            )
    return windows[:max_windows]


def _combined_excerpt(
    text: str,
    smart_excerpts: list[dict[str, str]],
    *,
    limit: int,
) -> str:
    header = _compact_text(text, limit=min(1600, limit))
    if not smart_excerpts:
        return _compact_text(text, limit=limit)

    parts = [f"HEADER: {header}"]
    for item in smart_excerpts:
        trigger = _compact_text(item.get("trigger"), limit=80)
        excerpt = _compact_text(item.get("text"), limit=1300)
        parts.append(f"WINDOW [{trigger}]: {excerpt}")
    return _compact_text("\n\n".join(parts), limit=limit)


def _document_id(doc: dict[str, Any], index: int) -> str:
    for key in ("id", "document_id", "external_document_id", "api_document_id"):
        value = str(doc.get(key) or "").strip()
        if value:
            return value
    filename = str(doc.get("filename") or doc.get("title") or "").strip()
    return filename or f"document-{index}"


def _document_title(doc: dict[str, Any]) -> str:
    return str(
        doc.get("title")
        or doc.get("name")
        or doc.get("filename")
        or doc.get("fileName")
        or "document"
    ).strip()


def _document_filename(doc: dict[str, Any]) -> str:
    return str(doc.get("filename") or doc.get("fileName") or _document_title(doc)).strip()


def _extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower().strip(".")
    return suffix


def _technical_detection_summary(detection: Any) -> dict[str, Any]:
    detection = detection if isinstance(detection, dict) else {}
    sections = detection.get("technical_sections") or []
    sections = sections if isinstance(sections, list) else []
    return {
        "detected": bool(detection.get("detected")),
        "reason": detection.get("reason"),
        "technical_sections_count": len(sections),
        "technical_section_titles": [
            _compact_text(section.get("title"), limit=160)
            for section in sections
            if isinstance(section, dict) and section.get("title")
        ][:6],
        "source_hint": detection.get("source_hint"),
        "blank_template": bool(detection.get("blank_template")),
        "references_only": bool(detection.get("references_only")),
        "has_product_table": bool(detection.get("has_product_table")),
        "section_has_real_spec": bool(detection.get("section_has_real_spec")),
    }


def _section_hints(
    detection_summary: dict[str, Any],
    smart_excerpts: list[dict[str, Any]],
    excerpt: str,
) -> list[str]:
    hints: list[str] = []

    def add(value: Any) -> None:
        text = _compact_text(value, limit=180)
        if not text:
            return
        normalized = _normalize_text(text)
        if any(_normalize_text(existing) == normalized for existing in hints):
            return
        hints.append(text)

    for title in detection_summary.get("technical_section_titles") or []:
        add(title)

    triggers = [
        str(item.get("trigger") or "")
        for item in smart_excerpts
        if isinstance(item, dict)
    ]
    excerpt_norm = _normalize_text(excerpt)
    for trigger in triggers:
        trigger_norm = _normalize_text(trigger)
        if (
            (
                "раздел" in trigger_norm
                and any(marker in trigger_norm for marker in ("техническое задание", "технические требования"))
            )
            or (
                "приложение" in trigger_norm
                and any(marker in trigger_norm for marker in ("техническому заданию", "техническим требованиям", "тз"))
            )
            or trigger_norm.startswith("спецификация")
        ):
            add(trigger)

    for trigger in SECTION_HINT_TRIGGERS:
        if trigger in triggers or trigger in excerpt_norm:
            add(SECTION_HINT_LABELS.get(trigger, trigger))

    return hints[:5]


def build_document_candidates(
    documents: list[dict[str, Any]],
    *,
    tender: dict[str, Any] | None = None,
    excerpt_chars: int = 4200,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, doc in enumerate(documents, start=1):
        text = str(doc.get("extracted_text") or "").strip()
        if not text:
            continue

        filename = _document_filename(doc)
        details = _classify_document_details(filename, text)
        doc_type = str(details.get("doc_type") or "other")
        detection = details.get("technical_spec_detection") or {}
        smart_excerpts = _smart_excerpts(text, tender=tender)
        detection_summary = _technical_detection_summary(detection)
        excerpt = _combined_excerpt(
            text,
            smart_excerpts,
            limit=excerpt_chars,
        )
        candidate = {
            "id": _document_id(doc, index),
            "title": _document_title(doc),
            "filename": filename,
            "mime_type": doc.get("mime_type"),
            "extension": _extension(filename),
            "doc_type": doc_type,
            "doc_type_text": _doc_type_text(doc_type),
            "doc_type_reason": details.get("doc_type_reason"),
            "technical_spec_detection": detection_summary,
            "text_len": len(text),
            "header_excerpt": _compact_text(text, limit=1600),
            "smart_excerpts": smart_excerpts,
            "section_hints": _section_hints(detection_summary, smart_excerpts, excerpt),
            "excerpt": excerpt,
        }
        candidates.append(candidate)
    return candidates


def _candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join(
        str(candidate.get(key) or "")
        for key in ("title", "filename", "doc_type", "doc_type_reason")
    ).casefold().replace("\u0451", "\u0435")


def _candidate_score(candidate: dict[str, Any]) -> int:
    text = _candidate_text(candidate)
    detection = candidate.get("technical_spec_detection") or {}
    score = 0
    if candidate.get("doc_type") == "technical_spec":
        score += 100
    if detection.get("detected"):
        score += 45
    if detection.get("technical_sections_count"):
        score += 25
    if TZ_RE.search(text):
        score += 35
    if any(marker in text for marker in TECHNICAL_TITLE_MARKERS):
        score += 35
    if any(marker in text for marker in NON_PRIMARY_TITLE_MARKERS):
        score -= 40
    if candidate.get("doc_type") == "contract":
        score -= 45
    if candidate.get("doc_type") == "application_requirements":
        score -= 35
    if candidate.get("doc_type") == "price_calculation":
        score -= 20
    return score


def _role_for_candidate(candidate: dict[str, Any]) -> str:
    text = _candidate_text(candidate)
    doc_type = candidate.get("doc_type")
    if doc_type == "technical_spec":
        detection = candidate.get("technical_spec_detection") or {}
        if (
            isinstance(detection, dict)
            and detection.get("source_hint") == "procurement_documentation"
        ) or any(marker in text for marker in ("документац", "извещен", "кд", "материал")):
            return "documentation"
        if "\u0441\u043f\u0435\u0446" in text or "specification" in text:
            return "specification"
        if "\u043e\u043f\u0438\u0441\u0430\u043d" in text:
            return "object_description"
        return "technical_requirements"
    if doc_type == "contract":
        return "contract"
    if doc_type == "price_calculation":
        return "nmck"
    if doc_type == "application_requirements":
        return "application_form"
    if "\u043f\u0440\u043e\u0442\u043e\u043a\u043e\u043b" in text:
        return "protocol"
    return "other"


def _primary_section_hint_for_candidate(candidate: dict[str, Any] | None) -> str:
    if not candidate:
        return ""
    hints = [
        str(item or "").strip()
        for item in (candidate.get("section_hints") or [])
        if str(item or "").strip()
    ]
    if not hints:
        detection = candidate.get("technical_spec_detection") or {}
        if isinstance(detection, dict):
            hints = [
                str(item or "").strip()
                for item in (detection.get("technical_section_titles") or [])
                if str(item or "").strip()
            ]
    return _compact_text(" / ".join(dict.fromkeys(hints[:4])), limit=260)


def _document_title_by_id(candidates: list[dict[str, Any]], doc_id: str | None) -> str:
    if not doc_id:
        return ""
    for candidate in candidates:
        if str(candidate.get("id")) == str(doc_id):
            return str(candidate.get("title") or candidate.get("filename") or "")
    return ""


def _candidate_by_id(
    candidates: list[dict[str, Any]],
    doc_id: str | None,
) -> dict[str, Any] | None:
    if not doc_id:
        return None
    for candidate in candidates:
        if str(candidate.get("id")) == str(doc_id):
            return candidate
    return None


def _selection_with_quality(
    selection: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    primary = _candidate_by_id(candidates, selection.get("primary_document_id"))
    docs_count = len(candidates)
    docs_with_text_count = sum(
        1 for candidate in candidates if int(candidate.get("text_len") or 0) > 0
    )
    quality = classify_primary_document_quality(
        selection,
        primary_candidate=primary,
        docs_count=docs_count,
        docs_with_text_count=docs_with_text_count,
    )
    return {**selection, **quality}


def _candidate_is_blank_template(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    detection = candidate.get("technical_spec_detection") or {}
    if not isinstance(detection, dict):
        return False
    if bool(
        detection.get("blank_template")
        and not detection.get("detected")
    ):
        return True
    if detection.get("detected"):
        return False
    corpus = _normalize_text(
        "\n".join(
            str(candidate.get(key) or "")
            for key in ("title", "filename", "header_excerpt", "excerpt")
        )
    )
    if "спецификац" not in corpus:
        return False
    if any(marker in corpus for marker in BLANK_SPEC_TEMPLATE_MARKERS):
        return True
    return "заполн" in corpus and "победител" in corpus


def _candidate_lacks_real_spec_evidence(candidate: dict[str, Any] | None) -> bool:
    if not candidate:
        return False
    detection = candidate.get("technical_spec_detection") or {}
    if isinstance(detection, dict) and detection.get("detected"):
        return False
    if candidate.get("doc_type") == "technical_spec":
        return False
    text_len = int(candidate.get("text_len") or 0)
    if text_len > 2500:
        return False
    triggers = {
        _normalize_text(item.get("trigger"))
        for item in (candidate.get("smart_excerpts") or [])
        if isinstance(item, dict)
    }
    meaningful_triggers = {
        trigger
        for trigger in triggers
        if trigger and trigger not in LOW_EVIDENCE_TEMPLATE_TRIGGERS
    }
    return not meaningful_triggers


def _secondary_ids(candidates: list[dict[str, Any]], primary_id: str | None) -> list[str]:
    result: list[str] = []
    for candidate in candidates:
        doc_id = str(candidate.get("id") or "")
        if not doc_id or doc_id == str(primary_id or ""):
            continue
        if candidate.get("doc_type") in SECONDARY_DOC_TYPES:
            result.append(doc_id)
        if len(result) >= 4:
            break
    return result


def _excluded_documents(
    candidates: list[dict[str, Any]],
    *,
    primary_id: str | None,
    secondary_ids: list[str],
) -> list[dict[str, str]]:
    selected = {str(primary_id or ""), *(str(doc_id) for doc_id in secondary_ids)}
    excluded: list[dict[str, str]] = []
    for candidate in candidates:
        doc_id = str(candidate.get("id") or "")
        if not doc_id or doc_id in selected:
            continue
        role = _role_for_candidate(candidate)
        reason = {
            "contract": "supporting contract terms only",
            "application_form": "application or participant form, not technical requirements",
            "protocol": "protocol, not technical requirements",
            "nmck": "price/NMCK support document, not primary technical requirements",
        }.get(role, "not selected as primary technical requirements")
        excluded.append({"id": doc_id, "reason": reason})
    return excluded[:20]


def deterministic_document_selection(
    candidates: list[dict[str, Any]],
    *,
    require_single_confident_primary: bool = False,
) -> dict[str, Any]:
    if not candidates:
        return _selection_with_quality(
            {
            "primary_document_id": None,
            "primary_document_role": "unknown",
            "primary_document_confidence": "low",
            "primary_document_title": "",
            "primary_section_hint": "",
            "secondary_document_ids": [],
            "excluded_document_ids": [],
            "status": "documents_missing",
            "selector_status": "documents_missing",
            "reason": "No documents with extracted text.",
            "selector_used": False,
            "selector_failed": False,
            "source": "deterministic",
            },
            candidates,
        )

    scored = sorted(
        ((candidate, _candidate_score(candidate)) for candidate in candidates),
        key=lambda item: item[1],
        reverse=True,
    )
    confident = [
        candidate
        for candidate, score in scored
        if candidate.get("doc_type") == "technical_spec" and score >= 110
    ]

    if require_single_confident_primary and len(confident) != 1:
        primary = None
    else:
        primary = confident[0] if confident else None

    if primary is None:
        secondary_ids = _secondary_ids(candidates, None)
        return _selection_with_quality(
            {
            "primary_document_id": None,
            "primary_document_role": "unknown",
            "primary_document_confidence": "low",
            "primary_document_title": "",
            "primary_section_hint": "",
            "secondary_document_ids": secondary_ids,
            "excluded_document_ids": _excluded_documents(
                candidates,
                primary_id=None,
                secondary_ids=secondary_ids,
            ),
            "status": DOCUMENTS_PRESENT_NO_PRIMARY_SPEC,
            "selector_status": DOCUMENTS_PRESENT_NO_PRIMARY_SPEC,
            "reason": "Documents are present, but no confident primary technical document was found deterministically.",
            "selector_used": False,
            "selector_failed": False,
            "source": "deterministic",
            },
            candidates,
        )

    primary_id = str(primary.get("id") or "")
    secondary_ids = _secondary_ids(candidates, primary_id)
    return _selection_with_quality(
        {
        "primary_document_id": primary_id,
        "primary_document_role": _role_for_candidate(primary),
        "primary_document_confidence": "high",
        "primary_document_title": _document_title_by_id(candidates, primary_id),
        "primary_section_hint": _primary_section_hint_for_candidate(primary),
        "secondary_document_ids": secondary_ids,
        "excluded_document_ids": _excluded_documents(
            candidates,
            primary_id=primary_id,
            secondary_ids=secondary_ids,
        ),
        "status": TECHNICAL_SPEC_FOUND,
        "selector_status": TECHNICAL_SPEC_FOUND,
        "reason": "One deterministic technical specification candidate was found.",
        "selector_used": False,
        "selector_failed": False,
        "source": "deterministic",
        },
        candidates,
    )


def _extract_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("selector response is not a JSON object")
    return parsed


def _normalize_selector_response(
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_ids = {str(candidate.get("id")) for candidate in candidates}
    primary_id = payload.get("primary_document_id")
    primary_id = str(primary_id).strip() if primary_id is not None else ""
    if primary_id and primary_id not in candidate_ids:
        primary_id = ""
    primary_candidate = _candidate_by_id(candidates, primary_id)
    if primary_id and _candidate_is_blank_template(primary_candidate):
        primary_id = ""

    role = str(payload.get("primary_document_role") or "unknown").strip()
    if role not in SELECTOR_ROLES:
        role = "unknown"
    if (
        primary_id
        and role in PRIMARY_ROLES
        and _candidate_lacks_real_spec_evidence(primary_candidate)
    ):
        primary_id = ""
        primary_candidate = None

    confidence = str(payload.get("primary_document_confidence") or "low").strip()
    if confidence not in SELECTOR_CONFIDENCES:
        confidence = "low"

    secondary_ids: list[str] = []
    for item in payload.get("secondary_document_ids") or []:
        doc_id = str(item or "").strip()
        if doc_id and doc_id in candidate_ids and doc_id != primary_id and doc_id not in secondary_ids:
            secondary_ids.append(doc_id)
        if len(secondary_ids) >= 5:
            break

    excluded: list[dict[str, str]] = []
    for item in payload.get("excluded_document_ids") or []:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("id") or "").strip()
        if not doc_id or doc_id not in candidate_ids or doc_id == primary_id:
            continue
        excluded.append(
            {
                "id": doc_id,
                "reason": _compact_text(item.get("reason"), limit=220),
            }
        )
    if not excluded:
        excluded = _excluded_documents(
            candidates,
            primary_id=primary_id or None,
            secondary_ids=secondary_ids,
        )

    section_hint = _compact_text(
        payload.get("primary_section_hint")
        or _primary_section_hint_for_candidate(primary_candidate),
        limit=260,
    )

    status = str(payload.get("selector_status") or payload.get("status") or "").strip()
    if status not in SELECTOR_STATUSES:
        status = TECHNICAL_SPEC_FOUND if primary_id else DOCUMENTS_PRESENT_NO_PRIMARY_SPEC
    if primary_id and status != TECHNICAL_SPEC_FOUND:
        status = TECHNICAL_SPEC_FOUND
    if not primary_id and status == TECHNICAL_SPEC_FOUND:
        status = DOCUMENTS_PRESENT_NO_PRIMARY_SPEC

    return _selection_with_quality(
        {
        "primary_document_id": primary_id or None,
        "primary_document_role": role if primary_id else "unknown",
        "primary_document_confidence": confidence if primary_id else "low",
        "primary_document_title": _document_title_by_id(candidates, primary_id),
        "primary_section_hint": section_hint if primary_id else "",
        "secondary_document_ids": secondary_ids,
        "excluded_document_ids": excluded,
        "status": status,
        "selector_status": status,
        "reason": _compact_text(payload.get("reason"), limit=500),
        "selector_used": True,
        "selector_failed": False,
        "source": "llm",
        },
        candidates,
    )


def build_selector_prompt(tender: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    tender_payload = {
        "title": tender.get("title"),
        "customer": tender.get("customer_name"),
        "price": tender.get("initial_price") or tender.get("price"),
        "procedure": tender.get("procedure_type"),
        "law": tender.get("law"),
        "category": tender.get("category")
        or tender.get("target_category")
        or tender.get("_llm_category"),
    }
    compact_candidates = [
        {
            key: candidate.get(key)
            for key in (
                "id",
                "title",
                "filename",
                "mime_type",
                "extension",
                "doc_type",
                "doc_type_reason",
                "technical_spec_detection",
                "text_len",
                "header_excerpt",
                "smart_excerpts",
                "section_hints",
                "excerpt",
            )
        }
        for candidate in candidates[:12]
    ]
    return f"""
Select the primary technical document for a full customer lead report.

Primary means the document that actually describes what is procured and the
technical requirements: TZ, technical assignment, object description,
specification, technical requirements, appendix with characteristics, supply
list, or procurement documentation with a real technical section.

You are only a context router. Do not analyze commercial attractiveness, do not
make go/maybe/no_go decisions, do not write a tender summary, do not create risk
analysis, and do not propose sales questions.

Do not choose as primary: protocols, application forms, pure NMCK/price
justification without technical content, empty/broken documents, or a contract
when a separate TZ/specification exists. Contracts, NMCK with positions/prices,
and equipment appendices may be secondary supporting documents.

If the technical requirements are embedded inside a general documentation file,
select that file and set primary_section_hint to the best short section hint.
Do not select a blank specification template as primary when it only says the
winner will fill the specification and contains no real product rows,
quantities, models, articles, or characteristics.

Return one strict JSON object only:
{{
  "primary_document_id": "id or null",
  "primary_document_title": "title or empty string",
  "primary_section_hint": "short section hint or empty string",
  "primary_document_role": "technical_requirements|specification|object_description|documentation|contract|nmck|other|unknown",
  "primary_document_confidence": "high|medium|low",
  "secondary_document_ids": ["id"],
  "selector_status": "technical_spec_found|documents_present_no_primary_spec|documents_missing|selector_failed",
  "reason": "one short routing sentence"
}}

Tender card:
{json.dumps(tender_payload, ensure_ascii=False, indent=2, default=str)}

Document candidates:
{json.dumps(compact_candidates, ensure_ascii=False, indent=2, default=str)}
""".strip()


def select_documents_for_lead_report(
    tender: dict[str, Any],
    documents: list[dict[str, Any]],
    *,
    use_llm_selector: bool = True,
) -> dict[str, Any]:
    candidates = build_document_candidates(documents, tender=tender)
    if not candidates:
        return deterministic_document_selection(candidates)

    deterministic = deterministic_document_selection(
        candidates,
        require_single_confident_primary=True,
    )
    if deterministic.get("status") == TECHNICAL_SPEC_FOUND:
        deterministic["candidates"] = candidates
        return deterministic

    if not use_llm_selector:
        deterministic["candidates"] = candidates
        return deterministic

    try:
        client = create_llm_client()
        json_mode = bool(settings.llm_json_mode and client.provider == "routerai")
        response = client.generate_chat_completion(
            system_prompt=(
                "You select tender documents for an IT infrastructure lead report. "
                "Return strict JSON only."
            ),
            user_prompt=build_selector_prompt(tender, candidates),
            temperature=0.0,
            max_tokens=1200,
            json_mode=json_mode,
        )
        selected = _normalize_selector_response(_extract_json(response.text), candidates)
        selected["candidates"] = candidates
        if response.response_id:
            selected["response_id"] = response.response_id
        if response.usage:
            selected["usage"] = response.usage
        return selected
    except Exception as exc:
        fallback = deterministic_document_selection(
            candidates,
            require_single_confident_primary=False,
        )
        fallback["status"] = "selector_failed"
        fallback["selector_status"] = "selector_failed"
        fallback["selector_used"] = True
        fallback["selector_failed"] = True
        fallback["selector_error"] = _compact_text(exc, limit=500)
        fallback["reason"] = (
            "Selector failed; deterministic fallback was used. "
            + str(fallback.get("reason") or "")
        ).strip()
        fallback["candidates"] = candidates
        return fallback
