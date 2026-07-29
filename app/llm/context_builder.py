from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.business_profile import (
    domestic_context_keywords,
    load_business_profile,
)
from app.config import settings
from app.document_risk_analyzer import classify_document_details, doc_type_text
from app.llm.analysis_depth import (
    ANALYSIS_DEPTH_CHOICES,
    ANALYSIS_DEPTH_STANDARD,
    normalize_analysis_depth,
)
from app.llm.spec_facts_extractor import build_spec_facts, render_spec_facts_markdown
from app.technical_spec_detector import TECHNICAL_SECTION_FOUND_REASON


OUTPUT_DIR = Path("/app/data/llm_contexts")
CONTENT_DETECTED_TECH_SPEC_REASONS = {
    TECHNICAL_SECTION_FOUND_REASON,
    "content_contains_technical_spec",
    "content_contains_supply_table",
    "price_doc_contains_supply_spec",
}


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(float(value))
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def money(value: Any) -> str:
    if value is None:
        return "не указана"

    if isinstance(value, Decimal):
        value = float(value)

    try:
        return f"{float(value):,.0f} ₽".replace(",", " ")
    except Exception:
        return str(value)


def short(value: str | None, limit: int = 500) -> str:
    if not value:
        return "не указано"

    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value

    return value[: limit - 1].rstrip() + "…"


def normalize(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def load_guardrail_business_profile() -> dict[str, Any]:
    try:
        return load_business_profile()
    except Exception:
        return {}


def domestic_registry_corpus(tender: dict, prepared_docs: list[dict[str, Any]]) -> str:
    full = raw_full(tender)
    short_data = raw_short(tender)
    chunks: list[str] = [
        tender.get("title") or "",
        tender.get("customer_name") or "",
        tender.get("region") or "",
        tender.get("law") or "",
        tender.get("procedure_type") or "",
        full.get("orderNumber") or "",
        full.get("deliveryPlace") or "",
        full.get("deliveryTerm") or "",
        full.get("tenderTypeName") or "",
        short_data.get("etpName") or "",
    ]

    document_risk = tender.get("document_risk_result") or {}
    if isinstance(document_risk, dict):
        for key in ("key_findings", "evidence"):
            value = document_risk.get(key)
            if isinstance(value, list):
                chunks.extend(str(item) for item in value)

    for doc in prepared_docs:
        chunks.append(str(doc.get("filename") or ""))
        chunks.append(str(doc.get("text") or "")[:60000])

    return "\n".join(str(chunk) for chunk in chunks if chunk)


def detect_domestic_registry_context(
    tender: dict,
    prepared_docs: list[dict[str, Any]],
) -> dict[str, Any]:
    profile = load_guardrail_business_profile()
    keywords = domestic_context_keywords(profile)
    corpus = normalize(domestic_registry_corpus(tender, prepared_docs))
    matched = [
        keyword
        for keyword in keywords
        if keyword and normalize(keyword) in corpus
    ]

    return {
        "detected": bool(matched),
        "source": "business_profile.rule_patterns.domestic_restrictions",
        "matched_signals": list(dict.fromkeys(matched))[:12],
    }


def extract_delivery_snippets(text: str, limit: int = 8) -> list[str]:
    patterns = [
        "срок поставки",
        "срок выполнения",
        "поставка товара",
        "в течение",
        "рабочих дней",
        "календарных дней",
        "до ",
    ]

    snippets: list[str] = []
    normalized = normalize(text)

    for pattern in patterns:
        start = 0
        while len(snippets) < limit:
            idx = normalized.find(pattern, start)
            if idx == -1:
                break

            left = max(0, idx - 350)
            right = min(len(text), idx + 700)
            snippet = " ".join(text[left:right].split())

            if snippet and snippet not in snippets:
                snippets.append(snippet)

            start = idx + len(pattern)

    return snippets


def extract_stop_factor_snippets(text: str, limit: int = 12) -> list[str]:
    patterns = [
        "авторизационное письмо",
        "письмо производителя",
        "письмо от производителя",
        "требования к участнику",
        "наличие опыта",
        "лицензия",
        "фстэк",
        "фсб",
        "сертификат",
        "обеспечение",
        "штраф",
        "пени",
        "приемка",
        "приёмка",
        "оплата",
    ]

    snippets: list[str] = []
    normalized = normalize(text)

    for pattern in patterns:
        start = 0
        while len(snippets) < limit:
            idx = normalized.find(normalize(pattern), start)
            if idx == -1:
                break

            left = max(0, idx - 300)
            right = min(len(text), idx + 700)
            snippet = " ".join(text[left:right].split())

            if snippet and snippet not in snippets:
                snippets.append(snippet)

            start = idx + len(pattern)

    return snippets


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text

    return (
        text[:limit]
        + "\n\n[ТЕКСТ ОБРЕЗАН ДЛЯ LLM-КОНТЕКСТА. В документе есть продолжение.]"
    )


def technical_sections_from_doc(doc: dict[str, Any]) -> list[dict[str, Any]]:
    detection = doc.get("technical_spec_detection") or {}
    if not isinstance(detection, dict):
        return []
    sections = detection.get("technical_sections") or []
    if not isinstance(sections, list):
        return []
    return [
        section
        for section in sections
        if isinstance(section, dict) and str(section.get("text") or "").strip()
    ]


def technical_context_text(doc: dict[str, Any]) -> str:
    sections = technical_sections_from_doc(doc)
    if not sections:
        return doc.get("text") or ""
    return "\n\n".join(str(section.get("text") or "").strip() for section in sections)


def trim_technical_sections(
    sections: list[dict[str, Any]],
    *,
    max_chars: int,
) -> list[dict[str, Any]]:
    trimmed: list[dict[str, Any]] = []
    remaining = max_chars

    for section in sections:
        if remaining <= 0:
            break
        text = str(section.get("text") or "")
        trimmed_text = trim_text(text, remaining)
        item = {
            key: value
            for key, value in section.items()
            if key != "text"
        }
        item["text"] = trimmed_text
        trimmed.append(item)
        remaining -= len(trimmed_text)

    return trimmed


def _selection_ids(selection: dict[str, Any] | None, key: str) -> list[str]:
    if not isinstance(selection, dict):
        return []
    values = selection.get(key) or []
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value or "").strip()]


