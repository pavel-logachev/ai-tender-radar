from __future__ import annotations

import argparse
import re
from collections import Counter
from functools import lru_cache
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import settings
from app.business_profile import load_business_profile
from app.technical_spec_detector import (
    TECHNICAL_SECTION_FOUND_REASON,
    detect_technical_spec_content,
)


CONTENT_DETECTED_TECH_SPEC_REASONS = {
    TECHNICAL_SECTION_FOUND_REASON,
    "content_contains_technical_spec",
    "content_contains_supply_table",
    "price_doc_contains_supply_spec",
}


EQUIVALENT_PATTERNS = [
    "или эквивалент",
    "эквивалент",
    "эквивалентный",
    "эквивалентная",
    "эквивалентное",
]

AUTHORIZATION_PATTERNS = [
    "авторизационное письмо",
    "авторизационное письмо производителя",
    "письмо производителя",
    "письмо от производителя",
    "подтверждение производителя",
    "официальный партнер",
    "официального партнера",
    "статус партнера",
    "дилерское письмо",
    "сертификат партнера",
]

COMPATIBILITY_PATTERNS = [
    "совместимость с",
    "совместимо с",
    "имеющимся у заказчика",
    "имеющегося у заказчика",
    "установленным у заказчика",
    "используемым у заказчика",
    "инв№",
    "инв. №",
    "инвентар",
]

CERT_PATTERNS = [
    "фстэк",
    "фсб",
    "скзи",
    "средства доверенной загрузки",
    "сертификат соответствия",
    "сертификаты соответствия",
]

DELIVERY_PATTERNS = [
    "срок поставки",
    "срок выполнения",
    "рабочих дней",
    "календарных дней",
]

RESTRICTIVE_PATTERNS = [
    "замена не допускается",
    "без замены",
    "только ",
    "только:",
    "не допускается",
]

KNOWN_VENDOR_PATTERNS = [
    "cisco",
    "huawei",
    "juniper",
    "mikrotik",
    "d-link",
    "eltex",
    "qtech",
    "h3c",
    "hp",
    "hpe",
    "dell",
    "lenovo",
    "supermicro",
    "aquarius",
    "аквариус",
    "kraftway",
    "depo",
    "депо",
    "usergate",
    "код безопасности",
    "инфотекс",
    "vipnet",
    "континент",
    "криптопро",
    "astra linux",
    "ред ос",
    "по линия",
    "видеосервер линия",
]

MODEL_REGEXES = [
    re.compile(r"\b[A-Z]{1,6}[0-9]{2,5}[A-Z0-9-]{0,12}\b"),
    re.compile(r"\b[0-9]{2,5}[A-Z]{1,6}[0-9A-Z-]{0,12}\b"),
    re.compile(r"\b[A-Z0-9]{2,12}-[A-Z0-9-]{2,24}\b"),
]

NOISE_MODEL_VALUES = {
    "ГОСТ",
    "ФЗ",
    "ОКПД",
    "НМЦК",
    "НДС",
    "ЕИС",
}


def normalize(text: str | None) -> str:
    return (text or "").lower().replace("ё", "е")


def make_snippet(text: str, start: int, end: int, radius: int = 180) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = text[left:right]
    snippet = " ".join(snippet.split())
    return snippet[:520]


def technical_sections_from_detection(detection: Any) -> list[dict[str, Any]]:
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


def technical_context_text(text: str, detection: Any) -> str:
    sections = technical_sections_from_detection(detection)
    if not sections:
        return text
    return "\n\n".join(str(section.get("text") or "").strip() for section in sections)


