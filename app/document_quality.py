from __future__ import annotations

import re
from typing import Any


DOCUMENT_CONFIDENCE_HIGH = "high"
DOCUMENT_CONFIDENCE_MEDIUM = "medium"
DOCUMENT_CONFIDENCE_LOW = "low"

DOCUMENT_SOURCE_TECHNICAL_SPEC = "technical_spec"
DOCUMENT_SOURCE_OBJECT_DESCRIPTION = "object_description"
DOCUMENT_SOURCE_PRODUCT_SPECIFICATION = "product_specification"
DOCUMENT_SOURCE_PRICING_OR_BOM = "pricing_or_bom"
DOCUMENT_SOURCE_CONTRACT_OR_NOTICE = "contract_or_notice"
DOCUMENT_SOURCE_CARD_ONLY = "card_only"
DOCUMENT_SOURCE_MISSING = "missing"
DOCUMENT_SOURCE_UNKNOWN = "unknown"

DOCUMENT_CONFIDENCES = {
    DOCUMENT_CONFIDENCE_HIGH,
    DOCUMENT_CONFIDENCE_MEDIUM,
    DOCUMENT_CONFIDENCE_LOW,
}

DOCUMENT_SOURCE_KINDS = {
    DOCUMENT_SOURCE_TECHNICAL_SPEC,
    DOCUMENT_SOURCE_OBJECT_DESCRIPTION,
    DOCUMENT_SOURCE_PRODUCT_SPECIFICATION,
    DOCUMENT_SOURCE_PRICING_OR_BOM,
    DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
    DOCUMENT_SOURCE_CARD_ONLY,
    DOCUMENT_SOURCE_MISSING,
    DOCUMENT_SOURCE_UNKNOWN,
}

TZ_RE = re.compile(r"(?<![\wа-я])тз(?![\wа-я])", re.IGNORECASE)

STRONG_TECHNICAL_MARKERS = (
    "техническое задание",
    "техзадание",
    "технические требования",
    "технические характеристики",
    "описание объекта закупки",
    "описание предмета закупки",
    "описание объекта",
    "описание предмета",
)
OBJECT_DESCRIPTION_MARKERS = (
    "описание объекта закупки",
    "описание предмета закупки",
    "описание объекта",
    "описание предмета",
    "ооз",
)
SPECIFICATION_MARKERS = (
    "спецификация",
    "ведомость поставки",
    "ведомость оборудования",
    "перечень оборудования",
    "перечень товара",
    "перечень товаров",
    "техническая спецификация",
    "product specification",
    "specification",
)
PRICING_OR_BOM_MARKERS = (
    "обоснование нмцк",
    "обоснование нмцд",
    "расчет нмцк",
    "расчёт нмцк",
    "нмцк",
    "нмцд",
    "расчет цены",
    "расчёт цены",
    "ценовая таблица",
    "таблица товаров и цен",
    "перечень товаров и цен",
    "коммерческая форма",
    "форма кп",
    "коммерческое предложение",
    "bom",
)
CONTRACT_OR_NOTICE_MARKERS = (
    "проект договора",
    "проект контракта",
    "договор",
    "контракт",
    "извещение",
    "протокол",
    "форма заявки",
    "заявка на участие",
    "локальный сметный расчет",
    "локальный сметный расчёт",
    "смета",
)
SUSPICIOUS_PRIMARY_TITLE_MARKERS = (
    "обоснование нмцк",
    "обоснование нмцд",
    "расчет нмцк",
    "расчёт нмцк",
    "нмцк",
    "протокол",
    "извещение",
    "проект договора",
    "проект контракта",
    "форма кп",
    "коммерческое предложение",
    "смета",
    "локальный сметный расчет",
    "локальный сметный расчёт",
)
TECHNICAL_DETAIL_MARKERS = (
    "характеристик",
    "процессор",
    "оператив",
    "память",
    "накопител",
    "ssd",
    "hdd",
    "nvme",
    "raid",
    "порт",
    "ethernet",
    "sfp",
    "модель",
    "артикул",
    "сервер",
    "схд",
    "система хранения",
    "коммутатор",
    "маршрутизатор",
    "межсетевой экран",
)
PRODUCT_TABLE_MARKERS = (
    "наименование товара",
    "наименование оборудования",
    "наименование и характеристики",
    "количество",
    "кол-во",
    "ед. изм",
    "единица измерения",
)


def normalize_document_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def compact_reason(value: Any, *, limit: int = 260) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def has_any_marker(corpus: str, markers: tuple[str, ...]) -> bool:
    return any(marker in corpus for marker in markers)


def strong_technical_hint(*values: Any) -> bool:
    corpus = normalize_document_text(" ".join(str(value or "") for value in values))
    return bool(TZ_RE.search(corpus) or has_any_marker(corpus, STRONG_TECHNICAL_MARKERS))