def _selection_exclusion_reasons(selection: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(selection, dict):
        return {}
    excluded = selection.get("excluded_document_ids") or []
    if not isinstance(excluded, list):
        return {}
    result: dict[str, str] = {}
    for item in excluded:
        if not isinstance(item, dict):
            continue
        doc_id = str(item.get("id") or "").strip()
        if doc_id:
            result[doc_id] = str(item.get("reason") or "").strip()
    return result


def _prepared_doc_by_id(prepared_docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for doc in prepared_docs:
        doc_id = str(doc.get("id") or "").strip()
        if doc_id:
            result[doc_id] = doc
    return result


def _lead_document_text(doc: dict[str, Any]) -> str:
    if doc.get("doc_type") == "technical_spec":
        return technical_context_text(doc)
    return str(doc.get("text") or "")


def _lead_document_payload(
    doc: dict[str, Any],
    *,
    role: str,
    max_chars: int,
    section_hint: str = "",
) -> dict[str, Any]:
    return {
        "id": doc.get("id"),
        "title": doc.get("title") or doc.get("filename"),
        "filename": doc.get("filename"),
        "role": role,
        "section_hint": section_hint,
        "doc_type": doc.get("doc_type_text"),
        "doc_type_reason": doc.get("doc_type_reason"),
        "technical_spec_detection": doc.get("technical_spec_detection"),
        "text_len": doc.get("text_len"),
        "text": trim_text(_lead_document_text(doc), max_chars),
    }


def apply_lead_document_selection(
    package: dict[str, Any],
    prepared_docs: list[dict[str, Any]],
    *,
    document_selection: dict[str, Any] | None,
    max_spec_chars: int,
    max_other_chars: int,
) -> None:
    if not isinstance(document_selection, dict):
        return

    docs_by_id = _prepared_doc_by_id(prepared_docs)
    primary_id = str(document_selection.get("primary_document_id") or "").strip()
    primary_doc = docs_by_id.get(primary_id) if primary_id else None
    primary_role = str(document_selection.get("primary_document_role") or "unknown")
    secondary_ids = _selection_ids(document_selection, "secondary_document_ids")
    exclusion_reasons = _selection_exclusion_reasons(document_selection)

    package["document_selection"] = {
        key: value
        for key, value in document_selection.items()
        if key not in {"candidates", "usage"}
    }

    if primary_doc:
        package["lead_primary_document"] = _lead_document_payload(
            primary_doc,
            role=primary_role,
            max_chars=max_spec_chars,
            section_hint=str(document_selection.get("primary_section_hint") or ""),
        )
        package["lead_primary_document"].update(
            {
                "source_kind": document_selection.get("primary_document_source_kind"),
                "confidence": document_selection.get("primary_document_confidence"),
                "quality_reason": document_selection.get(
                    "primary_document_quality_reason"
                ),
                "is_full_technical_spec": document_selection.get(
                    "primary_document_is_full_technical_spec"
                ),
            }
        )
    else:
        package["lead_primary_document"] = None

    secondary_limit = max(1500, min(max_other_chars, 12000))
    secondary_docs = []
    for doc_id in secondary_ids:
        doc = docs_by_id.get(str(doc_id))
        if not doc:
            continue
        secondary_docs.append(
            _lead_document_payload(
                doc,
                role="supporting",
                max_chars=secondary_limit,
            )
        )
        if len(secondary_docs) >= 5:
            break
    package["lead_secondary_documents"] = secondary_docs

    selected_ids = {primary_id, *secondary_ids}
    other_docs = []
    for doc in prepared_docs:
        doc_id = str(doc.get("id") or "").strip()
        if not doc_id or doc_id in selected_ids:
            continue
        other_docs.append(
            {
                "id": doc_id,
                "title": doc.get("title") or doc.get("filename"),
                "filename": doc.get("filename"),
                "doc_type": doc.get("doc_type_text"),
                "doc_type_reason": doc.get("doc_type_reason"),
                "text_len": doc.get("text_len"),
                "selector_exclusion_reason": exclusion_reasons.get(doc_id, ""),
            }
        )
    package["lead_other_documents"] = other_docs[:30]


def get_tender_by_external_id(external_id: str) -> dict | None:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    t.*,
                    rb.score AS rule_score,
                    rb.recommendation AS rule_recommendation,
                    rb.confidence AS rule_confidence,
                    rb.result AS rule_result,
                    dr.score AS document_risk_score,
                    dr.recommendation AS document_risk_recommendation,
                    dr.confidence AS document_risk_confidence,
                    dr.result AS document_risk_result
                FROM tenders t
                LEFT JOIN analysis_results rb
                  ON rb.tender_id = t.id
                 AND rb.analysis_type = 'rule_based_score'
                LEFT JOIN analysis_results dr
                  ON dr.tender_id = t.id
                 AND dr.analysis_type = 'document_risk_analysis'
                WHERE t.source = 'zakupki360'
                  AND t.external_id = %s
                LIMIT 1;
                """,
                (external_id,),
            )
            return cur.fetchone()


def get_top_tender() -> dict | None:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    t.*,
                    rb.score AS rule_score,
                    rb.recommendation AS rule_recommendation,
                    rb.confidence AS rule_confidence,
                    rb.result AS rule_result,
                    dr.score AS document_risk_score,
                    dr.recommendation AS document_risk_recommendation,
                    dr.confidence AS document_risk_confidence,
                    dr.result AS document_risk_result
                FROM tenders t
                JOIN analysis_results rb
                  ON rb.tender_id = t.id
                 AND rb.analysis_type = 'rule_based_score'
                LEFT JOIN analysis_results dr
                  ON dr.tender_id = t.id
                 AND dr.analysis_type = 'document_risk_analysis'
                WHERE t.source = 'zakupki360'
                  AND rb.recommendation IN ('go', 'maybe')
                ORDER BY rb.score DESC, t.deadline_at ASC NULLS LAST
                LIMIT 1;
                """
            )
            return cur.fetchone()


def get_documents(tender_id: str) -> list[dict]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT *
                FROM documents
                WHERE tender_id = %s
                ORDER BY created_at ASC;
                """,
                (tender_id,),
            )
            return cur.fetchall()


def get_company_profile() -> dict | None:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT name, profile
                FROM company_profiles
                ORDER BY created_at DESC
                LIMIT 1;
                """
            )
            return cur.fetchone()