def classify_document_details(filename: str | None, text: str | None) -> dict[str, Any]:
    filename_src = normalize(filename or "")
    text_head = normalize(text[:2000] if text else "")
    src = f"{filename_src}\n{text_head}"
    content_detection = detect_technical_spec_content(filename, text)

    if any(
        x in filename_src
        for x in ["описание объекта закупки", "техническое задание", "техническая часть"]
    ) or re.search(r"(^|[\s_.-])тз($|[\s_.-])", filename_src):
        return {
            "doc_type": "technical_spec",
            "doc_type_reason": "filename_or_header_contains_technical_spec",
            "technical_spec_detection": content_detection,
        }

    if content_detection.get("detected"):
        return {
            "doc_type": "technical_spec",
            "doc_type_reason": content_detection.get("reason") or "content_contains_technical_spec",
            "technical_spec_detection": content_detection,
        }

    contract_filename_patterns = [
        "проект контракта",
        "проект_контракта",
        "проект договора",
        "проект_договора",
    ]
    contract_head_patterns = [
        "контракт №",
        "контракт n",
        "договор №",
        "договор n",
        "договор на поставку",
    ]

    if any(x in filename_src for x in contract_filename_patterns):
        return {
            "doc_type": "contract",
            "doc_type_reason": "filename_contains_contract",
            "technical_spec_detection": content_detection,
        }

    if text_head.strip().startswith(("договор", "контракт")):
        return {
            "doc_type": "contract",
            "doc_type_reason": "text_head_contains_contract",
            "technical_spec_detection": content_detection,
        }

    if any(x in text_head[:500] for x in contract_head_patterns):
        return {
            "doc_type": "contract",
            "doc_type_reason": "text_head_contains_contract",
            "technical_spec_detection": content_detection,
        }

    if any(x in src for x in ["требования к содержанию", "составу заявки", "инструкция по заполнению", "требование_к_заявке"]):
        return {
            "doc_type": "application_requirements",
            "doc_type_reason": "content_contains_application_requirements",
            "technical_spec_detection": content_detection,
        }

    if any(x in src for x in ["нмцк", "обоснование", "расчет цены", "расчёт", "обоснование цены"]):
        return {
            "doc_type": "price_calculation",
            "doc_type_reason": "filename_or_header_contains_price_calculation",
            "technical_spec_detection": content_detection,
        }

    return {
        "doc_type": "other",
        "doc_type_reason": "no_specific_document_type_detected",
        "technical_spec_detection": content_detection,
    }


def classify_document(filename: str | None, text: str | None) -> str:
    return str(classify_document_details(filename, text).get("doc_type") or "other")


DEFAULT_DOC_TYPE_WEIGHTS = {
    "technical_spec": 1.0,
    "contract": 0.35,
    "application_requirements": 0.15,
    "price_calculation": 0.25,
    "other": 0.4,
}


@lru_cache(maxsize=1)
def document_risk_profile() -> dict[str, Any]:
    try:
        profile = load_business_profile()
    except Exception:
        return {}

    cfg = profile.get("document_risk") or {}
    return cfg if isinstance(cfg, dict) else {}


def document_risk_threshold(name: str, default: int) -> int:
    thresholds = document_risk_profile().get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return default
    try:
        return int(thresholds.get(name, default))
    except (TypeError, ValueError):
        return default


def signal_weight(name: str, default: int) -> int:
    signals = document_risk_profile().get("signals") or {}
    if not isinstance(signals, dict):
        return default

    cfg = signals.get(name) or {}
    if not isinstance(cfg, dict):
        return default

    try:
        return int(cfg.get("weight", default))
    except (TypeError, ValueError):
        return default


def doc_type_weight(doc_type: str) -> float:
    weights = document_risk_profile().get("doc_type_weights") or {}
    if not isinstance(weights, dict):
        weights = {}

    default = DEFAULT_DOC_TYPE_WEIGHTS.get(doc_type, 0.4)
    try:
        return float(weights.get(doc_type, default))
    except (TypeError, ValueError):
        return default


def doc_type_text(doc_type: str) -> str:
    return {
        "technical_spec": "ТЗ / описание объекта закупки",
        "contract": "проект контракта",
        "application_requirements": "требования к заявке",
        "price_calculation": "НМЦК / расчёт цены",
        "other": "прочий документ",
    }.get(doc_type, doc_type)