def _candidate_text(candidate: dict[str, Any] | None, selection: dict[str, Any] | None) -> str:
    candidate = candidate or {}
    selection = selection or {}
    detection = candidate.get("technical_spec_detection")
    detection_text = ""
    if isinstance(detection, dict):
        detection_text = " ".join(
            str(value or "")
            for value in (
                detection.get("reason"),
                detection.get("source_hint"),
                " ".join(str(item or "") for item in detection.get("strong_headings") or []),
                " ".join(str(item or "") for item in detection.get("weak_headings") or []),
                " ".join(
                    str(section.get("title") or "")
                    for section in detection.get("technical_sections") or []
                    if isinstance(section, dict)
                ),
            )
        )

    smart_excerpt_text = ""
    for item in candidate.get("smart_excerpts") or []:
        if isinstance(item, dict):
            smart_excerpt_text += " " + str(item.get("trigger") or "")
            smart_excerpt_text += " " + str(item.get("text") or "")

    return normalize_document_text(
        "\n".join(
            str(value or "")
            for value in (
                selection.get("primary_document_title"),
                selection.get("primary_section_hint"),
                selection.get("primary_document_role"),
                candidate.get("title"),
                candidate.get("filename"),
                candidate.get("doc_type"),
                candidate.get("doc_type_reason"),
                candidate.get("header_excerpt"),
                candidate.get("excerpt"),
                smart_excerpt_text,
                detection_text,
            )
        )
    )


def _candidate_text_len(candidate: dict[str, Any] | None) -> int:
    if not candidate:
        return 0
    try:
        return int(float(candidate.get("text_len") or 0))
    except (TypeError, ValueError):
        return 0