def raw_full(tender: dict) -> dict:
    raw = tender.get("raw") or {}
    full = raw.get("full") or {}
    return full if isinstance(full, dict) else {}


def raw_short(tender: dict) -> dict:
    raw = tender.get("raw") or {}
    short_data = raw.get("short") or {}
    return short_data if isinstance(short_data, dict) else {}


def customer_name(tender: dict, full: dict) -> str:
    if tender.get("customer_name"):
        return tender["customer_name"]

    placer = full.get("placerOrganization") or {}
    if isinstance(placer, dict):
        return placer.get("fullName") or placer.get("name") or "не указан"

    customers = full.get("customers") or []
    if customers:
        first = customers[0] or {}
        if isinstance(first, dict):
            return first.get("fullName") or first.get("name") or "не указан"

    return "не указан"



def build_llm_package(
    tender: dict,
    documents: list[dict],
    *,
    max_spec_chars: int,
    max_other_chars: int,
    analysis_depth: str = ANALYSIS_DEPTH_STANDARD,
    document_selection: dict[str, Any] | None = None,
) -> dict:
    analysis_depth = normalize_analysis_depth(analysis_depth)
    full = raw_full(tender)
    short_data = raw_short(tender)
    profile = get_company_profile()

    prepared_docs = []

    for doc in documents:
        text = doc.get("extracted_text") or ""
        filename = doc.get("filename") or "document"
        doc_details = classify_document_details(filename, text)
        doc_type = str(doc_details.get("doc_type") or "other")

        prepared_docs.append(
            {
                "id": str(doc.get("id")),
                "title": doc.get("title") or filename,
                "filename": filename,
                "doc_type": doc_type,
                "doc_type_text": doc_type_text(doc_type),
                "doc_type_reason": doc_details.get("doc_type_reason"),
                "technical_spec_detection": doc_details.get("technical_spec_detection"),
                "mime_type": doc.get("mime_type"),
                "size_bytes": doc.get("size_bytes"),
                "text_len": len(text),
                "text": text,
            }
        )

    spec_docs = [doc for doc in prepared_docs if doc["doc_type"] == "technical_spec"]
    price_docs = [doc for doc in prepared_docs if doc["doc_type"] == "price_calculation"]
    contract_docs = [doc for doc in prepared_docs if doc["doc_type"] == "contract"]
    app_req_docs = [doc for doc in prepared_docs if doc["doc_type"] == "application_requirements"]
    other_docs = [doc for doc in prepared_docs if doc["doc_type"] == "other"]

    spec_facts = build_spec_facts(
        tender_title=tender.get("title") or "",
        documents=[
            {
                **doc,
                "text": technical_context_text(doc),
            }
            for doc in spec_docs
        ],
    )
    domestic_context = detect_domestic_registry_context(tender, prepared_docs)

    delivery_snippets = []
    stop_factor_snippets = []
    deep_mode = analysis_depth == "deep"
    delivery_snippet_cap = 20 if deep_mode else 12
    stop_factor_snippet_cap = 28 if deep_mode else 16
    contract_snippet_limit = 12 if deep_mode else 6
    application_snippet_limit = 14 if deep_mode else 8
    other_preview_limit = min(max_other_chars, 8000 if deep_mode else 4000)

    for doc in spec_docs + contract_docs + app_req_docs:
        text = doc["text"] or ""
        if not text:
            continue

        for snippet in extract_delivery_snippets(text):
            delivery_snippets.append(
                {
                    "source_document": doc["filename"],
                    "doc_type": doc["doc_type_text"],
                    "snippet": snippet,
                }
            )

        for snippet in extract_stop_factor_snippets(text):
            stop_factor_snippets.append(
                {
                    "source_document": doc["filename"],
                    "doc_type": doc["doc_type_text"],
                    "snippet": snippet,
                }
            )

    package = {
        "meta": {
            "purpose": "LLM-first presales analysis package",
            "analysis_depth": analysis_depth,
            "context_limits": {
                "max_spec_chars": max_spec_chars,
                "max_other_chars": max_other_chars,
            },
            "context_strategy": [
                "1. ТЗ / описание объекта закупки / спецификация / перечень оборудования.",
                "2. НМЦК и ценовые таблицы с позициями оборудования.",
                "3. Проект контракта: только сроки, штрафы, оплата, приемка, обеспечение.",
                "4. Требования к заявке: участник, реестры, лицензии, сертификаты, опыт.",
                "5. Прочие документы: только полезные фрагменты и риск-сниппеты.",
            ],
            "analysis_focus": [
                "Главный фокус: ТЗ / описание объекта закупки / спецификация.",
                "Второй критический фактор: срок поставки и исполнимость.",
                "Остальные документы использовать только для стоп-факторов.",
            ],
            "generated_at": datetime.now().isoformat(),
        },
        "tender": {
            "id": str(tender["id"]),
            "source": tender.get("source"),
            "external_id": tender.get("external_id"),
            "title": tender.get("title"),
            "order_number": full.get("orderNumber") or tender.get("external_id"),
            "url": tender.get("url") or full.get("tenderUrl"),
            "customer_name": customer_name(tender, full),
            "customer_inn": (
                (full.get("placerOrganization") or {}).get("inn")
                if isinstance(full.get("placerOrganization"), dict)
                else None
            ),
            "region_or_delivery_place": tender.get("region") or full.get("deliveryPlace"),
            "initial_price": clean(tender.get("initial_price")),
            "initial_price_text": money(tender.get("initial_price")),
            "law": tender.get("law"),
            "procedure_type": tender.get("procedure_type") or full.get("tenderTypeName"),
            "stage": full.get("tenderStageName"),
            "platform": short_data.get("etpName"),
            "published_at": clean(tender.get("published_at")),
            "deadline_at": clean(tender.get("deadline_at")),
            "delivery_term_from_card": full.get("deliveryTerm"),
            "delivery_place_from_card": full.get("deliveryPlace"),
            "guarantee_order": clean(full.get("guaranteeOrder")),
            "guarantee_execute_contract": clean(full.get("guaranteeExecuteContract")),
        },
        "contact": {
            "person": full.get("contactPerson"),
            "phone": full.get("contactPhone"),
            "email": full.get("contactEMail"),
        },
        "current_rule_based_analysis": {
            "score": tender.get("rule_score"),
            "recommendation": tender.get("rule_recommendation"),
            "confidence": tender.get("rule_confidence"),
            "result": tender.get("rule_result"),
        },
        "current_document_risk_analysis": {
            "score": tender.get("document_risk_score"),
            "recommendation": tender.get("document_risk_recommendation"),
            "confidence": tender.get("document_risk_confidence"),
            "result": tender.get("document_risk_result"),
        },
        "domestic_registry_context": domestic_context,
        "company_profile": {
            "name": profile.get("name") if profile else None,
            "profile": profile.get("profile") if profile else None,
        },
        "documents_summary": [
            {
                "filename": doc["filename"],
                "doc_type": doc["doc_type_text"],
                "doc_type_reason": doc.get("doc_type_reason"),
                "technical_spec_note": (
                    "Техническая часть найдена внутри закупочной документации"
                    if doc.get("doc_type_reason")
                    in CONTENT_DETECTED_TECH_SPEC_REASONS
                    else None
                ),
                "technical_sections_count": len(technical_sections_from_doc(doc)),
                "technical_section_titles": [
                    str(section.get("title") or "")[:180]
                    for section in technical_sections_from_doc(doc)
                ],
                "technical_spec_reference_note": (
                    "Документ ссылается на спецификацию, но сама спецификация не найдена"
                    if (doc.get("technical_spec_detection") or {}).get("reason")
                    == "references_specification_only"
                    else None
                ),
                "text_len": doc["text_len"],
                "mime_type": doc["mime_type"],
            }
            for doc in prepared_docs
        ],
        "spec_facts": spec_facts,
        "technical_spec_documents": [
            {
                "filename": doc["filename"],
                "doc_type": doc["doc_type_text"],
                "doc_type_reason": doc.get("doc_type_reason"),
                "technical_spec_detection": doc.get("technical_spec_detection"),
                "text_len": doc["text_len"],
                "text": trim_text(technical_context_text(doc), max_spec_chars),
                "technical_sections": trim_technical_sections(
                    technical_sections_from_doc(doc),
                    max_chars=max_spec_chars,
                ),
            }
            for doc in spec_docs
        ],
        "price_documents": [
            {
                "filename": doc["filename"],
                "doc_type": doc["doc_type_text"],
                "text_len": doc["text_len"],
                "text": trim_text(doc["text"], max_other_chars),
            }
            for doc in price_docs
        ],
        "delivery_and_execution_snippets": delivery_snippets[:delivery_snippet_cap],
        "possible_stop_factor_snippets": stop_factor_snippets[:stop_factor_snippet_cap],
        "contract_documents_short": [
            {
                "filename": doc["filename"],
                "doc_type": doc["doc_type_text"],
                "text_len": doc["text_len"],
                "relevant_snippets": extract_delivery_snippets(
                    doc["text"],
                    limit=contract_snippet_limit,
                )
                + extract_stop_factor_snippets(
                    doc["text"],
                    limit=contract_snippet_limit,
                ),
            }
            for doc in contract_docs
        ],
        "application_requirements_short": [
            {
                "filename": doc["filename"],
                "doc_type": doc["doc_type_text"],
                "text_len": doc["text_len"],
                "relevant_snippets": extract_stop_factor_snippets(
                    doc["text"],
                    limit=application_snippet_limit,
                ),
            }
            for doc in app_req_docs
        ],
        "other_documents_short": [
            {
                "filename": doc["filename"],
                "doc_type": doc["doc_type_text"],
                "text_len": doc["text_len"],
                "relevant_snippets": extract_delivery_snippets(doc["text"], limit=4)
                + extract_stop_factor_snippets(doc["text"], limit=6),
                "text_preview": trim_text(doc["text"], other_preview_limit),
            }
            for doc in other_docs
        ],
        "llm_task": {
            "role": "Ты AI-пресейл-аналитик IT-интегратора.",
            "instruction": [
                "Основной анализ делай по ТЗ/описанию объекта закупки.",
                "Отдельно оцени срок поставки: реалистичный он или подозрительно короткий.",
                "Не раздувай риски из типовых юридических формулировок 44-ФЗ.",
                "Если утверждаешь риск заточки, укажи конкретные основания из ТЗ.",
                "Не придумывай факты. Если данных нет, прямо напиши, что нужно проверить вручную.",
                "Если в пакете обнаружен отечественный/реестровый контекст, не предлагай иностранные бренды как основной вариант.",
            ],
            "required_output": {
                "summary": "Краткое описание, что закупается.",
                "technical_spec_analysis": "Что требует ТЗ, какие ключевые характеристики, модели, вендоры, эквиваленты.",
                "delivery_feasibility": "Оценка сроков поставки и исполнимости.",
                "tailoring_risk": "low | medium | high с основаниями.",
                "potential_offer": "Что примерно может предложить интегратор.",
                "problems_in_tz": "Только реальные проблемы и неясности ТЗ: противоречия, отсутствующие данные, риски отклонения, барьеры, спорная эквивалентность/реестр; не повторять обычные требования.",
                "questions_to_customer": "3-7 вопросов заказчику.",
                "who_to_contact": "Контакт из карточки закупки.",
                "recommendation": "go | maybe | no_go.",
                "next_action": "Что сделать sales/pre-sale сегодня.",
            },
        },
    }

    apply_lead_document_selection(
        package,
        prepared_docs,
        document_selection=document_selection,
        max_spec_chars=max_spec_chars,
        max_other_chars=max_other_chars,
    )

    return package