def find_keyword_snippets(text: str, patterns: list[str], limit: int = 5) -> list[dict[str, str]]:
    normalized = normalize(text)
    results = []

    for pattern in patterns:
        normalized_pattern = normalize(pattern)
        start = 0

        while len(results) < limit:
            idx = normalized.find(normalized_pattern, start)
            if idx == -1:
                break

            results.append(
                {
                    "pattern": pattern,
                    "snippet": make_snippet(text, idx, idx + len(pattern)),
                }
            )
            start = idx + len(pattern)

    return results


def find_model_like_mentions(text: str, limit: int = 15) -> list[str]:
    found = []

    for regex in MODEL_REGEXES:
        for match in regex.finditer(text):
            value = match.group(0)

            if value.upper() in NOISE_MODEL_VALUES:
                continue

            if value.isdigit():
                continue

            # Отсекаем даты и типовые номера, насколько возможно.
            if re.fullmatch(r"\d{2}-\d{2}-\d{2,4}", value):
                continue

            if value not in found:
                found.append(value)

            if len(found) >= limit:
                return found

    return found


def extract_short_delivery_days(text: str) -> list[dict[str, Any]]:
    results = []

    patterns = [
        re.compile(r"(\d{1,3})\s+(рабочих|рабочий|рабочие)\s+дн", re.IGNORECASE),
        re.compile(r"(\d{1,3})\s+(календарных|календарный|календарные)\s+дн", re.IGNORECASE),
    ]

    for regex in patterns:
        for match in regex.finditer(text):
            days = int(match.group(1))
            snippet = make_snippet(text, match.start(), match.end(), radius=220)
            normalized_snippet = normalize(snippet)

            is_delivery_context = any(
                phrase in normalized_snippet
                for phrase in [
                    "срок поставки",
                    "поставка товара",
                    "поставить товар",
                    "поставки товара",
                    "срок выполнения",
                    "оказания услуг",
                    "выполнения работ",
                ]
            )

            is_legal_noise = any(
                phrase in normalized_snippet
                for phrase in [
                    "независимой гарантии",
                    "требование заказчика об уплате",
                    "денежной суммы",
                    "банковской гарантии",
                ]
            )

            if days <= 10 and is_delivery_context and not is_legal_noise:
                results.append(
                    {
                        "days": days,
                        "snippet": snippet,
                    }
                )

            if len(results) >= 5:
                return results

    return results


def is_manufacturer_auth_legal_noise(snippet: str | None) -> bool:
    normalized = normalize(snippet)

    return any(
        phrase in normalized
        for phrase in [
            "обоснование предлагаемых цены контракта",
            "обоснование предлагаемой цены контракта",
            "суммы цен единиц товара",
            "гарантийное письмо от производителя с указанием цены",
            "предусмотренными настоящей статьей",
        ]
    )


def is_restrictive_legal_noise(snippet: str | None) -> bool:
    normalized = normalize(snippet)

    return any(
        phrase in normalized
        for phrase in [
            "пустые ячейки заполнению не подлежат",
            "вносить изменения в наименование товара",
            "инструкция по заполнению",
            "независимой гарантии",
            "уменьшение не допускается",
            "представитель поставщика к приемке товара не допускается",
            "могут быть только субъекты малого и среднего предпринимательства",
            "только субъекты малого и среднего предпринимательства",
            "только субъектов малого и среднего предпринимательства",
            "только субъекты мсп",
            "участниками которого могут быть только субъекты",
            "участниками которой могут быть только субъекты",
            "только после получения отказа",
            "возмещения только фактически понесенного ущерба",
            "подача заявок на участие в аукционе осуществляется только лицами",
            "только лицами, получившими аккредитацию",
            "участник конкурентной закупки вправе подать только одну заявку",
            "подать только одну заявку",
            "не допускается указание в первой части заявки",
            "сведений об участнике",
        ]
    )