def _candidate_detection(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {}
    detection = candidate.get("technical_spec_detection") or {}
    return detection if isinstance(detection, dict) else {}


def _has_product_or_technical_table(corpus: str, detection: dict[str, Any]) -> bool:
    matched = detection.get("matched_signals")
    matched = matched if isinstance(matched, dict) else {}
    return bool(
        detection.get("has_product_table")
        or detection.get("table_has_nomenclature")
        or detection.get("section_has_real_spec")
        or matched.get("filled_item_rows")
        or (
            has_any_marker(corpus, PRODUCT_TABLE_MARKERS)
            and (
                has_any_marker(corpus, TECHNICAL_DETAIL_MARKERS)
                or has_any_marker(corpus, ("цена", "стоимость", "сумма"))
            )
        )
    )


def classify_primary_document_quality(
    selection: dict[str, Any] | None = None,
    *,
    primary_candidate: dict[str, Any] | None = None,
    docs_count: int = 0,
    docs_with_text_count: int = 0,
) -> dict[str, Any]:
    selection = selection or {}
    primary_id = str(selection.get("primary_document_id") or "").strip()
    selector_status = str(
        selection.get("selector_status") or selection.get("status") or ""
    ).strip()

    if not primary_id:
        if docs_count <= 0:
            return {
                "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
                "primary_document_source_kind": DOCUMENT_SOURCE_MISSING,
                "primary_document_quality_reason": "No downloaded documents are available; report is card-only.",
                "primary_document_is_full_technical_spec": False,
                "primary_document_suspicious": False,
            }
        if docs_with_text_count <= 0:
            return {
                "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
                "primary_document_source_kind": DOCUMENT_SOURCE_CARD_ONLY,
                "primary_document_quality_reason": "Documents exist, but no extracted text is available; report is card-only.",
                "primary_document_is_full_technical_spec": False,
                "primary_document_suspicious": False,
            }
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
            "primary_document_source_kind": DOCUMENT_SOURCE_UNKNOWN,
            "primary_document_quality_reason": "Documents have text, but no primary technical source was selected.",
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": False,
        }

    text_len = _candidate_text_len(primary_candidate)
    detection = _candidate_detection(primary_candidate)
    corpus = _candidate_text(primary_candidate, selection)
    title_text = normalize_document_text(
        " ".join(
            str(value or "")
            for value in (
                selection.get("primary_document_title"),
                primary_candidate.get("title") if primary_candidate else "",
                primary_candidate.get("filename") if primary_candidate else "",
            )
        )
    )
    section_hint = normalize_document_text(selection.get("primary_section_hint"))

    if text_len <= 0:
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
            "primary_document_source_kind": DOCUMENT_SOURCE_CARD_ONLY,
            "primary_document_quality_reason": "Primary document has no extracted text.",
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": False,
        }

    if detection.get("blank_template"):
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
            "primary_document_source_kind": DOCUMENT_SOURCE_UNKNOWN,
            "primary_document_quality_reason": "Primary source looks like a blank specification template.",
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": False,
        }

    if detection.get("references_only"):
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
            "primary_document_source_kind": DOCUMENT_SOURCE_UNKNOWN,
            "primary_document_quality_reason": "Primary source only references a specification; it does not contain the specification itself.",
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": False,
        }

    title_is_pricing_or_bom = has_any_marker(title_text, PRICING_OR_BOM_MARKERS)
    title_is_contract_or_notice = has_any_marker(title_text, CONTRACT_OR_NOTICE_MARKERS)
    has_strong_title_or_section = strong_technical_hint(title_text, section_hint)
    has_strong_anywhere = strong_technical_hint(corpus)
    has_spec_marker = has_any_marker(corpus, SPECIFICATION_MARKERS)
    has_product_table = _has_product_or_technical_table(corpus, detection)
    has_technical_details = has_any_marker(corpus, TECHNICAL_DETAIL_MARKERS)

    suspicious_primary = bool(
        selector_status == "technical_spec_found"
        and has_any_marker(title_text, SUSPICIOUS_PRIMARY_TITLE_MARKERS)
        and not has_strong_title_or_section
    )

    if title_is_pricing_or_bom:
        confidence = (
            DOCUMENT_CONFIDENCE_MEDIUM
            if has_product_table or has_technical_details
            else DOCUMENT_CONFIDENCE_LOW
        )
        reason = (
            "Primary source is an NMCK/pricing/BOM document with item rows or technical hints; useful, but not a full TZ."
            if confidence == DOCUMENT_CONFIDENCE_MEDIUM
            else "Primary source is an NMCK/pricing document without enough technical characteristics."
        )
        return {
            "primary_document_confidence": confidence,
            "primary_document_source_kind": DOCUMENT_SOURCE_PRICING_OR_BOM,
            "primary_document_quality_reason": reason,
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": suspicious_primary,
        }

    if has_strong_title_or_section or (
        has_strong_anywhere and detection.get("technical_section_found")
    ):
        if has_any_marker(corpus, OBJECT_DESCRIPTION_MARKERS):
            source_kind = DOCUMENT_SOURCE_OBJECT_DESCRIPTION
            reason = "Primary source has an explicit object-description / OOZ technical section."
        elif has_any_marker(corpus, SPECIFICATION_MARKERS):
            source_kind = DOCUMENT_SOURCE_PRODUCT_SPECIFICATION
            reason = "Primary source has an explicit specification or technical-characteristics section."
        else:
            source_kind = DOCUMENT_SOURCE_TECHNICAL_SPEC
            reason = "Primary source has an explicit technical assignment / TZ section."
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_HIGH,
            "primary_document_source_kind": source_kind,
            "primary_document_quality_reason": reason,
            "primary_document_is_full_technical_spec": True,
            "primary_document_suspicious": False,
        }

    if has_spec_marker and (has_product_table or has_technical_details):
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_HIGH
            if detection.get("section_has_real_spec")
            else DOCUMENT_CONFIDENCE_MEDIUM,
            "primary_document_source_kind": DOCUMENT_SOURCE_PRODUCT_SPECIFICATION,
            "primary_document_quality_reason": "Primary source has a filled specification/product table, but no explicit full TZ heading.",
            "primary_document_is_full_technical_spec": bool(detection.get("section_has_real_spec")),
            "primary_document_suspicious": suspicious_primary,
        }

    if title_is_contract_or_notice:
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
            "primary_document_source_kind": DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
            "primary_document_quality_reason": "Primary source looks like a notice, protocol, contract, estimate, or form rather than technical requirements.",
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": suspicious_primary,
        }

    if has_product_table or has_technical_details:
        return {
            "primary_document_confidence": DOCUMENT_CONFIDENCE_MEDIUM,
            "primary_document_source_kind": DOCUMENT_SOURCE_PRODUCT_SPECIFICATION,
            "primary_document_quality_reason": "Primary source contains product/technical rows, but it is not clearly a full TZ or OOZ.",
            "primary_document_is_full_technical_spec": False,
            "primary_document_suspicious": suspicious_primary,
        }

    return {
        "primary_document_confidence": DOCUMENT_CONFIDENCE_LOW,
        "primary_document_source_kind": DOCUMENT_SOURCE_UNKNOWN,
        "primary_document_quality_reason": "Primary source does not show clear technical requirements in title, section hint, or extracted text.",
        "primary_document_is_full_technical_spec": False,
        "primary_document_suspicious": suspicious_primary,
    }


def normalize_document_quality_fields(values: dict[str, Any]) -> dict[str, Any]:
    confidence = str(values.get("primary_document_confidence") or "").strip()
    if confidence not in DOCUMENT_CONFIDENCES:
        confidence = DOCUMENT_CONFIDENCE_LOW
    source_kind = str(values.get("primary_document_source_kind") or "").strip()
    if source_kind not in DOCUMENT_SOURCE_KINDS:
        source_kind = DOCUMENT_SOURCE_UNKNOWN
    return {
        "primary_document_confidence": confidence,
        "primary_document_source_kind": source_kind,
        "primary_document_quality_reason": compact_reason(
            values.get("primary_document_quality_reason")
        ),
        "primary_document_is_full_technical_spec": bool(
            values.get("primary_document_is_full_technical_spec")
        ),
        "primary_document_suspicious": bool(values.get("primary_document_suspicious")),
    }