def lead_package_to_markdown(package: dict) -> str:
    tender = package["tender"]
    contact = package.get("contact") or {}
    status = package.get("document_status") or {}
    primary_doc = package.get("lead_primary_document") or {}
    secondary_docs = package.get("lead_secondary_documents") or []
    other_docs = package.get("lead_other_documents") or package.get("documents_summary") or []
    profile = package.get("company_profile") or {}

    lines = [
        "# LLM package for customer lead report",
        "",
        "## 1. Lead report instructions",
        "",
        "Create a customer-development lead report. Triage has already decided that a full report is worth attempting; this report must refine the customer story using documents when available.",
        "Do not treat triage as the final commercial verdict. If document context is weak, lower confidence and make the next action include checking or obtaining the TZ / technical documentation.",
        "When a primary technical document is present, analyze it first: what is actually procured, current platform/vendor signals, account-development potential, registry/import-substitution constraints, technical customer, and questions for procurement and IT.",
        "",
        "## 2. Tender card and customer metadata",
        "",
        f"Title: {tender.get('title')}",
        f"Number: {tender.get('order_number')}",
        f"Customer: {tender.get('customer_name')}",
        f"Customer INN: {tender.get('customer_inn') or 'not specified'}",
        f"Price: {tender.get('initial_price_text')}",
        f"Law: {tender.get('law')}",
        f"Procedure: {tender.get('procedure_type')}",
        f"Stage: {tender.get('stage')}",
        f"Platform: {tender.get('platform') or 'not specified'}",
        f"Published at: {tender.get('published_at')}",
        f"Deadline at: {tender.get('deadline_at')}",
        f"Delivery place: {tender.get('delivery_place_from_card') or tender.get('region_or_delivery_place') or 'not specified'}",
        f"Delivery term from card: {tender.get('delivery_term_from_card') or 'not specified'}",
        f"URL: {tender.get('url')}",
        "",
        "Contact:",
        f"- Person: {contact.get('person') or 'not specified'}",
        f"- Phone: {contact.get('phone') or 'not specified'}",
        f"- Email: {contact.get('email') or 'not specified'}",
        "",
        "## 3. Document status",
        "",
        "```json",
        json.dumps(status, ensure_ascii=False, indent=2, default=str),
        "```",
        "",
    ]

    if status.get("warning"):
        lines.extend(["Warning:", str(status.get("warning")), ""])
    if status.get("primary_document_confidence") in {"medium", "low"}:
        lines.extend(
            [
                "Primary source confidence note:",
                (
                    f"confidence={status.get('primary_document_confidence')}; "
                    f"source_kind={status.get('primary_document_source_kind')}; "
                    f"is_full_technical_spec={str(bool(status.get('primary_document_is_full_technical_spec'))).lower()}; "
                    f"reason={status.get('primary_document_quality_reason') or 'not specified'}"
                ),
                "Distinguish confirmed document facts from card-based assumptions.",
                "",
            ]
        )

    lines.extend(["## 4. Primary technical document text", ""])
    if isinstance(primary_doc, dict) and primary_doc:
        lines.extend(
            [
                f"Document: {primary_doc.get('title') or primary_doc.get('filename')}",
                f"Role: {primary_doc.get('role') or 'unknown'}",
                f"Section hint: {primary_doc.get('section_hint') or 'not specified'}",
                f"Source kind: {primary_doc.get('source_kind') or 'unknown'}",
                f"Confidence: {primary_doc.get('confidence') or 'unknown'}",
                f"Full technical spec: {str(bool(primary_doc.get('is_full_technical_spec'))).lower()}",
                f"Quality reason: {primary_doc.get('quality_reason') or 'not specified'}",
                f"Detection: {primary_doc.get('doc_type_reason') or 'not specified'}",
                "",
                str(primary_doc.get("text") or ""),
                "",
            ]
        )
    else:
        lines.extend(
            [
                "No primary technical document text is available in this package.",
                "Use the tender card only as a preliminary signal and avoid categorical conclusions about supply composition, platform, tailoring, or exact technical requirements.",
                "",
            ]
        )

    lines.extend(["## 5. Secondary supporting excerpts", ""])
    if secondary_docs:
        for doc in secondary_docs:
            if not isinstance(doc, dict):
                continue
            lines.extend(
                [
                    f"### {doc.get('title') or doc.get('filename')}",
                    f"Role: {doc.get('role') or 'supporting'}; type: {doc.get('doc_type') or 'unknown'}",
                    "",
                    str(doc.get("text") or ""),
                    "",
                ]
            )
    else:
        lines.append("No secondary supporting document excerpts selected.")

    lines.extend(["", "## 6. Other document inventory", ""])
    if other_docs:
        for doc in other_docs:
            if not isinstance(doc, dict):
                continue
            reason = doc.get("selector_exclusion_reason") or doc.get("doc_type_reason") or ""
            suffix = f" | reason: {reason}" if reason else ""
            lines.append(
                f"- {doc.get('title') or doc.get('filename')} | type: {doc.get('doc_type')} | text_len: {doc.get('text_len')}{suffix}"
            )
    else:
        lines.append("No other documents with extracted text.")

    lines.extend(
        [
            "",
            "## 7. Business and profile context",
            "",
            "Current rule-based analysis:",
            "```json",
            json.dumps(package.get("current_rule_based_analysis") or {}, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
            "Current document risk analysis:",
            "```json",
            json.dumps(package.get("current_document_risk_analysis") or {}, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
            "Domestic / registry context:",
            "```json",
            json.dumps(package.get("domestic_registry_context") or {}, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
            "Company profile:",
            "```json",
            json.dumps(profile, ensure_ascii=False, indent=2, default=str),
            "```",
            "",
        ]
    )

    return "\n".join(lines)


def package_to_markdown(package: dict) -> str:
    if (package.get("meta") or {}).get("report_kind") == "lead":
        return lead_package_to_markdown(package)

    tender = package["tender"]
    contact = package["contact"]
    domestic_context = package.get("domestic_registry_context") or {}

    spec_facts_markdown = render_spec_facts_markdown(package.get("spec_facts") or {})
    priority_spec_lines = spec_facts_markdown.splitlines() if spec_facts_markdown else []
    if priority_spec_lines:
        priority_spec_lines.append("")

    lines = [
        "# LLM-пакет для анализа закупки",
        "",
        "## Инструкция для LLM",
        "",
        f"Режим анализа: {package.get('meta', {}).get('analysis_depth') or 'standard'}",
        "",
        "Ты AI-пресейл-аналитик IT-интегратора. Главный фокус анализа - ТЗ и срок поставки. Остальные документы используй только для поиска стоп-факторов.",
        "",
        "Нужно подготовить пресейл-отчёт: что закупается, насколько это подходит интегратору, признаки заточки, что можно предложить, какие вопросы задать заказчику и финальный go/maybe/no_go.",
        "",
        "## Карточка закупки",
        "",
        f"Название: {tender.get('title')}",
        f"Номер: {tender.get('order_number')}",
        f"Заказчик: {tender.get('customer_name')}",
        f"ИНН заказчика: {tender.get('customer_inn') or 'не указан'}",
        f"НМЦК: {tender.get('initial_price_text')}",
        f"Закон: {tender.get('law')}",
        f"Тип процедуры: {tender.get('procedure_type')}",
        f"Этап: {tender.get('stage')}",
        f"Площадка: {tender.get('platform') or 'не указана'}",
        f"Публикация: {tender.get('published_at')}",
        f"Подача до: {tender.get('deadline_at')}",
        f"Место поставки: {tender.get('delivery_place_from_card') or tender.get('region_or_delivery_place') or 'не указано'}",
        f"Срок поставки из карточки: {tender.get('delivery_term_from_card') or 'не указан'}",
        f"Обеспечение заявки: {tender.get('guarantee_order') or 'не указано'}",
        f"Обеспечение контракта: {tender.get('guarantee_execute_contract') or 'не указано'}",
        f"Ссылка: {tender.get('url')}",
        "",
        "## Контакт заказчика",
        "",
        f"ФИО: {contact.get('person') or 'не указано'}",
        f"Телефон: {contact.get('phone') or 'не указан'}",
        f"Email: {contact.get('email') or 'не указан'}",
        "",
        *priority_spec_lines,
        "## Текущий rule-based анализ",
        "",
        "```json",
        json.dumps(package["current_rule_based_analysis"], ensure_ascii=False, indent=2, default=str),
        "```",
        "",
        "## Текущий анализ документов правилами",
        "",
        "```json",
        json.dumps(package["current_document_risk_analysis"], ensure_ascii=False, indent=2, default=str),
        "```",
        "",
    ]

    if isinstance(domestic_context, dict) and domestic_context.get("detected"):
        matched = domestic_context.get("matched_signals") or []
        matched_text = ", ".join(str(item) for item in matched[:8]) or "есть признаки в ТЗ/документах"
        lines.extend(
            [
                "## Отечественный / реестровый контекст",
                "",
                f"Обнаружены признаки: {matched_text}.",
                "",
                "Правила для LLM:",
                "- Не предлагай Dell, HPE, HP, Lenovo, Huawei, Cisco, Supermicro, Inspur и другие иностранные бренды как основной вариант.",
                "- В разделе «Что можно предложить» используй российское, реестровое или допустимое по ТЗ оборудование.",
                "- Формулируй осторожно: конкретный вендор только после проверки ТЗ, реестра и канала.",
                "- Импортные бренды можно упоминать только как риск/ограничение или как вариант, который нельзя предлагать без подтверждения допустимости иностранного происхождения.",
                "- Безопасная формулировка: российское или реестровое оборудование; YADRO, Aquarius, DEPO, Kraftway, Рикор, Fplus, Норси-Транс, Аэродиск или другой производитель при подтверждении соответствия ТЗ и реестру.",
                "",
            ]
        )

    lines.extend(["## Сводка документов", ""])

    for doc in package["documents_summary"]:
        note = f" | {doc['technical_spec_note']}" if doc.get("technical_spec_note") else ""
        if doc.get("technical_spec_reference_note"):
            note = f" | {doc['technical_spec_reference_note']}"
        lines.append(f"- {doc['filename']} | {doc['doc_type']}{note} | {doc['text_len']} символов")

    available_document_titles = package.get("available_document_titles") or []
    if available_document_titles:
        lines.extend(["", "## Available document titles from tender card", ""])
        for doc in available_document_titles:
            if not isinstance(doc, dict):
                continue
            title = doc.get("title") or doc.get("filename") or "document"
            doc_id = doc.get("id")
            suffix = f" | id={doc_id}" if doc_id else ""
            lines.append(f"- {title}{suffix}")

    technical_spec_documents = package["technical_spec_documents"]
    spec_reference_only = any(
        doc.get("technical_spec_reference_note")
        for doc in package.get("documents_summary") or []
        if isinstance(doc, dict)
    )

    if technical_spec_documents:
        lines.extend(["", "## ТЗ / описание объекта закупки / спецификация", ""])

        for doc in technical_spec_documents:
            lines.extend([f"### Документ: {doc['filename']}", ""])
            sections = doc.get("technical_sections") or []
            if sections:
                for section in sections:
                    title = section.get("title") or "технический раздел"
                    reason = section.get("reason") or doc.get("doc_type_reason")
                    lines.extend(
                        [
                            f"### Найденный раздел: {title}",
                            f"Причина: {reason}",
                            "",
                            section.get("text") or "",
                            "",
                        ]
                    )
            else:
                lines.extend([doc["text"], ""])
    else:
        lines.extend(
            [
                "",
                "## Техническая спецификация не найдена в доступных документах",
                "",
                "Ниже приведены связанные фрагменты из доступных документов: договор, извещение, требования или документы, где упоминаются поставка/спецификация. Это не полноценное ТЗ.",
            ]
        )
        if spec_reference_only:
            lines.append(
                "Спецификация упоминается в документах, но сама заполненная таблица спецификации не найдена."
            )

    lines.extend(["", "## Фрагменты про сроки поставки и исполнимость", ""])

    if not package["delivery_and_execution_snippets"]:
        lines.append("Фрагменты про сроки поставки не найдены.")

    for item in package["delivery_and_execution_snippets"]:
        lines.extend(
            [
                f"Источник: {item['source_document']} ({item['doc_type']})",
                f"> {item['snippet']}",
                "",
            ]
        )

    lines.extend(["", "## НМЦК / расчёт цены", ""])

    if not package["price_documents"]:
        lines.append("Документы НМЦК не найдены.")

    for doc in package["price_documents"]:
        lines.extend(
            [
                f"### Документ: {doc['filename']}",
                "",
                doc["text"],
                "",
            ]
        )

    lines.extend(["", "## Возможные стоп-факторы из контракта и требований к заявке", ""])

    if not package["possible_stop_factor_snippets"]:
        lines.append("Потенциальные стоп-факторы не найдены.")

    for item in package["possible_stop_factor_snippets"]:
        lines.extend(
            [
                f"Источник: {item['source_document']} ({item['doc_type']})",
                f"> {item['snippet']}",
                "",
            ]
        )

    lines.extend(["", "## Прочие документы: полезные фрагменты", ""])

    if not package["other_documents_short"]:
        lines.append("Прочие документы отсутствуют.")

    for doc in package["other_documents_short"]:
        lines.extend([f"### Документ: {doc['filename']}", ""])
        snippets = doc.get("relevant_snippets") or []
        if snippets:
            for snippet in snippets[:10]:
                lines.extend([f"> {snippet}", ""])
        elif doc.get("text_preview"):
            lines.extend([doc["text_preview"], ""])
        else:
            lines.append("Полезные фрагменты не найдены.")

    lines.extend(
        [
            "",
            "## Требуемый формат ответа LLM",
            "",
            "Верни структурированный отчёт:",
            "",
            "1. Краткое резюме.",
            "2. Анализ ТЗ.",
            "3. Сроки поставки и исполнимость.",
            "4. Признаки заточки.",
            "5. Что можно предложить.",
            "6. Проблемы и неясности ТЗ.",
            "7. Вопросы заказчику.",
            "8. Кому звонить.",
            "9. Вердикт: go / maybe / no_go.",
            "10. Следующее действие для sales/pre-sale.",
            "",
        ]
    )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build LLM analysis package for a tender")
    parser.add_argument("--external-id", help="Zakupki360 external order id")
    parser.add_argument(
        "--analysis-depth",
        choices=ANALYSIS_DEPTH_CHOICES,
        default=ANALYSIS_DEPTH_STANDARD,
    )
    parser.add_argument("--max-spec-chars", type=int, default=90000)
    parser.add_argument("--max-other-chars", type=int, default=20000)
    args = parser.parse_args()

    tender = get_tender_by_external_id(args.external_id) if args.external_id else get_top_tender()

    if not tender:
        raise RuntimeError("Tender not found")

    documents = get_documents(str(tender["id"]))

    package = build_llm_package(
        tender,
        documents,
        max_spec_chars=args.max_spec_chars,
        max_other_chars=args.max_other_chars,
        analysis_depth=args.analysis_depth,
    )

    external_id = tender["external_id"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    json_path = OUTPUT_DIR / f"{external_id}.json"
    md_path = OUTPUT_DIR / f"{external_id}.md"

    json_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    md_path.write_text(
        package_to_markdown(package),
        encoding="utf-8",
    )

    print(f"LLM package created:")
    print(f"- {json_path}")
    print(f"- {md_path}")
    print()
    print(f"Technical spec docs: {len(package['technical_spec_documents'])}")
    print(f"Delivery snippets: {len(package['delivery_and_execution_snippets'])}")
    print(f"Stop factor snippets: {len(package['possible_stop_factor_snippets'])}")


if __name__ == "__main__":
    main()