def add_evidence(
    evidence: list[dict[str, Any]],
    *,
    signal_type: str,
    severity: str,
    doc: dict[str, Any],
    pattern: str | None = None,
    snippet: str | None = None,
    values: list[str] | None = None,
) -> None:
    item: dict[str, Any] = {
        "type": signal_type,
        "severity": severity,
        "source_document": doc.get("filename"),
        "doc_type": doc.get("doc_type"),
    }

    if pattern:
        item["pattern"] = pattern

    if snippet:
        item["snippet"] = snippet

    if values:
        item["values"] = values

    evidence.append(item)


def analyze_tender_docs(tender: dict, docs: list[dict]) -> dict[str, Any]:
    prepared_docs = []

    for doc in docs:
        text = doc.get("extracted_text") or ""
        filename = doc.get("filename") or "document"

        if not text:
            continue

        doc_details = classify_document_details(filename, text)
        doc_type = str(doc_details.get("doc_type") or "other")

        prepared_docs.append(
            {
                **doc,
                "text": text,
                "doc_type": doc_type,
                "doc_type_reason": doc_details.get("doc_type_reason"),
                "technical_spec_detection": doc_details.get("technical_spec_detection"),
                "technical_spec_text": technical_context_text(
                    text,
                    doc_details.get("technical_spec_detection"),
                ),
            }
        )

    if not prepared_docs:
        return {
            "tender_title": tender.get("title"),
            "documents_count": len(docs),
            "documents_with_text": 0,
            "total_text_chars": 0,
            "tailoring_risk": "unknown",
            "risk_score": 0,
            "recommendation": "no_text_extracted",
            "key_findings": ["Нет извлеченного текста для анализа документов."],
            "evidence": [],
            "doc_summaries": [],
            "legal_noise": [],
            "manual_checks": [
                "Скачать документы закупки.",
                "Извлечь текст из ТЗ и проекта контракта.",
                "После извлечения текста повторить анализ признаков заточки.",
            ],
        }

    doc_priority = {
        "technical_spec": 0,
        "price_calculation": 1,
        "other": 2,
        "contract": 3,
        "application_requirements": 4,
    }
    prepared_docs.sort(key=lambda d: doc_priority.get(d["doc_type"], 9))

    evidence: list[dict[str, Any]] = []
    legal_noise: list[dict[str, Any]] = []
    key_findings: list[str] = []
    risk_score = 0

    total_text_chars = sum(len(doc["text"]) for doc in prepared_docs)
    doc_type_counter = Counter(doc["doc_type"] for doc in prepared_docs)

    spec_docs = [doc for doc in prepared_docs if doc["doc_type"] == "technical_spec"]
    spec_text = "\n".join(
        doc.get("technical_spec_text") or doc["text"]
        for doc in spec_docs
    )

    content_detected_spec_docs = [
        doc
        for doc in spec_docs
        if doc.get("doc_type_reason")
        in CONTENT_DETECTED_TECH_SPEC_REASONS
    ]
    for doc in content_detected_spec_docs:
        key_findings.append(
            f"Техническая часть найдена внутри документа: {doc['filename']}."
        )

    has_equivalent_in_spec = bool(find_keyword_snippets(spec_text, EQUIVALENT_PATTERNS, limit=1))

    if spec_docs and has_equivalent_in_spec:
        risk_score += signal_weight("equivalent_in_spec", -10)
        key_findings.append("В ТЗ найдены упоминания эквивалента.")
    elif spec_docs and not has_equivalent_in_spec:
        risk_score += signal_weight("no_equivalent_in_spec", 25)
        key_findings.append("В ТЗ не найдена фраза 'или эквивалент'.")

    for doc in prepared_docs:
        text = doc["text"]
        doc_type = doc["doc_type"]
        weight = doc_type_weight(doc_type)

        # 1. Совместимость с имеющейся инфраструктурой заказчика - сильный сигнал.
        compatibility = find_keyword_snippets(text, COMPATIBILITY_PATTERNS, limit=3)
        if compatibility:
            delta = int(signal_weight("compatibility_with_existing_infra", 30) * weight)
            risk_score += delta
            key_findings.append(
                f"Есть признаки привязки к имеющейся инфраструктуре заказчика в документе: {doc['filename']}."
            )
            for item in compatibility[:2]:
                add_evidence(
                    evidence,
                    signal_type="compatibility_with_existing_infra",
                    severity="high" if doc_type == "technical_spec" else "medium",
                    doc=doc,
                    pattern=item["pattern"],
                    snippet=item["snippet"],
                )

        # 2. Конкретные вендоры/продукты.
        vendors = find_keyword_snippets(text, KNOWN_VENDOR_PATTERNS, limit=5)
        if vendors:
            delta = int(signal_weight("known_vendor_or_product", 18) * weight)
            risk_score += delta
            key_findings.append(
                f"Найдены упоминания вендоров/продуктов в документе: {doc['filename']}."
            )
            for item in vendors[:3]:
                add_evidence(
                    evidence,
                    signal_type="known_vendor_or_product",
                    severity="medium" if doc_type == "technical_spec" else "low",
                    doc=doc,
                    pattern=item["pattern"],
                    snippet=item["snippet"],
                )

        # 3. Модели и артикулы.
        model_mentions = find_model_like_mentions(text, limit=12)
        if model_mentions:
            if doc_type in {"contract", "application_requirements"}:
                legal_noise.append(
                    {
                        "type": "model_like_legal_noise",
                        "source_document": doc.get("filename"),
                        "doc_type": doc_type,
                        "values": model_mentions[:12],
                    }
                )
            else:
                delta = int(signal_weight("model_like_mentions", 20) * weight)
                risk_score += delta
                key_findings.append(
                    f"Найдены похожие на модели/артикулы обозначения в документе: {doc['filename']}."
                )
                add_evidence(
                    evidence,
                    signal_type="model_like_mentions",
                    severity="medium" if doc_type == "technical_spec" else "low",
                    doc=doc,
                    values=model_mentions[:12],
                )

        # 4. Авторизация/письмо производителя.
        auth = find_keyword_snippets(text, AUTHORIZATION_PATTERNS, limit=3)
        if auth:
            auth_real_signals = []
            auth_noise = []

            for item in auth[:3]:
                if doc_type in {"application_requirements", "contract"} or is_manufacturer_auth_legal_noise(item.get("snippet")):
                    auth_noise.append(item)
                else:
                    auth_real_signals.append(item)

            for item in auth_noise[:2]:
                legal_noise.append(
                    {
                        "type": "manufacturer_authorization_legal_noise",
                        "source_document": doc.get("filename"),
                        "doc_type": doc_type,
                        "pattern": item["pattern"],
                        "snippet": item["snippet"],
                    }
                )

            if auth_real_signals:
                delta = int(signal_weight("manufacturer_authorization", 25) * weight)
                risk_score += delta
                key_findings.append(
                    f"Есть признаки требования авторизации/письма производителя в документе: {doc['filename']}."
                )
                for item in auth_real_signals[:2]:
                    add_evidence(
                        evidence,
                        signal_type="manufacturer_authorization",
                        severity="high" if doc_type == "technical_spec" else "medium",
                        doc=doc,
                        pattern=item["pattern"],
                        snippet=item["snippet"],
                    )

        # 5. Сертификаты и лицензии.
        certs = find_keyword_snippets(text, CERT_PATTERNS, limit=5)
        if certs:
            delta = int(signal_weight("cert_or_license_requirement", 12) * weight)
            risk_score += delta
            key_findings.append(
                f"Есть требования к сертификатам/лицензиям в документе: {doc['filename']}."
            )
            for item in certs[:3]:
                add_evidence(
                    evidence,
                    signal_type="cert_or_license_requirement",
                    severity="medium",
                    doc=doc,
                    pattern=item["pattern"],
                    snippet=item["snippet"],
                )

        # 6. Короткие сроки поставки.
        short_delivery = extract_short_delivery_days(text)
        if short_delivery:
            delta = int(signal_weight("short_delivery_term", 20) * weight)
            risk_score += delta
            key_findings.append(
                f"Найдены короткие сроки поставки/исполнения в документе: {doc['filename']}."
            )
            for item in short_delivery[:2]:
                add_evidence(
                    evidence,
                    signal_type="short_delivery_term",
                    severity="high" if item["days"] <= 5 else "medium",
                    doc=doc,
                    pattern=f"{item['days']} дней",
                    snippet=item["snippet"],
                )

        # 7. Ограничивающие формулировки. В контракте часто шум, в ТЗ полезнее.
        restrictive = find_keyword_snippets(text, RESTRICTIVE_PATTERNS, limit=4)
        if restrictive:
            restrictive_real_signals = []
            restrictive_noise = []

            for item in restrictive[:4]:
                if doc_type in {"contract", "application_requirements"} or is_restrictive_legal_noise(item.get("snippet")):
                    restrictive_noise.append(item)
                else:
                    restrictive_real_signals.append(item)

            for item in restrictive_noise[:2]:
                legal_noise.append(
                    {
                        "type": "restrictive_legal_noise",
                        "source_document": doc.get("filename"),
                        "doc_type": doc_type,
                        "pattern": item["pattern"],
                        "snippet": item["snippet"],
                    }
                )

            if restrictive_real_signals:
                delta = int(signal_weight("restrictive_phrase", 10) * weight)
                risk_score += delta
                key_findings.append(
                    f"Есть потенциально ограничивающие формулировки в документе: {doc['filename']}."
                )
                for item in restrictive_real_signals[:2]:
                    add_evidence(
                        evidence,
                        signal_type="restrictive_phrase",
                        severity="medium",
                        doc=doc,
                        pattern=item["pattern"],
                        snippet=item["snippet"],
                    )

        # 8. Типовые фразы про товарный знак - в шум, а не в риск.
        trademark_noise = find_keyword_snippets(text, ["товарный знак", "страна происхождения"], limit=2)
        for item in trademark_noise:
            legal_noise.append(
                {
                    "type": "standard_44fz_trademark_language",
                    "source_document": doc.get("filename"),
                    "doc_type": doc_type,
                    "pattern": item["pattern"],
                    "snippet": item["snippet"],
                }
            )

    # Убираем дубли находок, сохраняя порядок.
    seen = set()
    deduped_findings = []
    for item in key_findings:
        if item not in seen:
            seen.add(item)
            deduped_findings.append(item)

    risk_score = max(0, min(100, risk_score))

    high_threshold = document_risk_threshold("high", 70)
    medium_threshold = document_risk_threshold("medium", 35)

    if risk_score >= high_threshold:
        tailoring_risk = "high"
        recommendation = "manual_check_required"
    elif risk_score >= medium_threshold:
        tailoring_risk = "medium"
        recommendation = "check_before_go"
    else:
        tailoring_risk = "low"
        recommendation = "no_strong_tailoring_signals"

    doc_summaries = [
        {
            "filename": doc.get("filename"),
            "doc_type": doc.get("doc_type"),
            "doc_type_text": doc_type_text(doc.get("doc_type")),
            "doc_type_reason": doc.get("doc_type_reason"),
            "technical_spec_detection": doc.get("technical_spec_detection"),
            "technical_sections_count": len(
                technical_sections_from_detection(doc.get("technical_spec_detection"))
            ),
            "text_len": len(doc.get("text") or ""),
        }
        for doc in prepared_docs
    ]

    doc_type_summary = {
        doc_type_text(key): value for key, value in doc_type_counter.items()
    }

    if doc_type_summary:
        deduped_findings.insert(
            0,
            "Типы проанализированных документов: "
            + "; ".join(f"{k}: {v}" for k, v in doc_type_summary.items())
        )

    severity_priority = {"high": 0, "medium": 1, "low": 2}
    doc_priority_for_evidence = {
        "technical_spec": 0,
        "price_calculation": 1,
        "other": 2,
        "contract": 3,
        "application_requirements": 4,
    }

    evidence.sort(
        key=lambda item: (
            severity_priority.get(item.get("severity"), 9),
            doc_priority_for_evidence.get(item.get("doc_type"), 9),
        )
    )

    return {
        "tender_title": tender.get("title"),
        "documents_count": len(docs),
        "documents_with_text": len(prepared_docs),
        "total_text_chars": total_text_chars,
        "tailoring_risk": tailoring_risk,
        "risk_score": risk_score,
        "recommendation": recommendation,
        "key_findings": deduped_findings[:12],
        "evidence": evidence[:20],
        "legal_noise": legal_noise[:20],
        "doc_summaries": doc_summaries,
        "doc_type_summary": doc_type_summary,
        "manual_checks": [
            "Проверить ТЗ/описание объекта закупки, а не только проект контракта.",
            "Проверить, допускаются ли эквиваленты.",
            "Проверить конкретные модели, артикулы и вендоров.",
            "Проверить совместимость с имеющейся инфраструктурой заказчика.",
            "Проверить требования к письмам производителя или авторизации.",
            "Проверить сроки поставки и реалистичность исполнения.",
            "Проверить требования к лицензиям, сертификатам и опыту.",
        ],
    }


def get_candidate_data(limit_tenders: int) -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = {}

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    t.id AS tender_id,
                    t.external_id,
                    t.title,
                    t.raw,
                    d.id AS document_id,
                    d.filename,
                    d.storage_path,
                    d.extracted_text
                FROM tenders t
                JOIN analysis_results a ON a.tender_id = t.id
                LEFT JOIN documents d ON d.tender_id = t.id
                WHERE t.source = 'zakupki360'
                  AND a.analysis_type = 'rule_based_score'
                  AND a.recommendation IN ('go', 'maybe')
                ORDER BY a.score DESC, t.deadline_at ASC NULLS LAST, d.created_at ASC NULLS LAST
                LIMIT %s;
                """,
                (limit_tenders * 30,),
            )
            rows = cur.fetchall()

    for row in rows:
        tender_id = str(row["tender_id"])

        if tender_id not in data:
            data[tender_id] = {
                "tender": {
                    "id": tender_id,
                    "external_id": row["external_id"],
                    "title": row["title"],
                    "raw": row["raw"],
                },
                "docs": [],
            }

        if row["document_id"]:
            data[tender_id]["docs"].append(
                {
                    "id": str(row["document_id"]),
                    "filename": row["filename"],
                    "storage_path": row["storage_path"],
                    "extracted_text": row["extracted_text"],
                }
            )

    return dict(list(data.items())[:limit_tenders])


def save_analysis(tender_id: str, result: dict[str, Any]) -> None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM analysis_results
                WHERE tender_id = %s
                  AND analysis_type = 'document_risk_analysis';
                """,
                (tender_id,),
            )

            cur.execute(
                """
                INSERT INTO analysis_results (
                    tender_id,
                    analysis_type,
                    model,
                    result,
                    score,
                    recommendation,
                    confidence
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    tender_id,
                    "document_risk_analysis",
                    "rules-docs-v1",
                    Jsonb(result),
                    result.get("risk_score"),
                    result.get("recommendation"),
                    "medium" if result.get("tailoring_risk") != "unknown" else "low",
                ),
            )

        conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze extracted tender documents")
    parser.add_argument("--limit-tenders", type=int, default=10)
    args = parser.parse_args()

    candidates = get_candidate_data(limit_tenders=args.limit_tenders)
    print(f"Tenders to analyze: {len(candidates)}")

    for item in candidates.values():
        tender = item["tender"]
        docs = item["docs"]

        print()
        print(f"Tender {tender['external_id']}: {tender['title']}")
        print(f"Documents: {len(docs)}")

        result = analyze_tender_docs(tender, docs)
        save_analysis(tender["id"], result)

        print(f"Tailoring risk: {result['tailoring_risk']}")
        print(f"Risk score: {result['risk_score']}")
        print("Key findings:")
        for finding in result["key_findings"][:8]:
            print(f"- {finding}")


if __name__ == "__main__":
    main()
