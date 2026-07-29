import argparse
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from app.config import settings
from app.document_status import (
    build_document_status,
    document_status_label,
    document_status_quality_label,
)
from app.llm.tz_problem_cleanup import clean_report_problems_in_tz
from app.procedure_url import procedure_url_from_sources


MAX_REGION_LEN = 180
MAX_DOCS = 5
RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY = "_recent_digest_recommendation"
LEAD_LLM_REPORT_ANALYSIS_TYPE = "llm_customer_lead_report"
LEAD_TRIAGE_ANALYSIS_TYPE = "llm_customer_lead_triage"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_DEADLINE_MIN_DAYS = 3.0
TODAY_FILTER_AUDIT_DEFAULT_LIMIT = 50
TODAY_FILTER_AUDIT_MESSAGE_LIMIT = 3500
PREPARATION_REQUEUE_EVENT = "targeted_document_download_requeue"
PREPARATION_BLOCKED_EVENT = "targeted_document_preparation_blocked"
PARTIAL_DOCUMENT_DOWNLOAD_EVENT = "targeted_document_download_partial"
DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT = "document_download_rate_limited"
NON_CORE_TRANSPORT_SECURITY_REASON = "non_core_transport_security_infrastructure"
MANUAL_DOCUMENT_REASON_KEY = "_manual_document_reason"
TODAY_MANUAL_DOCUMENT_REASON_KEY = "_today_manual_document_reason"
DIGEST_REASON_SHOWN = "shown"
HIDDEN_BY_FEEDBACK = "hidden_by_feedback"
HIDDEN_BY_NO_GO = "hidden_by_no_go"
HIDDEN_BY_DIGEST_LIMIT = "hidden_by_digest_limit"
HIDDEN_BY_MISSING_REPORT_FIELDS = "hidden_by_missing_report_fields"
HIDDEN_BY_STATUS = "hidden_by_status"
HIDDEN_BY_SELECTION_POLICY = "hidden_by_selection_policy"
HIDDEN_BY_LEAD_TRIAGE_REJECT = "hidden_by_lead_triage_reject"
HIDDEN_BY_LEAD_HARD_NOISE = "hidden_by_lead_hard_noise"
DEADLINE_LEAD_WARNING_TEXT = (
    "Срок подачи близко/истек. Использовать как повод для звонка/знакомства, "
    "не как задачу на участие в текущей процедуре."
)
LEAD_DIGEST_VISIBLE_RECOMMENDATIONS = {"go", "maybe"}
MANUAL_DOCUMENT_REASONS = {
    "preparation_blocked=marketplace_auth",
    "preparation_no_valid_documents",
    "preparation_exhausted=documents_missing",
    "preparation_missing_primary_technical_document",
}
SALES_IN_WORK_STATUS = "sales_in_work"
SALES_NOT_RELEVANT_STATUS = "sales_not_relevant"
SALES_RETURNED_STATUS = "sales_returned"
SALES_EXISTING_CLIENT_STATUS = "sales_existing_client"
SALES_WORKFLOW_STATUSES = (
    SALES_IN_WORK_STATUS,
    SALES_NOT_RELEVANT_STATUS,
    SALES_RETURNED_STATUS,
    SALES_EXISTING_CLIENT_STATUS,
)
REJECT_REASON_FEEDBACK_STATUSES = (
    "bad_price",
    "bad_region",
    "not_profile",
    "service_only",
    "low_value_one_off",
    "bad_customer_fit",
    "bad_timing",
    "duplicate_or_processed",
    "other_reject",
)
SALES_FEEDBACK_LOOKUP_STATUSES = (
    *SALES_WORKFLOW_STATUSES,
    "interesting",
    "in_work",
    "tailored",
    "skip",
    "not_interesting",
    "not_our_profile",
    *REJECT_REASON_FEEDBACK_STATUSES,
)
SALES_WORKFLOW_HIDDEN_STATUSES = {
    SALES_IN_WORK_STATUS,
    SALES_NOT_RELEVANT_STATUS,
    SALES_EXISTING_CLIENT_STATUS,
}
LEAD_TRIAGE_REJECT_OVERRIDE_FEEDBACK_STATUSES = {
    SALES_IN_WORK_STATUS,
    SALES_RETURNED_STATUS,
    "interesting",
    "in_work",
}
NEGATIVE_SALES_FEEDBACK_STATUSES = {
    SALES_NOT_RELEVANT_STATUS,
    SALES_EXISTING_CLIENT_STATUS,
    "skip",
    "not_interesting",
    "not_our_profile",
    "tailored",
    *REJECT_REASON_FEEDBACK_STATUSES,
}
EXISTING_CLIENT_CUSTOMER_SELECT_SQL = """
                    existing_client_feedback.status AS existing_client_customer_status,
                    existing_client_feedback.created_at AS existing_client_customer_status_created_at,
                    existing_client_feedback.user_name AS existing_client_customer_user_name,
                    existing_client_feedback.source_tender_id AS existing_client_source_tender_id,
"""
WORKING_DIGEST_EXCLUDED_ACTIONS = {"skip_incumbent", "skip_low_priority", "no_go"}
WORKING_DIGEST_EXCLUDED_MARKET_ACCESS = {
    NON_CORE_TRANSPORT_SECURITY_REASON,
    "service_noise",
    "low_priority_deal",
}
PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION = """
(
    lower(coalesce(d.filename, '')) LIKE '%%техническое задание%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%описание объекта закупки%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%описание предмета закупки%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%техническая спецификация%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%спецификац%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%перечень оборудования%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%ведомость оборудования%%'
    OR lower(coalesce(d.filename, '')) ~ '(^|[^[:alnum:]_а-яё])тз([^[:alnum:]_а-яё]|$)'
    OR lower(coalesce(d.filename, '')) ~ '(^|[^[:alnum:]_а-яё])ооз([^[:alnum:]_а-яё]|$)'
)
""".strip()


@dataclass(frozen=True)
class TenderDigestCard:
    tender_id: str
    message_text: str
    external_id: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class DigestSelectionStats:
    total_found: int
    shown: int
    deadline_warning: int = 0
    hidden_by_feedback: int = 0
    hidden_no_go: int = 0
    hidden_by_lead_triage_reject: int = 0
    hidden_by_lead_hard_noise: int = 0


@dataclass(frozen=True)
class TodayDigestStats:
    total_found: int
    shown: int
    manual_documents_needed: int = 0
    hidden_no_go: int = 0
    hidden_deadline: int = 0
    deadline_warning: int = 0
    hidden_preparation: int = 0
    hidden_by_lead_triage_reject: int = 0
    hidden_by_lead_hard_noise: int = 0
    hidden_by_feedback: int = 0


@dataclass(frozen=True)
class OperationalLeadQueueStats:
    total_found: int
    shown: int
    hidden_in_work: int = 0
    hidden_negative_feedback: int = 0
    hidden_by_lead_triage_reject: int = 0
    hidden_by_lead_hard_noise: int = 0


@dataclass(frozen=True)
class OperationalLeadInWorkStats:
    total_found: int
    shown: int
    hidden_expired: int = 0


@dataclass(frozen=True)
class OperationalLeadAllStats:
    total_found: int
    shown: int
    new_count: int = 0
    in_work_count: int = 0
    hidden_in_work: int = 0
    hidden_negative_feedback: int = 0
    hidden_by_digest_limit: int = 0
    hidden_by_lead_triage_reject: int = 0
    hidden_by_lead_hard_noise: int = 0


@dataclass(frozen=True)
class TodayFilterAuditStats:
    total_found: int
    would_show: int
    manual_documents_needed: int = 0
    hidden_no_go: int = 0
    hidden_deadline: int = 0
    hidden_by_lead_triage_reject: int = 0
    hidden_by_lead_hard_noise: int = 0
    hidden_by_feedback: int = 0
    hidden_other: int = 0
    listed: int = 0
    limit: int = TODAY_FILTER_AUDIT_DEFAULT_LIMIT
    go_leads: int = 0
    maybe_leads: int = 0
    rejected_triage: int = 0
    hard_noise_total: int = 0
    hard_noise_diagnostics_available: bool = False
    processed_in_work: int = 0
    processed_not_relevant: int = 0
    processed_total: int = 0


@dataclass(frozen=True)
class TodayFilterAuditEntry:
    tender: dict
    status: str
    reason: str
    bucket: str
    documents: str


@dataclass(frozen=True)
class TodayFilterAuditCard:
    tender_id: str
    message_text: str
    status: str
    reason: str
    external_id: str | None = None
    title: str | None = None


def money(value) -> str:
    if value is None:
        return "не указана"

    if isinstance(value, Decimal):
        value = float(value)

    return f"{value:,.0f} ₽".replace(",", " ")


def date_text(value) -> str:
    if not value:
        return "не указан"

    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")

    return str(value)


def short_text(value: str | None, limit: int = 180) -> str:
    if not value:
        return "не указано"

    value = str(value).strip()
    if len(value) <= limit:
        return value

    return value[: limit - 1].rstrip() + "…"


def list_block(items: list[str], empty: str = "—") -> str:
    if not items:
        return empty

    return "\n".join(f"— {item}" for item in items)


def sql_placeholders(count: int) -> str:
    return ", ".join("%s" for _ in range(count))


def existing_client_customer_feedback_sql(workflow_status_placeholders: str) -> str:
    return f"""
                LEFT JOIN LATERAL (
                    SELECT
                        source_feedback.status,
                        source_feedback.created_at,
                        source_feedback.user_name,
                        source_t.id AS source_tender_id
                    FROM tenders source_t
                    JOIN LATERAL (
                        SELECT f.status, f.created_at, f.user_name
                        FROM feedback f
                        WHERE f.tender_id = source_t.id
                          AND f.status IN ({workflow_status_placeholders})
                        ORDER BY f.created_at DESC, f.id DESC
                        LIMIT 1
                    ) source_feedback ON TRUE
                    WHERE source_feedback.status = %s
                      AND btrim(coalesce(source_t.customer_name, '')) <> ''
                      AND lower(regexp_replace(btrim(coalesce(source_t.customer_name, '')), '\\s+', ' ', 'g'))
                        = lower(regexp_replace(btrim(coalesce(t.customer_name, '')), '\\s+', ' ', 'g'))
                    ORDER BY source_feedback.created_at DESC, source_t.id DESC
                    LIMIT 1
                ) existing_client_feedback ON TRUE
"""


def recommendation_emoji(recommendation: str) -> str:
    return {
        "go": "🟢",
        "maybe": "🟡",
        "no_go": "🔴",
    }.get(recommendation, "⚪")


def recommendation_text(recommendation: str) -> str:
    return {
        "go": "хорошая возможность",
        "maybe": "быстрый ручной чек перед пресейлом",
        "no_go": "не тратить время",
    }.get(recommendation, recommendation)


from app.business_rules import (
    business_assessment,
    business_assessment_block,
    digest_sort_key,
    effective_recommendation,
    lead_hard_noise_reason_for_row as business_lead_hard_noise_reason_for_row,
    selection_warnings,
)


def recommendation_explanation_block(tender: dict) -> str:
    warnings = selection_warnings(tender)
    if not warnings:
        return "— критичных предупреждений нет"

    return list_block(warnings)


def law_text(value: str | None, raw_full: dict[str, Any]) -> str:
    if value and not value.startswith("lawId="):
        return value

    law_id = raw_full.get("lawId")

    mapping = {
        1: "223-ФЗ",
        2: "615-ПП",
        3: "44-ФЗ",
        4: "Коммерческая",
    }

    if law_id in mapping:
        return mapping[law_id]

    if value:
        return value

    return "не указан"


def raw_full(tender: dict) -> dict[str, Any]:
    raw = tender.get("raw") or {}
    full = raw.get("full") or {}

    if isinstance(full, dict):
        return full

    return {}


IMPORTANT_API_DOCUMENT_PATTERNS = (
    "техническое задание",
    "описание предмета закупки",
    "описание объекта закупки",
    "техническая спецификация",
    "спецификац",
    "конкурсная документация",
    "закупочная документация",
    "документация",
    "материалы закупки",
    "материалы",
    "приложение",
    "приложения",
    "перечень оборудования",
    "ведомость оборудования",
)

IMPORTANT_API_HARDWARE_PATTERNS = (
    "сервер",
    "серверы",
    "серверное оборудование",
    "схд",
    "система хранения",
    "коммутатор",
    "маршрутизатор",
    "сетевое оборудование",
    "межсетевой экран",
    "firewall",
    "оборудование",
    "онм",
)

IMPORTANT_API_TOKEN_RE = re.compile(
    r"(?<![а-яa-z0-9])(?:кд|тз|пк)(?![а-яa-z0-9])",
    re.IGNORECASE,
)


def api_documents_from_tender(tender: dict) -> list[dict]:
    documents = raw_full(tender).get("documents") or []
    return [doc for doc in documents if isinstance(doc, dict)]


def api_documents_count(tender: dict) -> int | None:
    documents = api_documents_from_tender(tender)
    if documents:
        return len(documents)

    full = raw_full(tender)
    for container in (tender, full):
        if not isinstance(container, dict):
            continue
        for key in (
            "api_docs_count",
            "api_documents_count",
            "documents_available_count",
            "available_documents_count",
            "documentsCount",
            "documents_count",
        ):
            if key not in container:
                continue
            try:
                return max(0, int(container.get(key) or 0))
            except (TypeError, ValueError):
                continue

    return None


def api_document_priority_text(doc: dict) -> str:
    return " ".join(
        str(doc.get(key) or "")
        for key in ("title", "filename", "fileName", "name")
    )


def api_document_looks_important(doc: dict) -> bool:
    normalized = normalized_text(api_document_priority_text(doc))
    if not normalized:
        return False
    return bool(
        IMPORTANT_API_TOKEN_RE.search(normalized)
        or any(pattern in normalized for pattern in IMPORTANT_API_DOCUMENT_PATTERNS)
        or any(pattern in normalized for pattern in IMPORTANT_API_HARDWARE_PATTERNS)
    )


def has_unprocessed_important_api_documents(tender: dict, stored_count: int) -> bool:
    documents = api_documents_from_tender(tender)
    if not documents or len(documents) <= stored_count:
        return False
    return any(api_document_looks_important(doc) for doc in documents)


def get_customer_name(tender: dict, full: dict[str, Any]) -> str:
    if tender.get("customer_name"):
        return tender["customer_name"]

    placer = full.get("placerOrganization") or {}
    if isinstance(placer, dict):
        for key in ("fullName", "name"):
            if placer.get(key):
                return placer[key]

    customers = full.get("customers") or []
    if customers:
        first = customers[0] or {}
        if isinstance(first, dict):
            return first.get("fullName") or first.get("name") or "не указан"

    return "не указан"


def documents_block(full: dict[str, Any]) -> str:
    documents = full.get("documents") or []

    if not documents:
        return "— документы не найдены в карточке API"

    lines = []

    for doc in documents[:MAX_DOCS]:
        if not isinstance(doc, dict):
            continue

        title = doc.get("title") or "Документ без названия"
        doc_id = doc.get("id")

        if doc_id:
            lines.append(f"— {title} (id: {doc_id})")
        else:
            lines.append(f"— {title}")

    if len(documents) > MAX_DOCS:
        lines.append(f"— и ещё {len(documents) - MAX_DOCS} док.")

    return "\n".join(lines) if lines else "— документы не найдены в карточке API"


def document_risk_text(value: str | None) -> str:
    return {
        "high": "высокий",
        "medium": "средний",
        "low": "низкий",
        "unknown": "неизвестно",
    }.get(value or "", value or "не анализировался")


PARTIAL_DOCUMENT_DOWNLOAD_WARNING_TEXT = (
    "Документы обработаны частично: часть документов не скачана из-за лимита 429. "
    "Проверь проект контракта/требования вручную."
)


def has_partial_document_download_warning(tender: dict) -> bool:
    raw_event = tender.get("partial_document_download_event")
    if not raw_event:
        return False

    payload = parse_processing_event_payload(raw_event)
    if payload:
        skipped = _non_negative_int(
            payload.get("documents_skipped_due_to_rate_limit")
            or payload.get("skipped_due_to_rate_limit")
        )
        return skipped > 0 or bool(payload.get("stopped_due_to_rate_limit"))

    event_text = str(raw_event)
    match = re.search(r"documents_skipped_due_to_rate_limit=(\d+)", event_text)
    if match:
        return int(match.group(1)) > 0

    return "document_download_rate_limited" in event_text or "after 429" in event_text


def partial_document_download_warning_text(tender: dict) -> str | None:
    if has_partial_document_download_warning(tender):
        return PARTIAL_DOCUMENT_DOWNLOAD_WARNING_TEXT

    return None


def document_analysis_block(tender: dict) -> str:
    result = tender.get("document_risk_result")
    partial_warning = partial_document_download_warning_text(tender)

    if not result:
        if partial_warning:
            return f"{partial_warning}\nДокументы ещё не анализировались."
        return "Документы ещё не анализировались."

    risk = result.get("tailoring_risk")
    risk_score = result.get("risk_score")
    findings = result.get("key_findings") or []
    evidence = result.get("evidence") or []
    docs_with_text = result.get("documents_with_text")
    total_text_chars = result.get("total_text_chars")

    lines = [
        f"Риск заточки: {document_risk_text(risk)}",
        f"Оценка риска: {risk_score}/100" if risk_score is not None else "Оценка риска: не указана",
        f"Документов с текстом: {docs_with_text}, символов: {total_text_chars}" if docs_with_text is not None else "",
    ]

    lines = [line for line in lines if line]
    if partial_warning:
        lines.insert(0, partial_warning)

    if findings:
        lines.append("")
        lines.append("Ключевые находки:")
        for item in findings[:5]:
            lines.append(f"— {item}")

    if evidence:
        lines.append("")
        lines.append("Фрагменты/сигналы:")
        shown = 0

        for item in evidence:
            if shown >= 3:
                break

            item_type = item.get("type")

            if item_type == "model_like_mentions":
                values = item.get("values") or []
                if values:
                    lines.append(f"— Похожие на модели/артикулы: {', '.join(values[:8])}")
                    shown += 1
                continue

            snippet = item.get("snippet")
            pattern = item.get("pattern")

            if snippet:
                lines.append(f"— {pattern}: {snippet[:260]}…")
                shown += 1

    return "\n".join(lines)



def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def short_list(items, limit: int = 3) -> list[str]:
    if not items:
        return []
    return [str(item) for item in as_list(items)[:limit]]


def has_deep_llm_guidance(tender: dict) -> bool:
    result = tender.get("llm_report_result") or {}
    if not isinstance(result, dict):
        return False

    if llm_report_is_stale_without_document_text(tender, result):
        return False
    if llm_report_is_stale_after_primary_technical_document(tender, result):
        return False

    report = result_report(result)
    if not isinstance(report, dict) or not report:
        return False

    offer = as_dict(report.get("potential_offer"))
    guidance_fields = [
        report.get("what_presales_should_check"),
        offer.get("what_presales_should_check"),
        offer.get("what_to_verify"),
        report.get("next_action"),
        report.get("recommended_actions"),
        report.get("problems_in_tz"),
        report.get("questions_to_customer"),
    ]
    return any(as_list(value) for value in guidance_fields)


def append_value_or_bullets(
    lines: list[str],
    label: str,
    value,
    *,
    limit: int = 5,
) -> None:
    if isinstance(value, list):
        values = short_list(value, limit)
        if not values:
            return
        lines.append(f"{label}:")
        for item in values:
            lines.append(f"— {item}")
        return

    if value:
        lines.append(f"{label}: {value}")


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0

    return max(0, parsed)


STALE_LLM_NO_TEXT_MESSAGE = (
    "LLM-анализ не показывается: нет извлеченного текста документов. "
    "Нужен повторный разбор после подготовки документов."
)
STALE_LLM_PRIMARY_TECH_MESSAGE = (
    "LLM-анализ устарел: после отчета обработан основной технический документ. "
    "Запустите разбор заново."
)
PRELIMINARY_NO_TECH_SPEC_TITLE = "⚠️ Предварительный разбор без ТЗ/спецификации"
PRELIMINARY_NO_TECH_SPEC_TEXT = (
    "В доступных документах не найдено техническое задание или спецификация. "
    "Полный комплект может быть на внешней площадке или требует ручного доступа."
)
PRELIMINARY_NO_TECH_SPEC_NEXT_ACTION = (
    "Запросить ТЗ/спецификацию/перечень оборудования у заказчика или скачать полный комплект с внешней площадки."
)
NO_TECH_SPEC_PHRASES = (
    "тз отсутств",
    "тз не найден",
    "тз не предостав",
    "техническое задание отсутств",
    "техническое задание не найден",
    "технического задания нет",
    "нет технического задания",
    "не найдено техническое задание",
    "спецификация отсутств",
    "спецификация не найден",
    "нет спецификации",
    "перечень оборудования отсутств",
    "перечень оборудования не найден",
    "технические характеристики отсутств",
    "технические характеристики не указаны",
    "technical specification missing",
)
CONTENT_DETECTED_TECH_SPEC_REASONS = {
    "technical_section_found_inside_document",
    "content_contains_technical_spec",
    "content_contains_supply_table",
    "price_doc_contains_supply_spec",
}
TECH_SPEC_FOUND_INSIDE_REVIEW_WARNING = (
    "Техническая часть найдена в КД/приложении, но требует ручной проверки."
)
SPEC_REFERENCED_BUT_NOT_FOUND_TEXT = (
    "Спецификация упоминается в документах, но сама таблица спецификации не найдена."
)


def text_contains_no_extracted_documents(value: Any) -> bool:
    joined = " ".join(str(item) for item in as_list(value)).lower().replace("ё", "е")
    return "нет извлеченного текста" in joined


def normalized_text(value: Any) -> str:
    return str(value or "").lower().replace("ё", "е")


REPORT_PAYLOAD_KEYS = (
    "document_status",
    "lead_summary",
    "customer_signal",
    "target_roles",
    "target_end_customer_roles",
    "possible_needs",
    "opening_phrase",
    "first_phrase",
    "discovery_questions",
    "next_action",
    "lead_priority",
    "recommendation",
    "verdict",
    "decision",
    "summary",
    "what_is_required",
    "potential_offer",
    "technical_spec_analysis",
)


def report_payload_from_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    report = value.get("report")
    if isinstance(report, dict):
        return report

    if any(key in value for key in REPORT_PAYLOAD_KEYS):
        return value

    return {}


def text_corpus(value: Any, *, limit: int = 60000) -> str:
    chunks: list[str] = []

    def walk(item: Any) -> None:
        if sum(len(chunk) for chunk in chunks) >= limit:
            return
        if isinstance(item, dict):
            for key, nested in item.items():
                chunks.append(str(key))
                walk(nested)
            return
        if isinstance(item, (list, tuple, set)):
            for nested in item:
                walk(nested)
            return
        if item is not None:
            chunks.append(str(item))

    walk(value)
    return normalized_text(" ".join(chunks))[:limit]


def text_contains_no_technical_spec(value: Any) -> bool:
    corpus = text_corpus(value)
    return any(phrase in corpus for phrase in NO_TECH_SPEC_PHRASES)


def result_report(result: dict) -> dict:
    return report_payload_from_result(result)


def result_meta(result: dict) -> dict:
    if not isinstance(result, dict):
        return {}
    meta = result.get("meta") or result.get("llm_metadata") or {}
    return meta if isinstance(meta, dict) else {}


def document_risk_result(tender: dict) -> dict:
    result = tender.get("document_risk_result") or {}
    return result if isinstance(result, dict) else {}


def documents_with_text_count(tender: dict, result: dict | None = None) -> int:
    containers = [tender, document_risk_result(tender)]
    if isinstance(result, dict):
        containers.append(result_meta(result))

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in (
            "documents_with_text",
            "docs_with_text",
            "documents_with_text_count",
            "docs_with_text_count",
        ):
            if key in container:
                count = _non_negative_int(container.get(key))
                if count > 0:
                    return count

    return 0


def spec_fact_candidates(result: dict) -> list[dict]:
    report = result_report(result)
    meta = result_meta(result)
    candidates: list[Any] = [
        report.get("extracted_spec_facts"),
        result.get("extracted_spec_facts") if isinstance(result, dict) else None,
        meta.get("extracted_spec_facts"),
        meta.get("spec_facts"),
    ]
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def spec_facts_have_content(facts: dict) -> bool:
    for count_key in ("supply_items_count", "technical_characteristics_count"):
        if _non_negative_int(facts.get(count_key)) > 0:
            return True

    for list_key in ("supply_items", "technical_characteristics"):
        value = facts.get(list_key)
        if isinstance(value, list) and value:
            return True

    return False


def report_has_extracted_spec_content(result: dict) -> bool:
    return any(spec_facts_have_content(facts) for facts in spec_fact_candidates(result))


def report_has_meaningful_technical_content(report: dict) -> bool:
    technical = as_dict(report.get("technical_spec_analysis"))
    candidates: list[Any] = [
        technical.get("what_is_required"),
        technical.get("key_characteristics"),
        report.get("what_is_required"),
    ]

    for candidate in candidates:
        for item in as_list(candidate):
            text = normalized_text(item).strip()
            if text and not text_contains_no_technical_spec(text):
                return True

    return False


def doc_summary_has_technical_spec(doc: dict) -> bool:
    doc_type = normalized_text(doc.get("doc_type"))
    doc_type_text_value = normalized_text(doc.get("doc_type_text") or doc.get("doc_type"))
    reason = str(doc.get("doc_type_reason") or "")
    detection = doc.get("technical_spec_detection")

    if doc_type == "technical_spec":
        return True
    if "тз / описание объекта закупки" in doc_type_text_value:
        return True
    if reason in CONTENT_DETECTED_TECH_SPEC_REASONS:
        return True
    if isinstance(detection, dict) and detection.get("detected"):
        return True

    return False


def document_context_has_technical_spec(tender: dict, result: dict) -> bool:
    meta = result_meta(result)

    for key in ("technical_spec_documents_count", "technical_spec_docs_count"):
        if _non_negative_int(meta.get(key)) > 0:
            return True

    if _non_negative_int(meta.get("technical_spec_found_inside_count")) > 0:
        return True

    for key in ("technical_spec_documents", "technical_spec_docs"):
        value = meta.get(key)
        if isinstance(value, list) and value:
            return True

    for container in (document_risk_result(tender), meta):
        if not isinstance(container, dict):
            continue
        for key in ("doc_summaries", "documents_summary"):
            summaries = container.get(key)
            if isinstance(summaries, list) and any(
                isinstance(doc, dict) and doc_summary_has_technical_spec(doc)
                for doc in summaries
            ):
                return True

        doc_type_summary = container.get("doc_type_summary")
        if isinstance(doc_type_summary, dict) and doc_type_summary:
            for label, count in doc_type_summary.items():
                label_text = normalized_text(label)
                if _non_negative_int(count) > 0 and (
                    "тз / описание объекта закупки" in label_text
                    or "техническое задание" in label_text
                ):
                    return True

    return False


def technical_spec_found_inside_needs_review(tender: dict, result: dict) -> bool:
    if report_has_extracted_spec_content(result):
        return False

    meta = result_meta(result)
    if _non_negative_int(meta.get("technical_spec_found_inside_count")) > 0:
        return True

    for container in (document_risk_result(tender), meta):
        if not isinstance(container, dict):
            continue
        for key in ("doc_summaries", "documents_summary"):
            summaries = container.get(key)
            if not isinstance(summaries, list):
                continue
            for doc in summaries:
                if not isinstance(doc, dict):
                    continue
                if str(doc.get("doc_type_reason") or "") in CONTENT_DETECTED_TECH_SPEC_REASONS:
                    return True

    return False


def specification_references_only_found(tender: dict, result: dict) -> bool:
    for container in (document_risk_result(tender), result_meta(result)):
        if not isinstance(container, dict):
            continue
        for key in ("doc_summaries", "documents_summary"):
            summaries = container.get(key)
            if not isinstance(summaries, list):
                continue
            for doc in summaries:
                if not isinstance(doc, dict):
                    continue
                detection = doc.get("technical_spec_detection")
                if isinstance(detection, dict) and detection.get("reason") == "references_specification_only":
                    return True
                if doc.get("technical_spec_reference_note"):
                    return True

    return False


def document_context_lacks_technical_spec(tender: dict, result: dict) -> bool:
    known = False
    meta = result_meta(result)

    if document_context_has_technical_spec(tender, result):
        return False

    if "preliminary_no_technical_spec" in meta:
        return bool(meta.get("preliminary_no_technical_spec"))
    if "no_technical_spec" in meta:
        return bool(meta.get("no_technical_spec"))

    for key in ("technical_spec_documents_count", "technical_spec_docs_count"):
        if key in meta:
            known = True
            if _non_negative_int(meta.get(key)) > 0:
                return False

    for key in ("technical_spec_documents", "technical_spec_docs"):
        value = meta.get(key)
        if isinstance(value, list):
            known = True
            if value:
                return False

    document_result = document_risk_result(tender)
    doc_summaries = document_result.get("doc_summaries")
    if isinstance(doc_summaries, list) and doc_summaries:
        known = True
        for doc in doc_summaries:
            if not isinstance(doc, dict):
                continue
            if doc_summary_has_technical_spec(doc):
                return False

    doc_type_summary = document_result.get("doc_type_summary")
    if isinstance(doc_type_summary, dict) and doc_type_summary:
        known = True
        for label, count in doc_type_summary.items():
            label_text = normalized_text(label)
            if _non_negative_int(count) > 0 and (
                "тз / описание объекта закупки" in label_text
                or "техническое задание" in label_text
            ):
                return False

    documents_summary = meta.get("documents_summary")
    if isinstance(documents_summary, list) and documents_summary:
        known = True
        for doc in documents_summary:
            if not isinstance(doc, dict):
                continue
            if doc_summary_has_technical_spec(doc):
                return False

    return known


def llm_report_is_preliminary_without_technical_spec(tender: dict, result: dict) -> bool:
    if documents_with_text_count(tender, result) <= 0:
        return False

    report = result_report(result)
    meta = result_meta(result)

    if report_has_extracted_spec_content(result):
        return False

    if report_has_meaningful_technical_content(report):
        return False

    if document_context_has_technical_spec(tender, result):
        return False

    if meta.get("preliminary_no_technical_spec") or meta.get("no_technical_spec"):
        return True

    explicit_no_spec = text_contains_no_technical_spec(report)
    if explicit_no_spec:
        return True

    return document_context_lacks_technical_spec(tender, result)


def tender_is_preliminary_without_technical_spec(tender: dict) -> bool:
    result = tender.get("llm_report_result") or {}
    return isinstance(result, dict) and llm_report_is_preliminary_without_technical_spec(tender, result)


def cap_preliminary_decision(decision: Any) -> str | None:
    normalized = str(decision or "").strip().lower().replace("-", "_")
    if normalized == "go":
        return "maybe"
    if normalized in {"maybe", "no_go"}:
        return normalized
    return str(decision).strip() if decision else None


def cap_preliminary_confidence(confidence: Any) -> str:
    normalized = str(confidence or "").strip().lower()
    if normalized == "low":
        return "low"
    return "low"


def prepend_unique_action(actions: list[Any], first_action: str) -> list[str]:
    result = [first_action]
    first_norm = normalized_text(first_action)
    for action in actions:
        action_text = str(action or "").strip()
        if action_text and normalized_text(action_text) != first_norm:
            result.append(action_text)
    return result


def llm_report_is_stale_without_document_text(tender: dict, result: dict) -> bool:
    for key in (
        "documents_with_text",
        "docs_with_text",
        "documents_with_text_count",
        "docs_with_text_count",
    ):
        if key in tender and _non_negative_int(tender.get(key)) <= 0:
            return True

    document_result = tender.get("document_risk_result") or {}
    if isinstance(document_result, dict):
        if (
            "documents_with_text" in document_result
            and _non_negative_int(document_result.get("documents_with_text")) <= 0
        ):
            return True

        if text_contains_no_extracted_documents(document_result.get("key_findings")):
            return True

        if text_contains_no_extracted_documents(document_result.get("missing_data")):
            return True

    meta = result.get("meta") or {}
    if isinstance(meta, dict):
        if meta.get("card_only") or meta.get("context_source") == "card_only":
            return True
        if (
            "documents_with_text" in meta
            and _non_negative_int(meta.get("documents_with_text")) <= 0
        ):
            return True

    return False


def parse_report_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def moscow_today_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=MOSCOW_TZ)
    moscow_now = current_time.astimezone(MOSCOW_TZ)
    start = datetime.combine(moscow_now.date(), time.min, tzinfo=MOSCOW_TZ)
    return start, start + timedelta(days=1)


def datetime_in_range(
    value: datetime | None,
    *,
    start: datetime,
    end: datetime,
) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return start <= value.astimezone(start.tzinfo) < end


def datetime_after(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left > right


def llm_report_created_at(tender: dict, result: dict) -> datetime | None:
    candidates = [
        tender.get("llm_report_created_at"),
        result.get("created_at") if isinstance(result, dict) else None,
        result_meta(result).get("created_at") if isinstance(result, dict) else None,
    ]
    for candidate in candidates:
        parsed = parse_report_datetime(candidate)
        if parsed is not None:
            return parsed
    return None


def latest_primary_technical_document_created_at(tender: dict) -> datetime | None:
    candidates = [
        tender.get("latest_primary_technical_document_created_at"),
        tender.get("latest_technical_spec_document_created_at"),
        tender.get("primary_technical_document_created_at"),
        tender.get("technical_spec_document_created_at"),
    ]
    parsed = [parse_report_datetime(value) for value in candidates]
    parsed = [value for value in parsed if value is not None]
    return max(parsed) if parsed else None


def llm_report_is_stale_after_primary_technical_document(
    tender: dict,
    result: dict,
) -> bool:
    report_created_at = llm_report_created_at(tender, result)
    primary_doc_created_at = latest_primary_technical_document_created_at(tender)
    if report_created_at is None or primary_doc_created_at is None:
        return False

    if datetime_after(primary_doc_created_at, report_created_at):
        return True

    document_risk_created_at = parse_report_datetime(tender.get("document_risk_created_at"))
    if (
        document_risk_created_at is not None
        and datetime_after(document_risk_created_at, report_created_at)
        and document_context_has_technical_spec(tender, result)
    ):
        return True

    return False


def llm_presales_block(tender: dict) -> str:
    result = tender.get("llm_report_result") or {}

    if not isinstance(result, dict):
        return "LLM-анализ ещё не выполнялся."

    report = result.get("report") or {}

    if not isinstance(report, dict) or not report:
        return "LLM-анализ ещё не выполнялся."

    if llm_report_is_stale_without_document_text(tender, result):
        return STALE_LLM_NO_TEXT_MESSAGE

    if llm_report_is_stale_after_primary_technical_document(tender, result):
        return STALE_LLM_PRIMARY_TECH_MESSAGE

    preliminary = llm_report_is_preliminary_without_technical_spec(tender, result)
    recommendation = as_dict(report.get("recommendation"))
    tailoring = as_dict(report.get("tailoring_risk"))
    delivery = report.get("delivery_feasibility")
    delivery_dict = as_dict(delivery)
    offer = report.get("potential_offer")
    offer_dict = as_dict(offer)
    commercial = as_dict(report.get("commercial_assessment"))
    technical_spec = as_dict(report.get("technical_spec_analysis"))

    lines = []

    if preliminary:
        lines.append(PRELIMINARY_NO_TECH_SPEC_TITLE)
        lines.append(PRELIMINARY_NO_TECH_SPEC_TEXT)
        if specification_references_only_found(tender, result):
            lines.append(SPEC_REFERENCED_BUT_NOT_FOUND_TEXT)
        lines.append("")
    elif technical_spec_found_inside_needs_review(tender, result):
        lines.append(TECH_SPEC_FOUND_INSIDE_REVIEW_WARNING)
        lines.append("")

    summary = report.get("summary")
    if summary:
        lines.append(f"Резюме: {summary}")

    decision = recommendation.get("decision") or report.get("verdict")
    confidence = recommendation.get("confidence") or report.get("confidence")
    if preliminary:
        decision = cap_preliminary_decision(decision) or "maybe"
        confidence = cap_preliminary_confidence(confidence)
    if decision or confidence:
        lines.append(f"Вердикт LLM: {decision or 'не указан'} / уверенность: {confidence or 'не указана'}")

    tailoring_level = tailoring.get("level")
    if tailoring_level:
        lines.append(f"Риск заточки по LLM: {tailoring_level}")

    if delivery_dict:
        delivery_term = delivery_dict.get("delivery_term")
        delivery_assessment = delivery_dict.get("assessment")
        if delivery_term or delivery_assessment:
            if isinstance(delivery_term, list) or isinstance(delivery_assessment, list):
                lines.append("Срок поставки / оценка:")
                if delivery_term:
                    for item in short_list(delivery_term, 3):
                        lines.append(f"— Срок: {item}")
                if delivery_assessment:
                    for item in short_list(delivery_assessment, 4):
                        lines.append(f"— Оценка: {item}")
            else:
                lines.append(f"Срок поставки: {delivery_term or 'не указан'} / {delivery_assessment or 'не оценён'}")
    elif delivery:
        append_value_or_bullets(lines, "Срок поставки / оценка", delivery, limit=5)

    mentioned = as_list(technical_spec.get("mentioned_models_or_vendors"))
    if mentioned:
        lines.append("")
        lines.append("Модели/вендоры:")
        for item in short_list(mentioned, 4):
            lines.append(f"— {item}")

    supply = as_list(report.get("supply_composition"))
    if supply:
        lines.append("")
        lines.append("Состав поставки:")
        for item in short_list(supply, 5):
            lines.append(f"— {item}")

    critical = as_list(
        report.get("critical_tz_requirements")
        or report.get("what_is_required")
        or technical_spec.get("key_characteristics")
        or technical_spec.get("what_is_required")
    )
    if critical:
        lines.append("")
        lines.append("Критичные требования ТЗ:")
        for item in short_list(critical, 5):
            lines.append(f"— {item}")

    what_to_offer = offer_dict.get("what_to_offer") if offer_dict else offer
    if what_to_offer:
        lines.append("")
        lines.append("Что можно предложить:")
        for item in short_list(what_to_offer, 3):
            lines.append(f"— {item}")

    presales_checks = as_list(
        report.get("what_presales_should_check")
        or offer_dict.get("what_presales_should_check")
        or offer_dict.get("what_to_verify")
    )
    if presales_checks:
        lines.append("")
        lines.append("Что проверить пресейлу:")
        for item in short_list(presales_checks, 4):
            lines.append(f"— {item}")

    raw_problems = as_list(report.get("problems_in_tz"))
    problems = clean_report_problems_in_tz(report) if raw_problems else []
    if problems:
        lines.append("")
        lines.append("Проблемы и неясности ТЗ:")
        for item in short_list(problems, 4):
            lines.append(f"— {item}")

    questions = as_list(report.get("questions_to_customer"))
    if questions:
        lines.append("")
        lines.append("Что уточнить у заказчика:")
        for item in short_list(questions, 4):
            lines.append(f"— {item}")

    next_action = as_list(report.get("recommended_actions") or report.get("next_action"))
    if preliminary:
        next_action = prepend_unique_action(
            next_action,
            PRELIMINARY_NO_TECH_SPEC_NEXT_ACTION,
        )
    if next_action:
        lines.append("")
        lines.append("Рекомендованные действия:")
        for item in short_list(next_action, 3):
            lines.append(f"— {item}")

    fit = commercial.get("fit_for_integrator")
    if fit:
        lines.append("")
        lines.append(f"Fit для интегратора: {fit}")

    return "\n".join(lines) if lines else "LLM-анализ есть, но краткий вывод пуст."

def contacts_block(full: dict[str, Any]) -> str:
    contact_person = full.get("contactPerson")
    contact_phone = full.get("contactPhone")
    contact_email = full.get("contactEMail")

    lines = [
        f"ФИО: {contact_person or 'не указано'}",
        f"Телефон: {contact_phone or 'не указан'}",
        f"Email: {contact_email or 'не указан'}",
    ]

    return "\n".join(lines)


def finance_block(tender: dict, full: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"НМЦК: {money(tender.get('initial_price'))}",
            f"Обеспечение заявки: {money(full.get('guaranteeOrder'))}",
            f"Обеспечение контракта: {money(full.get('guaranteeExecuteContract'))}",
        ]
    )


def procedure_block(tender: dict, full: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Закон: {law_text(tender.get('law'), full)}",
            f"Тип процедуры: {full.get('tenderTypeName') or tender.get('procedure_type') or 'не указан'}",
            f"Этап: {full.get('tenderStageName') or 'не указан'}",
            f"Площадка: {(full.get('short') or {}).get('etpName') if isinstance(full.get('short'), dict) else 'не указана'}",
        ]
    )


def delivery_block(tender: dict, full: dict[str, Any]) -> str:
    delivery_place = full.get("deliveryPlace") or tender.get("region")
    delivery_term = full.get("deliveryTerm")

    return "\n".join(
        [
            f"Место поставки: {short_text(delivery_place, MAX_REGION_LEN)}",
            f"Срок поставки: {delivery_term or 'не указан'}",
        ]
    )


def next_action_block(recommendation: str, tender: dict | None = None) -> str:
    if tender and tender_is_preliminary_without_technical_spec(tender):
        return (
            f"1. {PRELIMINARY_NO_TECH_SPEC_NEXT_ACTION}\n"
            "2. Открыть внешнюю площадку и проверить, доступен ли полный комплект документации.\n"
            "3. Повторить пресейл-разбор после получения ТЗ."
        )

    assessment = business_assessment(tender or {}) if tender else {"action": "quick_check"}
    action = assessment["action"]

    if action == "skip_incumbent":
        return (
            "1. Не тратить пресейл без подтверждения, что мы можем зайти как действующий или уполномоченный поставщик.\n"
            "2. Проверить, это продление уже установленного решения или новая закупка.\n"
            "3. Если это обычное продление - отложить."
        )

    if action == "check_domestic_analog":
        return (
            "1. Проверить, действительно ли действует отечественное ограничение/реестр.\n"
            "2. Понять, есть ли российский аналог или реестровая позиция, которую мы можем поставить.\n"
            "3. Без подтверждения аналога не тратить глубокий пресейл."
        )

    if action == "check_exact_supply":
        return (
            "1. Быстро проверить точную поставляемость указанной конфигурации.\n"
            "2. Проверить канал поставки, срок и маржу.\n"
            "3. Если можем поставить именно это решение - передать в пресейл и связаться с заказчиком."
        )

    if recommendation == "go":
        return (
            "1. Открыть ТЗ и проверить спецификацию.\n"
            "2. Проверить поставляемость и маржинальность.\n"
            "3. Связаться с контактным лицом заказчика.\n"
            "4. Передать закупку пресейлу на оценку."
        )

    if recommendation == "maybe":
        return (
            "1. Быстро проверить ТЗ и регион.\n"
            "2. Оценить, есть ли смысл тратить пресейл.\n"
            "3. Если ТЗ профильное - связаться с заказчиком."
        )

    return "Не тратить время без дополнительной причины."


def digest_effective_recommendation(tender: dict) -> str:
    recommendation = tender.get(RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY) or effective_recommendation(tender)
    if tender_is_preliminary_without_technical_spec(tender) and recommendation == "go":
        return "maybe"
    return recommendation


def digest_business_assessment_block(tender: dict) -> str:
    block = business_assessment_block(tender)
    override = tender.get(RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY)
    if tender_is_preliminary_without_technical_spec(tender) and override != "no_go":
        override = "maybe"

    if override not in {"maybe", "no_go"}:
        return block

    lines = []
    replaced = False
    for line in block.splitlines():
        if line.startswith("Бизнес-действие:"):
            lines.append(f"Бизнес-действие: {recommendation_text(override)}")
            replaced = True
        else:
            lines.append(line)

    if not replaced:
        lines.append(f"Бизнес-действие: {recommendation_text(override)}")

    return "\n".join(lines)


def procedure_url(tender: dict, full: dict[str, Any] | None = None) -> str | None:
    full = full if full is not None else raw_full(tender)
    return procedure_url_from_sources(tender, full)


OUTREACH_DEFAULT_TARGET = (
    "ИТ-директор, руководитель ИТ, инфраструктуры, ЦОД, ИБ или технический заказчик."
)
OUTREACH_PROCUREMENT_CENTER_TARGET = (
    "технического заказчика или ИТ-ответственного у конечного получателя; "
    "закупочный контакт использовать как вход и попросить соединить с ИТ."
)
OUTREACH_PROCUREMENT_CONTACT_HINT = (
    "Начать с закупочного контакта и попросить соединить с ИТ-ответственным."
)
OUTREACH_DEFAULT_QUESTIONS = [
    "Кто отвечает за это направление внутри организации?",
    "Есть ли похожие инфраструктурные задачи в ближайшие месяцы?",
    "Можно ли познакомиться с ИТ-руководителем или техническим ответственным?",
]
OUTREACH_PROCEDURE_FIRST_MARKERS = [
    "по вашей закупк",
    "по этому тендер",
    "по закупке №",
    "по закупке no",
    "по процедуре",
    "в процедуре",
    "обсудить процедуру",
    "обсудить закупк",
    "участвовать в закупк",
    "участие в закупк",
    "допускается ли подача заявки",
    "подача заявки",
    "подать заявку",
    "заявку на участие",
    "какой срок поставки",
    "срок поставки",
    "группа реестра",
    "реестр",
    "письмо производителя",
    "авторизационное письмо",
    "какие документы предоставить",
    "документы предоставить",
    "предоставить документы",
    "эквивалент",
]
OUTREACH_CATEGORY_TEXT = {
    "servers": {
        "topic": "серверная инфраструктура",
        "phrase_subject": "серверной инфраструктуре",
        "reason": (
            "Закупка выглядит как сигнал активной задачи: серверная инфраструктура. "
            "Смежные темы: обновление серверного парка, виртуализация, 1С/БД, backup, СХД, поддержка."
        ),
        "questions": [
            "Кто отвечает за серверную инфраструктуру внутри организации?",
            "Есть ли похожие задачи по обновлению серверного парка, виртуализации или 1С/БД в ближайшие месяцы?",
            "Можно ли познакомиться с ИТ-руководителем или техническим ответственным?",
        ],
    },
    "storage": {
        "topic": "СХД и хранение данных",
        "phrase_subject": "СХД и хранению данных",
        "reason": (
            "Закупка выглядит как сигнал активной задачи: СХД и хранение данных. "
            "Смежные темы: рост данных, backup, отказоустойчивость, виртуализация, совместимость с российским ПО."
        ),
        "questions": [
            "Кто отвечает за СХД и хранение данных внутри организации?",
            "Есть ли похожие задачи по росту данных, backup или отказоустойчивости в ближайшие месяцы?",
            "Можно ли познакомиться с ИТ-руководителем или техническим ответственным?",
        ],
    },
    "security_hardware": {
        "topic": "защищенная инфраструктура / ИБ",
        "phrase_subject": "защищенной инфраструктуре и ИБ",
        "reason": (
            "Закупка выглядит как сигнал активной задачи: защищенная инфраструктура / ИБ. "
            "Смежные темы: импортозамещение, регуляторика, межсетевые экраны, сопровождение, аттестация."
        ),
        "questions": [
            "Кто отвечает за защищенную инфраструктуру и ИБ внутри организации?",
            "Есть ли похожие задачи по импортозамещению, регуляторике или сопровождению ИБ в ближайшие месяцы?",
            "Можно ли познакомиться с ИТ-руководителем или техническим ответственным?",
        ],
    },
    "network": {
        "topic": "сетевая инфраструктура",
        "phrase_subject": "сетевой инфраструктуре",
        "reason": (
            "Закупка выглядит как сигнал активной задачи: сетевая инфраструктура. "
            "Смежные темы: коммутация, отказоустойчивость, Wi-Fi, сегментация, безопасность."
        ),
        "questions": [
            "Кто отвечает за сетевую инфраструктуру внутри организации?",
            "Есть ли похожие задачи по коммутации, Wi-Fi или отказоустойчивости в ближайшие месяцы?",
            "Можно ли познакомиться с ИТ-руководителем или техническим ответственным?",
        ],
    },
    "infra_project": {
        "topic": "инфраструктурный проект",
        "phrase_subject": "инфраструктурным проектам",
        "reason": (
            "Закупка выглядит как сигнал активной задачи: инфраструктурный проект. "
            "Смежные темы: модернизация, монтаж, поддержка, проектные работы."
        ),
        "questions": [
            "Кто отвечает за инфраструктурные проекты внутри организации?",
            "Есть ли похожие задачи по модернизации, монтажу или поддержке в ближайшие месяцы?",
            "Можно ли познакомиться с ИТ-руководителем или техническим ответственным?",
        ],
    },
    "unknown": {
        "topic": "ИТ-инфраструктура",
        "phrase_subject": "ИТ-инфраструктуре",
        "reason": (
            "Закупка выглядит как сигнал активной инфраструктурной задачи. "
            "Смежные темы: серверы, СХД, виртуализация, сеть, ИБ, сопровождение."
        ),
        "questions": OUTREACH_DEFAULT_QUESTIONS,
    },
}
OUTREACH_CATEGORY_TEXT["generic"] = OUTREACH_CATEGORY_TEXT["unknown"]
OUTREACH_CATEGORY_ALIASES = {
    "server": "servers",
    "servers": "servers",
    "сервер": "servers",
    "серверы": "servers",
    "серверная инфраструктура": "servers",
    "storage": "storage",
    "схд": "storage",
    "хранение данных": "storage",
    "network": "network",
    "networks": "network",
    "сеть": "network",
    "сетевая инфраструктура": "network",
    "security": "security_hardware",
    "security_hardware": "security_hardware",
    "иб": "security_hardware",
    "защищенная инфраструктура": "security_hardware",
    "защищенная инфраструктура / иб": "security_hardware",
    "пак": "security_hardware",
    "pak": "security_hardware",
    "infra_project": "infra_project",
    "infrastructure_project": "infra_project",
    "инфраструктурный проект": "infra_project",
}
OUTREACH_CATEGORY_PRIORITY = [
    "security_hardware",
    "servers",
    "storage",
    "network",
    "infra_project",
]
OUTREACH_CATEGORY_KEYWORDS = {
    "security_hardware": [
        "информационная безопасность",
        "информационной безопасности",
        "защищенная инфраструктура",
        "защищенный",
        "защищенная",
        "защищенное",
        "аппаратно-программный комплекс",
        "межсетевой экран",
        "межсетев",
        "аттестация",
        "крипто",
        "пак",
        "иб",
    ],
    "servers": [
        "серверная инфраструктура",
        "серверное оборудование",
        "сервер",
        "server",
        "вычислительн",
        "центр обработки данных",
        "обработка данных",
        "1с",
        "бд",
        "виртуализац",
    ],
    "storage": [
        "схд",
        "система хранения",
        "системы хранения",
        "хранение данных",
        "хранилищ",
        "дисковый массив",
        "storage",
        "backup",
        "резервное копирование",
        "резервн",
    ],
    "network": [
        "сетевая инфраструктура",
        "сетевое оборудование",
        "сеть",
        "сетев",
        "коммутатор",
        "маршрутизатор",
        "wi-fi",
        "wifi",
        "точк",
        "сегментац",
    ],
    "infra_project": [
        "инфраструктурный проект",
        "инфраструктурные работы",
        "модернизация",
        "монтаж",
        "пусконалад",
        "проектные работы",
        "сопровождение",
    ],
}


def compact_card_price(tender: dict, full: dict[str, Any]) -> Any:
    return (
        tender.get("initial_price")
        or tender.get("max_price")
        or tender.get("price")
        or full.get("maxPrice")
        or full.get("nmck")
    )


def compact_card_deadline(tender: dict, full: dict[str, Any]) -> Any:
    return (
        tender.get("application_deadline")
        or tender.get("end_date")
        or tender.get("bidding_deadline")
        or tender.get("deadline_at")
        or full.get("endDate")
        or full.get("filingEndDate")
    )


def deadline_with_days_text(value: Any, *, now: datetime | None = None) -> str:
    base = date_text(value)
    current_time = now or datetime.now(timezone.utc)
    deadline = normalized_deadline(value, now=current_time)
    if deadline is None:
        return base

    seconds_left = (deadline - current_time).total_seconds()
    if seconds_left < 0:
        return f"{base} (срок прошел)"

    if seconds_left < 24 * 60 * 60:
        return f"{base} (сегодня)"

    days_left = int(seconds_left // (24 * 60 * 60))
    if seconds_left % (24 * 60 * 60):
        days_left += 1
    return f"{base} (осталось {days_left} дн.)"


def procurement_contact_text(full: dict[str, Any]) -> str | None:
    values = [
        full.get("contactPerson"),
        full.get("contactPhone"),
        full.get("contactEMail"),
    ]
    parts = [str(value).strip() for value in values if str(value or "").strip()]
    return ", ".join(parts) if parts else None


def customer_looks_procurement_center(customer: Any) -> bool:
    text = normalized_text(customer)
    if "закуп" in text and any(
        marker in text for marker in ("комитет", "управление", "департамент", "центр", "служба", "дирекция")
    ):
        return True
    return any(
        marker in text
        for marker in (
            "закупочный центр",
            "центр закуп",
            "комитет по закуп",
            "комитет госзакуп",
            "комитет государственн закуп",
            "управление закуп",
            "управление госзакуп",
            "управление государственн закуп",
            "департамент закуп",
            "департамент госзакуп",
            "департамент государственн закуп",
            "служба закуп",
            "контрактная служба",
            "единая дирекция закуп",
        )
    )


def call_first_recommendation_text(recommendation: str) -> str:
    return {
        "go": "передать в первичную обработку",
        "maybe": "проверить и взять в работу",
        "no_go": "не брать в работу без новой причины",
    }.get(recommendation, "проверить и взять в работу")


def outreach_simple_text(value: Any, *, limit: int = 240) -> str:
    if value is None:
        return ""

    if isinstance(value, dict):
        preferred_keys = (
            "text",
            "value",
            "summary",
            "reason",
            "phrase",
            "question",
            "role",
            "target",
            "name",
        )
        for key in preferred_keys:
            if value.get(key):
                return outreach_simple_text(value.get(key), limit=limit)

        simple_values = [
            str(item).strip()
            for item in value.values()
            if item is not None and not isinstance(item, (dict, list, tuple, set))
        ]
        value = " ".join(simple_values)

    if isinstance(value, (list, tuple, set)):
        simple_values = [outreach_simple_text(item, limit=limit) for item in value]
        value = " ".join(item for item in simple_values if item)

    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n-—•")
    if not text or text in {"{}", "[]"}:
        return ""
    return short_text(text, limit)


def outreach_first_value(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = source.get(key)
        if value:
            return value
    return None


def outreach_brief_from_report(tender: dict) -> dict[str, Any]:
    result = tender.get("llm_report_result") or {}
    if not isinstance(result, dict):
        return {}

    report = result_report(result)
    for candidate in (report.get("outreach_brief"), result.get("outreach_brief")):
        brief = as_dict(candidate)
        if brief:
            return brief
    return {}


def outreach_category_from_text(corpus: str) -> str | None:
    text = normalized_text(corpus)
    if not text:
        return None

    for category in OUTREACH_CATEGORY_PRIORITY:
        keywords = OUTREACH_CATEGORY_KEYWORDS.get(category) or []
        if any(normalized_text(keyword) in text for keyword in keywords):
            return category
    return None


def outreach_category_key(tender: dict) -> str:
    result = tender.get("llm_report_result") or {}
    report = result_report(result) if isinstance(result, dict) else {}
    facts = as_dict(report.get("extracted_spec_facts"))

    explicit_values = [
        report.get("detected_category"),
        facts.get("category"),
        tender.get("business_category"),
        tender.get("detected_category"),
        tender.get("category"),
    ]
    for value in explicit_values:
        category = OUTREACH_CATEGORY_ALIASES.get(normalized_text(value).strip())
        if category:
            return category

    full = raw_full(tender)
    title_corpus = text_corpus(
        [
            tender.get("title"),
            full.get("title"),
            full.get("name"),
            full.get("subject"),
            full.get("purchaseObjectInfo"),
            full.get("objectInfo"),
            full.get("lotName"),
        ],
        limit=6000,
    )
    return outreach_category_from_text(title_corpus) or "unknown"


def outreach_category_cfg(category_key: str) -> dict[str, Any]:
    return OUTREACH_CATEGORY_TEXT.get(category_key) or OUTREACH_CATEGORY_TEXT["generic"]


def outreach_looks_procedure_first(value: str) -> bool:
    text = normalized_text(value)
    if any(marker in text for marker in OUTREACH_PROCEDURE_FIRST_MARKERS):
        return True
    return bool(re.search(r"\bпп\s*(616|719|1875)\b", text))


def outreach_sentence_text(value: Any, *, limit: int = 300, max_sentences: int = 2) -> str:
    text = outreach_simple_text(value, limit=limit)
    if not text:
        return ""
    parts = re.findall(r"[^.!?]+[.!?]?", text)
    if parts:
        text = " ".join(part.strip() for part in parts[:max_sentences] if part.strip())
    if text and text[-1] not in ".!?":
        text += "."
    return short_text(text, limit)


def outreach_questions(value: Any) -> list[str]:
    if value is None:
        return []

    raw_items: list[Any] = []
    if isinstance(value, dict):
        for key in (
            "questions",
            "items",
            "what_to_ask",
            "discovery_questions",
            "customer_clarifications",
        ):
            nested = value.get(key)
            if nested:
                raw_items.extend(as_list(nested))
        if not raw_items:
            raw_items.extend(value.values())
    else:
        raw_items.extend(as_list(value))

    questions: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = outreach_simple_text(item, limit=190)
        if not text:
            continue
        if text[-1] not in ".!?":
            text += "?"
        key = normalized_text(text)
        if key in seen:
            continue
        seen.add(key)
        questions.append(text)
        if len(questions) == 3:
            break
    return questions


def outreach_who_to_find(tender: dict, brief: dict[str, Any], full: dict[str, Any]) -> str:
    if customer_looks_procurement_center(get_customer_name(tender, full)):
        return OUTREACH_PROCUREMENT_CENTER_TARGET

    raw_value = outreach_first_value(
        brief,
        (
            "who_to_find",
            "target_person",
            "target_persona",
            "target_role",
            "target_roles",
            "contact_target",
            "whom_to_find",
            "кого_искать",
            "Кого искать",
        ),
    )
    candidate = outreach_simple_text(raw_value, limit=180)
    candidate_norm = normalized_text(candidate)
    technical_markers = ("ит", "инфраструкт", "цод", "иб", "техническ", "системн")
    if candidate and any(marker in candidate_norm for marker in technical_markers):
        target = candidate
    else:
        target = OUTREACH_DEFAULT_TARGET

    if procurement_contact_text(full):
        return f"{target} {OUTREACH_PROCUREMENT_CONTACT_HINT}"
    return target


def outreach_reason_to_call(brief: dict[str, Any], category_key: str) -> str:
    return outreach_category_cfg(category_key)["reason"]


def outreach_first_phrase(brief: dict[str, Any], category_key: str) -> str:
    subject = outreach_category_cfg(category_key)["phrase_subject"]
    return (
        f"Здравствуйте, увидели, что у вас сейчас есть задачи по {subject}, "
        "хотели познакомиться с ответственным за ИТ-инфраструктуру и понять, можем ли быть полезны по похожим задачам."
    )


def outreach_questions_to_ask(brief: dict[str, Any], tender: dict) -> list[str]:
    category_key = outreach_category_key(tender)
    questions = outreach_category_cfg(category_key).get("questions") or OUTREACH_DEFAULT_QUESTIONS
    return list(questions)[:3]


def compact_outreach_block(tender: dict) -> str:
    full = raw_full(tender)
    brief = outreach_brief_from_report(tender)
    category_key = outreach_category_key(tender)

    who = outreach_who_to_find(tender, brief, full)
    reason = outreach_reason_to_call(brief, category_key)
    first_phrase = outreach_first_phrase(brief, category_key)
    questions = outreach_questions_to_ask(brief, tender)

    lines = ["📞 Для знакомства"]
    if who:
        lines.append(f"Кого искать: {who}")
    if reason:
        lines.append(f"Почему звонить: {reason}")
    if first_phrase:
        lines.append(f"Первая фраза: {first_phrase}")
    if questions:
        lines.append("Что выяснить:")
        for index, question in enumerate(questions[:3], start=1):
            lines.append(f"{index}. {question}")
    return "\n".join(lines)


def lead_report_text_value(value: Any, *, limit: int = 260) -> str:
    return outreach_simple_text(value, limit=limit)


def lead_report_list_values(value: Any, *, limit: int = 4, item_limit: int = 180) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in as_list(value):
        text = lead_report_text_value(item, limit=item_limit)
        if not text:
            continue
        key = normalized_text(text)
        if key in seen:
            continue
        seen.add(key)
        values.append(text)
        if len(values) >= limit:
            break
    return values


def lead_report_body(tender: dict) -> dict[str, Any]:
    result = tender.get("llm_report_result") or tender.get("result") or {}
    return _report_from_result(result)


def lead_report_document_status(tender: dict, report: dict[str, Any] | None = None) -> dict[str, Any]:
    report = report if isinstance(report, dict) else lead_report_body(tender)
    status = report.get("document_status") if isinstance(report, dict) else None
    if isinstance(status, dict) and status.get("code"):
        return status

    result = tender.get("llm_report_result") or tender.get("result") or {}
    meta = result.get("meta") if isinstance(result, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    status = meta.get("document_status")
    if isinstance(status, dict) and status.get("code"):
        return status

    return build_document_status(
        document_selection=meta.get("document_selection"),
        document_preparation=meta.get("document_preparation"),
        documents_summary=meta.get("documents_summary") or [],
    )


def lead_report_document_status_label(tender: dict, report: dict[str, Any] | None = None) -> str:
    status = lead_report_document_status(tender, report)
    return document_status_quality_label(status)


LEAD_PRIORITY_LABELS = {
    "high": "высокий",
    "medium": "средний",
    "low": "низкий",
}
LEAD_CONFIDENCE_LABELS = {
    "high": "высокая",
    "medium": "средняя",
    "low": "низкая",
}


def lead_report_confidence_from_row(tender: dict, report: dict[str, Any]) -> str:
    confidence = lead_report_text_value(
        report.get("confidence")
        or tender.get("llm_report_confidence")
        or tender.get("confidence"),
        limit=40,
    ).lower()
    return LEAD_CONFIDENCE_LABELS.get(confidence, "не указана")


def lead_report_priority_text(priority: str) -> str:
    return LEAD_PRIORITY_LABELS.get(priority, "не указан")


def lead_report_possible_needs(report: dict[str, Any]) -> list[str]:
    for key in (
        "possible_needs",
        "customer_needs",
        "likely_needs",
        "potential_needs",
    ):
        values = lead_report_list_values(report.get(key), limit=4)
        if values:
            return values
    return []


def lead_report_customer_story_text(report: dict[str, Any]) -> str:
    for value in (
        report.get("likely_customer_story"),
        report.get("customer_story"),
        report.get("account_hypothesis"),
        report.get("customer_history"),
    ):
        text = lead_report_text_value(value, limit=360)
        if text:
            return text
    return ""


def lead_report_who_to_find_values(
    tender: dict,
    report: dict[str, Any],
    full: dict[str, Any],
) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in (
        *lead_report_list_values(
            report.get("target_end_customer_roles"),
            limit=4,
        ),
        *lead_report_list_values(report.get("target_roles"), limit=4),
    ):
        key = normalized_text(item)
        if item and key not in seen:
            seen.add(key)
            values.append(item)
        if len(values) >= 4:
            return values

    fallback = outreach_who_to_find(tender, outreach_brief_from_report(tender), full)
    if fallback:
        values.append(fallback)
    return values[:4]


def lead_report_who_to_find(tender: dict, report: dict[str, Any], full: dict[str, Any]) -> str:
    values = lead_report_who_to_find_values(tender, report, full)
    if values:
        return "; ".join(values[:4])

    brief = outreach_brief_from_report(tender)
    return outreach_who_to_find(tender, brief, full)


def lead_report_signal_text(tender: dict, report: dict[str, Any]) -> str:
    for value in (
        report.get("lead_summary"),
        report.get("customer_signal"),
        report.get("likely_customer_story"),
        report.get("why_priority"),
    ):
        text = lead_report_text_value(value, limit=320)
        if text:
            return text
    return outreach_reason_to_call(outreach_brief_from_report(tender), outreach_category_key(tender))


def lead_report_opening_phrase(tender: dict, report: dict[str, Any]) -> str:
    phrase = lead_report_text_value(
        report.get("opening_phrase") or report.get("first_phrase"),
        limit=360,
    )
    if phrase:
        return phrase
    return outreach_first_phrase(outreach_brief_from_report(tender), outreach_category_key(tender))


def lead_report_discovery_questions(tender: dict, report: dict[str, Any]) -> list[str]:
    questions = outreach_questions(report.get("discovery_questions"))
    if questions:
        return questions[:3]
    return outreach_questions_to_ask(outreach_brief_from_report(tender), tender)[:3]


def lead_next_action_is_call_first(text: str) -> bool:
    normalized = normalized_text(text)
    if not normalized or len(text) > 260:
        return False
    return any(
        marker in normalized
        for marker in (
            "позвон",
            "звон",
            "созвон",
            "набрать",
            "связаться по телефону",
            "call",
        )
    )


def lead_report_next_action(report: dict[str, Any]) -> str:
    action = lead_report_text_value(report.get("next_action"), limit=300)
    return action


def lead_report_append_list(lines: list[str], title: str, items: list[str]) -> None:
    if not items:
        return
    lines.append(title)
    for item in items[:4]:
        lines.append(f"- {item}")


def format_caller_lead_card(tender: dict, index: int | None = None) -> str:
    full = raw_full(tender)
    report = lead_report_body(tender)

    recommendation = (
        operational_queue_decision_from_row(tender)
        or lead_report_stored_recommendation_from_row(tender)
        or "go"
    )
    emoji = recommendation_emoji(recommendation)
    number = f"{index}. " if index is not None else ""
    title = short_text(tender.get("title") or "Без названия", 240)
    customer = get_customer_name(tender, full)
    price = compact_card_price(tender, full)
    deadline = compact_card_deadline(tender, full)
    priority = lead_report_priority_text(lead_report_priority_from_row(tender))
    confidence = lead_report_confidence_from_row(tender, report)
    document_status = lead_report_document_status_label(tender, report)
    contact = procurement_contact_text(full)
    url = procedure_url(tender, full)
    signal = lead_report_signal_text(tender, report)
    possible_needs = lead_report_possible_needs(report)
    who = lead_report_who_to_find_values(tender, report, full)
    phrase = lead_report_opening_phrase(tender, report)
    questions = lead_report_discovery_questions(tender, report)
    next_action = lead_report_next_action(report)

    lines = [
        f"{number}{emoji} {title}",
        f"Рекомендация: {call_first_recommendation_text(recommendation)}",
        f"Приоритет: {priority}",
        f"Уверенность: {confidence}",
        document_status,
        f"НМЦК: {money(price)}",
        f"Срок подачи (справочно): {deadline_with_days_text(deadline)}",
        f"Заказчик: {customer}",
    ]
    append_lead_deadline_warning(lines, deadline)
    if contact:
        lines.append(f"Контакт закупки: {contact}")
    lines.append(f"Ссылка: {url or 'не найдена'}")

    if signal:
        lines.extend(["", f"Сигнал по заказчику: {signal}"])
    lead_report_append_list(lines, "Возможные потребности:", possible_needs)
    lead_report_append_list(lines, "Кого искать:", who)

    lines.extend(["", "📞 Первичный контакт"])
    if phrase:
        lines.append(f"Первая фраза: {phrase}")
    if questions:
        lines.append("Что выяснить:")
        for question_index, question in enumerate(questions[:3], start=1):
            lines.append(f"{question_index}. {question}")
    if next_action:
        lines.append(f"Следующее действие: {next_action}")

    return "\n".join(lines)


def compact_working_card(tender: dict, index: int | None = None) -> str:
    full = raw_full(tender)
    effective_rec = digest_effective_recommendation(tender)
    emoji = recommendation_emoji(effective_rec)
    number = f"{index}. " if index is not None else ""

    title = short_text(tender.get("title") or "Без названия", 240)
    customer = get_customer_name(tender, full)
    price = compact_card_price(tender, full)
    deadline = compact_card_deadline(tender, full)
    contact = procurement_contact_text(full)
    url = procedure_url(tender, full)

    lines = [
        f"{number}{emoji} {title}",
        "",
        f"Рекомендация: {call_first_recommendation_text(effective_rec)}",
        f"НМЦК: {money(price)}",
        f"Срок подачи: {deadline_with_days_text(deadline)}",
        f"Заказчик: {customer}",
    ]
    append_lead_deadline_warning(lines, deadline)
    if contact:
        lines.append(f"Контакт закупки: {contact}")
    lines.extend(
        [
            f"Ссылка: {url or 'не найдена'}",
            "",
            compact_outreach_block(tender),
        ]
    )
    return "\n".join(lines)


def format_card(tender: dict, index: int | None = None) -> str:
    return compact_working_card(tender, index=index)


def tender_card_from_row(tender: dict, message_text: str) -> TenderDigestCard:
    tender_id = tender.get("tender_id") or tender.get("id")
    if tender_id is None:
        raise ValueError("Tender row does not contain tender_id/id")

    return TenderDigestCard(
        tender_id=str(tender_id),
        message_text=message_text,
        external_id=str(tender.get("external_id")) if tender.get("external_id") else None,
        title=str(tender.get("title")) if tender.get("title") else None,
    )


def non_negative_days(value: str) -> float:
    try:
        days = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ожидается число дней") from exc

    if days < 0:
        raise argparse.ArgumentTypeError("количество дней не может быть отрицательным")

    return days


def deadline_cutoff(
    min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    *,
    now: datetime | None = None,
) -> datetime:
    current_time = now or datetime.now(timezone.utc)
    return current_time + timedelta(days=min_days)


def normalized_deadline(value: Any, *, now: datetime) -> datetime | None:
    if not value:
        return None

    if isinstance(value, datetime):
        deadline = value
    elif isinstance(value, date):
        deadline = datetime.combine(value, time.max)
    elif isinstance(value, str):
        try:
            deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if deadline.tzinfo is None and now.tzinfo is not None:
        deadline = deadline.replace(tzinfo=now.tzinfo)
    elif deadline.tzinfo is not None and now.tzinfo is None:
        deadline = deadline.replace(tzinfo=None)

    return deadline


def deadline_is_fresh(
    tender: dict,
    min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    *,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(timezone.utc)
    deadline = normalized_deadline(tender.get("deadline_at"), now=current_time)
    if deadline is None:
        return True

    return deadline >= deadline_cutoff(min_days, now=current_time)


def deadline_is_active(tender: dict, *, now: datetime | None = None) -> bool:
    current_time = now or datetime.now(timezone.utc)
    deadline = normalized_deadline(tender.get("deadline_at"), now=current_time)
    if deadline is None:
        return True

    return deadline >= current_time


def deadline_value_needs_lead_warning(
    value: Any,
    min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    *,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(timezone.utc)
    deadline = normalized_deadline(value, now=current_time)
    if deadline is None:
        return False

    return deadline < deadline_cutoff(min_days, now=current_time)


def lead_deadline_value(tender: dict) -> Any:
    return compact_card_deadline(tender, raw_full(tender))


def lead_deadline_needs_warning(
    tender: dict,
    min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    *,
    now: datetime | None = None,
) -> bool:
    return deadline_value_needs_lead_warning(
        lead_deadline_value(tender),
        min_days,
        now=now,
    )


def append_lead_deadline_warning(
    lines: list[str],
    deadline: Any,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    now: datetime | None = None,
) -> None:
    if deadline_value_needs_lead_warning(deadline, deadline_min_days, now=now):
        lines.append(DEADLINE_LEAD_WARNING_TEXT)


def latest_sales_workflow_status(tender: dict) -> str | None:
    status = str(
        tender.get("latest_sales_status")
        or tender.get("sales_status")
        or tender.get("sales_workflow_status")
        or ""
    ).strip()
    return status if status in SALES_WORKFLOW_STATUSES else None


def hide_from_working_sales_lists(tender: dict) -> bool:
    return latest_sales_workflow_status(tender) in SALES_WORKFLOW_HIDDEN_STATUSES


def hidden_by_existing_client_customer(tender: dict) -> bool:
    if latest_sales_workflow_status(tender) == SALES_RETURNED_STATUS:
        return False
    return (
        str(tender.get("existing_client_customer_status") or "").strip()
        == SALES_EXISTING_CLIENT_STATUS
        or bool(tender.get("existing_client_source_tender_id"))
    )


def latest_negative_sales_feedback_status(tender: dict) -> str | None:
    if hidden_by_existing_client_customer(tender):
        return SALES_EXISTING_CLIENT_STATUS
    status = str(tender.get("latest_sales_status") or "").strip()
    return status if status in NEGATIVE_SALES_FEEDBACK_STATUSES else None


def latest_positive_or_working_sales_feedback_status(tender: dict) -> str | None:
    status = str(tender.get("latest_sales_status") or "").strip()
    return status if status in LEAD_TRIAGE_REJECT_OVERRIDE_FEEDBACK_STATUSES else None


def hidden_by_sales_feedback_for_lead_queue(tender: dict) -> bool:
    return (
        latest_sales_workflow_status(tender) == SALES_IN_WORK_STATUS
        or latest_negative_sales_feedback_status(tender) is not None
    )


def hide_from_new_operational_lead_queue(tender: dict) -> bool:
    return hidden_by_sales_feedback_for_lead_queue(tender)


def lead_hard_noise_reason_for_digest_row(
    tender: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> str | None:
    try:
        return business_lead_hard_noise_reason_for_row(tender, assessment)
    except Exception:
        return None


def hidden_by_lead_hard_noise(
    tender: dict[str, Any],
    assessment: dict[str, Any] | None = None,
) -> bool:
    return lead_hard_noise_reason_for_digest_row(tender, assessment) is not None


def is_in_work_sales_candidate(tender: dict, *, now: datetime | None = None) -> bool:
    return (
        latest_sales_workflow_status(tender) == SALES_IN_WORK_STATUS
        and deadline_is_active(tender, now=now)
    )


def get_digest_rows(limit: int | None = 15) -> list[dict]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            limit_clause = "LIMIT %s" if limit is not None else ""
            params = (limit,) if limit is not None else ()
            sales_feedback_placeholders = sql_placeholders(
                len(SALES_FEEDBACK_LOOKUP_STATUSES)
            )
            sales_workflow_placeholders = sql_placeholders(len(SALES_WORKFLOW_STATUSES))
            cur.execute(
                f"""
                SELECT
                    t.id AS tender_id,
                    t.external_id,
                    t.title,
                    t.customer_name,
                    t.initial_price,
                    t.region,
                    t.law,
                    t.procedure_type,
                    t.published_at,
                    t.deadline_at,
                    t.url,
                    t.raw,
                    a.score,
                    a.recommendation,
                    a.confidence,
                    a.result,
                    dr.result AS document_risk_result,
                    dr.created_at AS document_risk_created_at,
                    lpr.result AS llm_report_result,
                    triage.result AS lead_triage_result,
                    triage.recommendation AS lead_triage_recommendation,
                    triage.confidence AS lead_triage_confidence,
                    triage.created_at AS lead_triage_created_at,
                    triage.analysis_type AS lead_triage_analysis_type,
                    triage.lead_decision AS lead_triage_decision,
                    operational_lr.created_at AS operational_lead_report_created_at,
                    operational_lr.analysis_type AS operational_lead_report_analysis_type,
                    operational_lr.recommendation AS operational_lead_report_recommendation,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment,
{EXISTING_CLIENT_CUSTOMER_SELECT_SQL}
                    COALESCE(docs_stats.docs_count, 0) AS docs_count,
                    COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text,
                    COALESCE(docs_stats.docs_with_text, 0) AS documents_with_text_count,
                    primary_docs.latest_primary_technical_document_created_at,
                    blocked_event.message AS preparation_blocked_event,
                    requeue_event.message AS preparation_requeue_event,
                    partial_download_event.message AS partial_document_download_event
                FROM tenders t
                JOIN LATERAL (
                    SELECT a.score, a.recommendation, a.confidence, a.result
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('rule_based_score', 'rule_based_scoring')
                      AND a.recommendation IN ('go', 'maybe')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) a ON TRUE
                LEFT JOIN analysis_results dr
                  ON dr.tender_id = t.id
                 AND dr.analysis_type = 'document_risk_analysis'
                LEFT JOIN analysis_results lpr
                  ON lpr.tender_id = t.id
                 AND lpr.analysis_type = 'llm_presales_report'
                LEFT JOIN LATERAL (
                    SELECT
                        result,
                        recommendation,
                        confidence,
                        created_at,
                        analysis_type,
                        lower(replace(coalesce(
                            result #>> '{{report,lead_decision}}',
                            result #>> '{{lead_decision}}',
                            result #>> '{{report,decision}}',
                            result #>> '{{decision}}',
                            recommendation,
                            ''
                        ), '-', '_')) AS lead_decision
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) triage ON TRUE
                LEFT JOIN LATERAL (
                    SELECT result, recommendation, confidence, created_at, analysis_type
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) operational_lr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN ({sales_feedback_placeholders})
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                {existing_client_customer_feedback_sql(sales_workflow_placeholders)}
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS docs_count,
                        COUNT(*) FILTER (
                            WHERE d.extracted_text IS NOT NULL
                              AND length(d.extracted_text) > 0
                        ) AS docs_with_text
                    FROM documents d
                    WHERE d.tender_id = t.id
                ) docs_stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT max(d.created_at) AS latest_primary_technical_document_created_at
                    FROM documents d
                    WHERE d.tender_id = t.id
                      AND d.extracted_text IS NOT NULL
                      AND length(d.extracted_text) > 0
                      AND {PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION}
                ) primary_docs ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type = %s
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) blocked_event ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type = %s
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) requeue_event ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type IN (%s, %s)
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) partial_download_event ON TRUE
                ORDER BY a.score DESC, t.deadline_at ASC NULLS LAST
                {limit_clause};
                """,
                (
                    LEAD_TRIAGE_ANALYSIS_TYPE,
                    LEAD_LLM_REPORT_ANALYSIS_TYPE,
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    *SALES_WORKFLOW_STATUSES,
                    SALES_EXISTING_CLIENT_STATUS,
                    PREPARATION_BLOCKED_EVENT,
                    PREPARATION_REQUEUE_EVENT,
                    PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                    DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                    *params,
                ),
            )
            return cur.fetchall()


def get_tender_digest_row(tender_id: str) -> dict | None:
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    t.id AS tender_id,
                    t.*,
                    COALESCE(rb.score, 0) AS score,
                    COALESCE(rb.recommendation, 'maybe') AS recommendation,
                    COALESCE(rb.confidence, 'low') AS confidence,
                    COALESCE(rb.result, '{{}}'::jsonb) AS result,
                    dr.result AS document_risk_result,
                    dr.created_at AS document_risk_created_at,
                    lpr.result AS llm_report_result,
                    lpr.recommendation AS llm_report_recommendation,
                    lpr.created_at AS llm_report_created_at,
                    lpr.analysis_type AS llm_report_analysis_type,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment,
                    COALESCE(docs_stats.docs_count, 0) AS docs_count,
                    COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text,
                    COALESCE(docs_stats.docs_with_text, 0) AS documents_with_text_count,
                    primary_docs.latest_primary_technical_document_created_at,
                    blocked_event.message AS preparation_blocked_event,
                    requeue_event.message AS preparation_requeue_event,
                    partial_download_event.message AS partial_document_download_event
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT a.score, a.recommendation, a.confidence, a.result
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) rb ON TRUE
                LEFT JOIN LATERAL (
                    SELECT a.result, a.created_at
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('document_risk_analysis', 'document_risk')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) dr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT a.result, a.recommendation, a.created_at, a.analysis_type
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type LIKE %s
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) lpr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN ({sales_feedback_placeholders})
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS docs_count,
                        COUNT(*) FILTER (
                            WHERE d.extracted_text IS NOT NULL
                              AND length(d.extracted_text) > 0
                        ) AS docs_with_text
                    FROM documents d
                    WHERE d.tender_id = t.id
                ) docs_stats ON TRUE
                LEFT JOIN LATERAL (
                    SELECT max(d.created_at) AS latest_primary_technical_document_created_at
                    FROM documents d
                    WHERE d.tender_id = t.id
                      AND d.extracted_text IS NOT NULL
                      AND length(d.extracted_text) > 0
                      AND {PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION}
                ) primary_docs ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type = %s
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) blocked_event ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type = %s
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) requeue_event ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type IN (%s, %s)
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) partial_download_event ON TRUE
                WHERE t.id = %s
                LIMIT 1;
                """,
                (
                    "llm_presales_report%",
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    PREPARATION_BLOCKED_EVENT,
                    PREPARATION_REQUEUE_EVENT,
                    PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                    DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                    tender_id,
                ),
            )
            return cur.fetchone()


def build_tender_digest_card(
    tender_id: str,
    *,
    prefix: str | None = None,
    llm_result_override: dict | None = None,
) -> TenderDigestCard | None:
    tender = get_tender_digest_row(tender_id)
    if not tender:
        return None

    tender = dict(tender)
    if isinstance(llm_result_override, dict):
        tender["llm_report_result"] = llm_result_override

    llm_recommendation = recent_llm_recommendation(tender)
    if llm_recommendation:
        tender[RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY] = llm_recommendation

    message_text = format_card(tender)
    if prefix:
        message_text = f"{prefix}\n\n{message_text}"

    return tender_card_from_row(tender, message_text)


def select_digest_rows_with_stats(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict], DigestSelectionStats]:
    # SQL выбирает по rule-based score, а бизнес-логика применяется уже в Python.
    # Поэтому берём пул шире, затем убираем no_go и сортируем заново.
    del include_deadline_risk
    pool_limit = None if limit is None else max(limit * 5, 50)
    rows = get_digest_rows(limit=pool_limit)

    visible_rows = []
    hidden_by_feedback = 0
    hidden_no_go = 0
    hidden_by_lead_triage_reject = 0
    hidden_lead_hard_noise_count = 0

    for row in rows:
        if hidden_by_sales_feedback_for_lead_queue(row):
            hidden_by_feedback += 1
            continue

        if hidden_by_latest_lead_triage_reject(row):
            hidden_by_lead_triage_reject += 1
            continue

        if effective_recommendation(row) == "no_go":
            hidden_no_go += 1
            continue

        try:
            assessment = business_assessment(row)
        except Exception:
            if hidden_by_lead_hard_noise(row):
                hidden_lead_hard_noise_count += 1
            continue

        if hidden_by_lead_hard_noise(row, assessment):
            hidden_lead_hard_noise_count += 1
            continue

        if not is_working_digest_candidate(row, assessment):
            hidden_no_go += 1
            continue

        manual_reason = manual_document_reason_for_tender(row)
        if manual_reason:
            row = mark_manual_document_reason(row, manual_reason)

        visible_rows.append(row)

    visible_rows = dedupe_by_tender_or_external_id(visible_rows)
    visible_rows = sorted(visible_rows, key=digest_sort_key)
    if limit is not None:
        visible_rows = visible_rows[:limit]
    deadline_warning_count = sum(
        1 for row in visible_rows if lead_deadline_needs_warning(row, deadline_min_days, now=now)
    )

    stats = DigestSelectionStats(
        total_found=len(rows),
        shown=len(visible_rows),
        deadline_warning=deadline_warning_count,
        hidden_by_feedback=hidden_by_feedback,
        hidden_no_go=hidden_no_go,
        hidden_by_lead_triage_reject=hidden_by_lead_triage_reject,
        hidden_by_lead_hard_noise=hidden_lead_hard_noise_count,
    )
    return visible_rows, stats


def select_digest_rows(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    visible_rows, stats = select_digest_rows_with_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    return visible_rows, stats.deadline_warning


def get_visible_digest_rows(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    rows, _ = select_digest_rows(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    return rows


def build_digest_cards_with_selection_stats(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], DigestSelectionStats]:
    visible_rows, stats = select_digest_rows_with_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )

    if not visible_rows:
        return [], stats

    cards: list[TenderDigestCard] = []

    for index, tender in enumerate(visible_rows, start=1):
        if tender.get(MANUAL_DOCUMENT_REASON_KEY):
            cards.append(manual_document_needed_card_from_row(tender))
        else:
            cards.append(tender_card_from_row(tender, format_card(tender, index=index)))

    return cards, stats


def build_digest_cards_with_stats(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], int]:
    cards, stats = build_digest_cards_with_selection_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    return cards, stats.deadline_warning


def build_digest_cards(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[TenderDigestCard]:
    cards, _ = build_digest_cards_with_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    return cards


def build_digest_messages(
    limit: int | None = 15,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[str]:
    cards, stats = build_digest_cards_with_selection_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    deadline_warning_count = stats.deadline_warning

    if not cards:
        return [
            "AI Tender Radar: подходящих закупок для дайджеста пока нет. "
            f"С близким/истекшим сроком: {deadline_warning_count}. "
            f"{HIDDEN_BY_LEAD_HARD_NOISE}: {stats.hidden_by_lead_hard_noise}."
        ]

    header = (
        "AI Tender Radar - утренний shortlist\n"
        f"Найдено закупок для просмотра: {len(cards)}\n"
        f"С близким/истекшим сроком: {deadline_warning_count}\n"
        f"{HIDDEN_BY_LEAD_HARD_NOISE}: {stats.hidden_by_lead_hard_noise}"
    )
    return [header] + [card.message_text for card in cards]


def sales_feedback_timestamp(tender: dict) -> float:
    value = tender.get("latest_sales_status_created_at")
    if isinstance(value, datetime):
        return value.timestamp()

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    return 0.0


def get_in_work_digest_rows(limit: int = 100) -> list[dict]:
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))
    query = f"""
        SELECT
            COUNT(*) OVER() AS in_work_total_found,
            t.id AS tender_id,
            t.*,
            COALESCE(rb.score, 0) AS score,
            COALESCE(rb.recommendation, 'maybe') AS recommendation,
            COALESCE(rb.confidence, 'low') AS confidence,
            COALESCE(rb.result, '{{}}'::jsonb) AS result,
            COALESCE(rb.result, '{{}}'::jsonb) AS rule_based_result,
            dr.result AS document_risk_result,
            dr.created_at AS document_risk_created_at,
            ll.result AS llm_report_result,
            ll.recommendation AS llm_report_recommendation,
            ll.created_at AS llm_report_created_at,
            ll.analysis_type AS llm_report_analysis_type,
            sales_feedback.status AS latest_sales_status,
            sales_feedback.created_at AS latest_sales_status_created_at,
            sales_feedback.user_name AS latest_sales_user_name,
            sales_feedback.comment AS latest_sales_comment,
            COALESCE(docs_stats.docs_count, 0) AS docs_count,
            COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text,
            primary_docs.latest_primary_technical_document_created_at,
            blocked_event.message AS preparation_blocked_event,
            requeue_event.message AS preparation_requeue_event,
            partial_download_event.message AS partial_document_download_event
        FROM tenders t
        LEFT JOIN LATERAL (
            SELECT a.score, a.recommendation, a.confidence, a.result
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
            ORDER BY a.created_at DESC
            LIMIT 1
        ) rb ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.result, a.created_at
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type IN ('document_risk_analysis', 'document_risk')
            ORDER BY a.created_at DESC
            LIMIT 1
        ) dr ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.result, a.recommendation, a.created_at, a.analysis_type
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type LIKE %s
            ORDER BY a.created_at DESC
            LIMIT 1
        ) ll ON TRUE
        JOIN LATERAL (
            SELECT f.status, f.created_at, f.user_name, f.comment
            FROM feedback f
            WHERE f.tender_id = t.id
              AND f.status IN ({sales_feedback_placeholders})
            ORDER BY f.created_at DESC, f.id DESC
            LIMIT 1
        ) sales_feedback ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS docs_count,
                COUNT(*) FILTER (
                    WHERE d.extracted_text IS NOT NULL
                      AND length(d.extracted_text) > 0
                ) AS docs_with_text
            FROM documents d
            WHERE d.tender_id = t.id
        ) docs_stats ON TRUE
        LEFT JOIN LATERAL (
            SELECT max(d.created_at) AS latest_primary_technical_document_created_at
            FROM documents d
            WHERE d.tender_id = t.id
              AND d.extracted_text IS NOT NULL
              AND length(d.extracted_text) > 0
              AND {PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION}
        ) primary_docs ON TRUE
        LEFT JOIN LATERAL (
            SELECT pe.message
            FROM processing_events pe
            WHERE pe.tender_id = t.id
              AND pe.event_type = %s
            ORDER BY pe.created_at DESC
            LIMIT 1
        ) blocked_event ON TRUE
        LEFT JOIN LATERAL (
            SELECT pe.message
            FROM processing_events pe
            WHERE pe.tender_id = t.id
              AND pe.event_type = %s
            ORDER BY pe.created_at DESC
            LIMIT 1
        ) requeue_event ON TRUE
        LEFT JOIN LATERAL (
            SELECT pe.message
            FROM processing_events pe
            WHERE pe.tender_id = t.id
              AND pe.event_type IN (%s, %s)
            ORDER BY pe.created_at DESC
            LIMIT 1
        ) partial_download_event ON TRUE
        WHERE sales_feedback.status = %s
          AND (t.deadline_at IS NULL OR t.deadline_at >= now())
        ORDER BY sales_feedback.created_at DESC, t.deadline_at ASC NULLS LAST, t.id DESC
        LIMIT %s;
    """

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                query,
                (
                    "llm_presales_report%",
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    PREPARATION_BLOCKED_EVENT,
                    PREPARATION_REQUEUE_EVENT,
                    PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                    DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                    SALES_IN_WORK_STATUS,
                    limit,
                ),
            )
            return cur.fetchall()


def select_in_work_digest_rows(
    limit: int = 100,
    *,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    pool_limit = max(limit * 2, 100)
    rows = get_in_work_digest_rows(limit=pool_limit)
    selected: list[dict] = []
    hidden_expired = 0

    for row in rows:
        if latest_sales_workflow_status(row) != SALES_IN_WORK_STATUS:
            continue

        if not deadline_is_active(row, now=now):
            hidden_expired += 1
            continue

        selected.append(row)

    selected = sorted(
        selected,
        key=lambda row: (sales_feedback_timestamp(row), str(row.get("tender_id") or "")),
        reverse=True,
    )[:limit]
    return selected, hidden_expired


def build_in_work_digest_cards_with_stats(
    limit: int = 100,
    *,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], int]:
    rows, hidden_expired = select_in_work_digest_rows(limit=limit, now=now)
    cards: list[TenderDigestCard] = []

    for index, tender in enumerate(rows, start=1):
        manual_reason = manual_document_reason_for_tender(tender)
        if manual_reason:
            cards.append(manual_document_needed_card_from_row(mark_manual_document_reason(tender, manual_reason)))
        else:
            cards.append(tender_card_from_row(tender, format_card(tender, index=index)))

    return cards, hidden_expired


def build_in_work_digest_cards(
    limit: int = 100,
    *,
    now: datetime | None = None,
) -> list[TenderDigestCard]:
    cards, _ = build_in_work_digest_cards_with_stats(limit=limit, now=now)
    return cards


def positive_recent_hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ожидается число часов") from exc

    if hours <= 0:
        raise argparse.ArgumentTypeError("количество часов должно быть больше нуля")

    return hours


def format_recent_hours(hours: float) -> str:
    return f"{hours:g}"


def recent_llm_cutoff(hours: float, *, now: datetime | None = None) -> datetime:
    current_time = now or datetime.now(timezone.utc)
    return current_time - timedelta(hours=hours)


def llm_report_is_recent(tender: dict, cutoff: datetime) -> bool:
    report_created_at = tender.get("llm_report_created_at")
    if not isinstance(report_created_at, datetime):
        return False

    if report_created_at.tzinfo is None and cutoff.tzinfo is not None:
        report_created_at = report_created_at.replace(tzinfo=cutoff.tzinfo)

    return report_created_at >= cutoff


def normalized_recommendation(value: Any) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in {"go", "maybe", "no_go"} else None


def _report_from_result(value: Any) -> dict[str, Any]:
    return report_payload_from_result(value)


def lead_report_stored_recommendation_from_row(row: dict[str, Any]) -> str | None:
    if row.get("llm_report_analysis_type"):
        return normalized_recommendation(row.get("llm_report_recommendation"))
    if row.get("analysis_type"):
        return normalized_recommendation(row.get("recommendation"))
    return None


def has_customer_lead_report(row: dict[str, Any]) -> bool:
    analysis_type = str(
        row.get("llm_report_analysis_type") or row.get("analysis_type") or ""
    ).strip()
    return analysis_type == LEAD_LLM_REPORT_ANALYSIS_TYPE


def lead_report_priority_from_row(row: dict[str, Any]) -> str:
    report = _report_from_result(row.get("llm_report_result") or row.get("result"))
    priority = report.get("lead_priority")
    if isinstance(priority, dict):
        priority = priority.get("priority") or priority.get("value")
    normalized = str(priority or "").strip().lower()
    return normalized if normalized in {"high", "medium", "low"} else ""


def lead_report_priority_rank(row: dict[str, Any]) -> int:
    return {
        "high": 0,
        "medium": 1,
        "low": 2,
    }.get(lead_report_priority_from_row(row), 3)


def has_latest_lead_triage(row: dict[str, Any]) -> bool:
    return any(
        row.get(key) is not None
        for key in (
            "lead_triage_analysis_type",
            "lead_triage_created_at",
            "lead_triage_result",
            "lead_triage_recommendation",
            "lead_triage_decision",
        )
    )


def report_created_not_before_triage(row: dict[str, Any]) -> bool:
    report_created_at = parse_report_datetime(
        row.get("llm_report_created_at") or row.get("created_at")
    )
    triage_created_at = parse_report_datetime(row.get("lead_triage_created_at"))
    if report_created_at is None or triage_created_at is None:
        return True
    if report_created_at.tzinfo is None and triage_created_at.tzinfo is not None:
        report_created_at = report_created_at.replace(tzinfo=triage_created_at.tzinfo)
    elif report_created_at.tzinfo is not None and triage_created_at.tzinfo is None:
        triage_created_at = triage_created_at.replace(tzinfo=report_created_at.tzinfo)
    return report_created_at >= triage_created_at


def datetime_after(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    if left.tzinfo is None and right.tzinfo is not None:
        left = left.replace(tzinfo=right.tzinfo)
    elif left.tzinfo is not None and right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left > right


def operational_lead_report_created_at_from_row(row: dict[str, Any]) -> datetime | None:
    operational_created = parse_report_datetime(row.get("operational_lead_report_created_at"))
    if operational_created is not None:
        return operational_created

    if has_customer_lead_report(row):
        return parse_report_datetime(row.get("llm_report_created_at") or row.get("created_at"))

    return None


def operational_lead_report_is_newer_than_triage(row: dict[str, Any]) -> bool:
    return datetime_after(
        operational_lead_report_created_at_from_row(row),
        parse_report_datetime(row.get("lead_triage_created_at")),
    )


def latest_positive_or_working_sales_feedback_overrides_latest_triage(
    row: dict[str, Any],
) -> bool:
    if latest_positive_or_working_sales_feedback_status(row) is None:
        return False

    triage_created_at = parse_report_datetime(row.get("lead_triage_created_at"))
    feedback_created_at = parse_report_datetime(row.get("latest_sales_status_created_at"))
    if triage_created_at is None or feedback_created_at is None:
        return True
    return datetime_after(feedback_created_at, triage_created_at)


def hidden_by_latest_lead_triage_reject(row: dict[str, Any]) -> bool:
    if not has_latest_lead_triage(row):
        return False
    if lead_triage_decision_from_row(row) != "reject":
        return False
    if operational_lead_report_is_newer_than_triage(row):
        return False
    return not latest_positive_or_working_sales_feedback_overrides_latest_triage(row)


def operational_queue_decision_from_row(row: dict[str, Any]) -> str | None:
    if not has_customer_lead_report(row):
        return None

    direct = normalized_recommendation(row.get("queue_decision"))
    if direct:
        return direct

    if has_latest_lead_triage(row):
        triage_decision = lead_triage_decision_from_row(row)
        if (
            triage_decision == "go"
            and report_created_not_before_triage(row)
        ):
            return "go"
        if triage_decision == "maybe":
            return "maybe"
        if operational_lead_report_is_newer_than_triage(row):
            stored = lead_report_stored_recommendation_from_row(row)
            if stored in LEAD_DIGEST_VISIBLE_RECOMMENDATIONS:
                return stored
        if latest_positive_or_working_sales_feedback_overrides_latest_triage(row):
            stored = lead_report_stored_recommendation_from_row(row)
            if stored in LEAD_DIGEST_VISIBLE_RECOMMENDATIONS:
                return stored
        return None

    stored = lead_report_stored_recommendation_from_row(row)
    if stored in LEAD_DIGEST_VISIBLE_RECOMMENDATIONS:
        return stored
    return None


def is_operational_lead_report_row(row: dict[str, Any]) -> bool:
    return operational_queue_decision_from_row(row) in LEAD_DIGEST_VISIBLE_RECOMMENDATIONS


def _lead_report_created_sort_value(row: dict[str, Any]) -> float:
    created_at = row.get("llm_report_created_at") or row.get("created_at")
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.timestamp()
    return 0.0


def digest_identity_key(row: dict[str, Any]) -> str:
    full = raw_full(row)
    value = (
        row.get("external_id")
        or full.get("orderNumber")
        or row.get("tender_id")
        or row.get("id")
    )
    return str(value or "").strip()


def has_operational_lead_report_payload(row: dict[str, Any]) -> bool:
    return has_customer_lead_report(row) and bool(lead_report_body(row))


def dedupe_by_tender_or_external_id(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def preference(row: dict[str, Any]) -> tuple[int, float, int]:
        return (
            1 if has_operational_lead_report_payload(row) else 0,
            _lead_report_created_sort_value(row),
            0 if row.get(MANUAL_DOCUMENT_REASON_KEY) else 1,
        )

    for row in rows:
        key = digest_identity_key(row)
        if not key:
            key = f"row:{id(row)}"
        if key not in selected:
            selected[key] = row
            order.append(key)
            continue
        if preference(row) > preference(selected[key]):
            selected[key] = row

    return [selected[key] for key in order]


def lead_report_created_at_from_row(row: dict[str, Any]) -> datetime | None:
    return parse_report_datetime(row.get("llm_report_created_at") or row.get("created_at"))


def lead_report_created_in_range(
    row: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
) -> bool:
    return datetime_in_range(
        lead_report_created_at_from_row(row),
        start=start,
        end=end,
    )


def operational_lead_report_sort_key(row: dict[str, Any]) -> tuple[int, float, str]:
    return (
        lead_report_priority_rank(row),
        -_lead_report_created_sort_value(row),
        str(row.get("tender_id") or row.get("id") or ""),
    )


def operational_lead_status_label(row: dict[str, Any]) -> str:
    if latest_sales_workflow_status(row) == SALES_IN_WORK_STATUS:
        return "В работе"
    if latest_sales_workflow_status(row) == SALES_EXISTING_CLIENT_STATUS:
        return "Существующий клиент"
    return "Новый"


def operational_lead_total_found(rows: list[dict[str, Any]], fallback: int) -> int:
    for row in rows:
        try:
            return int(
                row.get("lead_total_found")
                or row.get("lead_in_work_total_found")
                or fallback
            )
        except (TypeError, ValueError):
            return fallback
    return fallback


def operational_lead_hidden_reason(row: dict[str, Any]) -> str | None:
    if not has_customer_lead_report(row):
        return HIDDEN_BY_STATUS

    if not lead_report_body(row):
        return HIDDEN_BY_MISSING_REPORT_FIELDS

    if latest_sales_workflow_status(row) == SALES_IN_WORK_STATUS:
        return HIDDEN_BY_FEEDBACK
    if latest_negative_sales_feedback_status(row):
        return HIDDEN_BY_FEEDBACK

    stored_recommendation = lead_report_stored_recommendation_from_row(row)
    triage_decision = lead_triage_decision_from_row(row) if has_latest_lead_triage(row) else ""
    if hidden_by_latest_lead_triage_reject(row):
        return HIDDEN_BY_LEAD_TRIAGE_REJECT

    if stored_recommendation == "no_go":
        return HIDDEN_BY_NO_GO

    if hidden_by_lead_hard_noise(row):
        return HIDDEN_BY_LEAD_HARD_NOISE

    if operational_queue_decision_from_row(row) not in LEAD_DIGEST_VISIBLE_RECOMMENDATIONS:
        return HIDDEN_BY_SELECTION_POLICY

    return None


def operational_lead_all_selection_reasons(
    rows: list[dict[str, Any]],
    *,
    limit: int | None = 100,
) -> list[tuple[dict[str, Any], str]]:
    eligible_rows = [
        row for row in rows if operational_lead_hidden_reason(row) is None
    ]
    sorted_rows = sorted(eligible_rows, key=operational_lead_report_sort_key)
    selected_rows = sorted_rows if limit is None else sorted_rows[:limit]
    selected_ids = {id(row) for row in selected_rows}
    eligible_ids = {id(row) for row in sorted_rows}

    reasons: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        row_id = id(row)
        if row_id in selected_ids:
            reasons.append((row, DIGEST_REASON_SHOWN))
        elif row_id in eligible_ids:
            reasons.append((row, HIDDEN_BY_DIGEST_LIMIT))
        else:
            reasons.append((row, operational_lead_hidden_reason(row) or HIDDEN_BY_SELECTION_POLICY))
    return reasons


def split_operational_lead_queue_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int = 15,
) -> tuple[list[dict[str, Any]], OperationalLeadQueueStats]:
    report_rows = dedupe_by_tender_or_external_id(
        [row for row in rows if has_customer_lead_report(row)]
    )
    visible_rows: list[dict[str, Any]] = []
    hidden_in_work = 0
    hidden_negative_feedback = 0
    hidden_by_lead_triage_reject = 0
    hidden_lead_hard_noise_count = 0
    candidates: list[dict[str, Any]] = []

    for row in report_rows:
        if hidden_by_latest_lead_triage_reject(row):
            hidden_by_lead_triage_reject += 1
            continue
        if not is_operational_lead_report_row(row):
            continue
        candidates.append(row)
        if latest_sales_workflow_status(row) == SALES_IN_WORK_STATUS:
            hidden_in_work += 1
            continue
        if latest_negative_sales_feedback_status(row):
            hidden_negative_feedback += 1
            continue
        if hidden_by_lead_hard_noise(row):
            hidden_lead_hard_noise_count += 1
            continue
        visible_rows.append(row)

    selected = sorted(visible_rows, key=operational_lead_report_sort_key)[:limit]
    stats = OperationalLeadQueueStats(
        total_found=operational_lead_total_found(candidates, len(candidates)),
        shown=len(selected),
        hidden_in_work=hidden_in_work,
        hidden_negative_feedback=hidden_negative_feedback,
        hidden_by_lead_triage_reject=hidden_by_lead_triage_reject,
        hidden_by_lead_hard_noise=hidden_lead_hard_noise_count,
    )
    return selected, stats


def select_operational_lead_report_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int = 15,
) -> list[dict[str, Any]]:
    selected, _ = split_operational_lead_queue_rows(rows, limit=limit)
    return selected


def split_operational_lead_all_rows(
    rows: list[dict[str, Any]],
    *,
    limit: int | None = 100,
) -> tuple[list[dict[str, Any]], OperationalLeadAllStats]:
    report_rows = dedupe_by_tender_or_external_id(
        [row for row in rows if has_customer_lead_report(row)]
    )
    visible_rows: list[dict[str, Any]] = []
    hidden_negative_feedback = 0
    hidden_in_work = 0
    hidden_by_lead_triage_reject = 0
    hidden_lead_hard_noise_count = 0
    candidates: list[dict[str, Any]] = []

    for row in report_rows:
        if hidden_by_latest_lead_triage_reject(row):
            hidden_by_lead_triage_reject += 1
            continue
        if not is_operational_lead_report_row(row):
            continue
        candidates.append(row)
        if latest_sales_workflow_status(row) == SALES_IN_WORK_STATUS:
            hidden_in_work += 1
            continue
        if latest_negative_sales_feedback_status(row):
            hidden_negative_feedback += 1
            continue
        if hidden_by_lead_hard_noise(row):
            hidden_lead_hard_noise_count += 1
            continue
        visible_rows.append(row)

    selected = sorted(visible_rows, key=operational_lead_report_sort_key)
    visible_count = len(selected)
    if limit is not None:
        selected = selected[:limit]
    hidden_by_digest_limit = max(0, visible_count - len(selected))

    stats = OperationalLeadAllStats(
        total_found=operational_lead_total_found(candidates, len(candidates)),
        shown=len(selected),
        new_count=visible_count,
        in_work_count=0,
        hidden_in_work=hidden_in_work,
        hidden_negative_feedback=hidden_negative_feedback,
        hidden_by_digest_limit=hidden_by_digest_limit,
        hidden_by_lead_triage_reject=hidden_by_lead_triage_reject,
        hidden_by_lead_hard_noise=hidden_lead_hard_noise_count,
    )
    return selected, stats


def get_operational_lead_report_rows(
    limit: int | None = 15,
    *,
    report_created_from: datetime | None = None,
    report_created_to: datetime | None = None,
) -> list[dict[str, Any]]:
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))
    sales_workflow_placeholders = sql_placeholders(len(SALES_WORKFLOW_STATUSES))
    limit_clause = "LIMIT %s" if limit is not None else ""
    report_created_clause = ""
    report_created_params: tuple[Any, ...] = ()
    if report_created_from is not None:
        report_created_clause += "\n                      AND lr_report.created_at >= %s"
        report_created_params = (*report_created_params, report_created_from)
    if report_created_to is not None:
        report_created_clause += "\n                      AND lr_report.created_at < %s"
        report_created_params = (*report_created_params, report_created_to)
    params: tuple[Any, ...] = (
        LEAD_TRIAGE_ANALYSIS_TYPE,
        LEAD_LLM_REPORT_ANALYSIS_TYPE,
        *report_created_params,
        "go",
        "maybe",
        "go",
        "maybe",
        *SALES_FEEDBACK_LOOKUP_STATUSES,
        *SALES_WORKFLOW_STATUSES,
        SALES_EXISTING_CLIENT_STATUS,
    )
    if limit is not None:
        params = (*params, limit)

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) OVER() AS lead_total_found,
                    t.id AS tender_id,
                    t.*,
                    COALESCE(rb.score, 0) AS score,
                    COALESCE(rb.recommendation, 'maybe') AS recommendation,
                    COALESCE(rb.confidence, 'low') AS confidence,
                    COALESCE(rb.result, '{{}}'::jsonb) AS result,
                    COALESCE(rb.result, '{{}}'::jsonb) AS rule_based_result,
                    lr.result AS llm_report_result,
                    lr.recommendation AS llm_report_recommendation,
                    lr.confidence AS llm_report_confidence,
                    lr.created_at AS llm_report_created_at,
                    lr.analysis_type AS llm_report_analysis_type,
                    triage.result AS lead_triage_result,
                    triage.recommendation AS lead_triage_recommendation,
                    triage.confidence AS lead_triage_confidence,
                    triage.created_at AS lead_triage_created_at,
                    triage.analysis_type AS lead_triage_analysis_type,
                    triage.lead_decision AS lead_triage_decision,
                    CASE
                      WHEN triage.created_at IS NOT NULL
                       AND triage.lead_decision = 'go'
                       AND lr.created_at >= triage.created_at THEN 'go'
                      WHEN triage.created_at IS NOT NULL
                       AND triage.lead_decision = 'maybe' THEN 'maybe'
                      ELSE lower(replace(coalesce(lr.recommendation, ''), '-', '_'))
                    END AS queue_decision,
                    CASE
                      WHEN triage.created_at IS NOT NULL THEN 'triage'
                      ELSE 'legacy_report'
                    END AS queue_decision_source,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment,
{EXISTING_CLIENT_CUSTOMER_SELECT_SQL}
                    COALESCE(docs_stats.docs_count, 0) AS docs_count,
                    COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT a.score, a.recommendation, a.confidence, a.result
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) rb ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        result,
                        recommendation,
                        confidence,
                        created_at,
                        analysis_type,
                        lower(replace(coalesce(
                            result #>> '{{report,lead_decision}}',
                            result #>> '{{lead_decision}}',
                            result #>> '{{report,decision}}',
                            result #>> '{{decision}}',
                            recommendation,
                            ''
                        ), '-', '_')) AS lead_decision
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) triage ON TRUE
                JOIN LATERAL (
                    SELECT result, recommendation, confidence, created_at, analysis_type
                    FROM (
                      SELECT result, recommendation, confidence, created_at, analysis_type
                      FROM analysis_results lr_report
                      WHERE lr_report.tender_id = t.id
                        AND lr_report.analysis_type = %s
                        {report_created_clause}
                      ORDER BY lr_report.created_at DESC
                      LIMIT 1
                    ) lr_report
                    WHERE (
                        (
                          triage.created_at IS NOT NULL
                          AND triage.lead_decision = 'go'
                          AND created_at >= triage.created_at
                        )
                        OR (
                          triage.created_at IS NOT NULL
                          AND triage.lead_decision = 'maybe'
                        )
                        OR (
                          triage.created_at IS NULL
                          AND lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)
                        )
                        OR (
                          triage.created_at IS NOT NULL
                          AND created_at > triage.created_at
                          AND lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)
                        )
                      )
                    LIMIT 1
                ) lr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN ({sales_feedback_placeholders})
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                {existing_client_customer_feedback_sql(sales_workflow_placeholders)}
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS docs_count,
                        COUNT(*) FILTER (
                            WHERE d.extracted_text IS NOT NULL
                              AND length(d.extracted_text) > 0
                        ) AS docs_with_text
                    FROM documents d
                    WHERE d.tender_id = t.id
                ) docs_stats ON TRUE
                ORDER BY
                  CASE lower(coalesce(
                    lr.result #>> '{{report,lead_priority,priority}}',
                    lr.result #>> '{{report,lead_priority}}'
                  ))
                    WHEN 'high' THEN 0
                    WHEN 'medium' THEN 1
                    WHEN 'low' THEN 2
                    ELSE 3
                  END,
                  lr.created_at DESC
                {limit_clause};
                """,
                params,
            )
            return cur.fetchall()


def select_operational_lead_all_rows(
    limit: int | None = 100,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], OperationalLeadAllStats]:
    del now
    pool_limit = None if limit is None else max(limit * 5, 100)
    rows = get_operational_lead_report_rows(limit=pool_limit)
    return split_operational_lead_all_rows(rows, limit=limit)


def operational_lead_all_summary_text(stats: OperationalLeadAllStats) -> str:
    lines = [
        "AI Tender Radar - необработанные клиентские лиды",
        f"Необработанных лидов в очереди: {stats.new_count}",
        f"Скрыто как \"в работе\": {stats.hidden_in_work}",
        f"Исключено негативным feedback: {stats.hidden_negative_feedback}",
        f"Скрыто latest lead triage reject ({HIDDEN_BY_LEAD_TRIAGE_REJECT}): {stats.hidden_by_lead_triage_reject}",
        f"{HIDDEN_BY_LEAD_HARD_NOISE}: {stats.hidden_by_lead_hard_noise}",
    ]
    if stats.shown != stats.new_count:
        lines.append(f"Показано в сообщении: {stats.shown}")
    if stats.hidden_by_digest_limit:
        lines.append(
            f"Скрыто лимитом digest ({HIDDEN_BY_DIGEST_LIMIT}): {stats.hidden_by_digest_limit}"
        )
    if stats.new_count == 0:
        lines.append("Необработанных клиентских лидов пока нет.")
    return "\n".join(lines)


def select_operational_lead_queue_rows(
    limit: int = 15,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], OperationalLeadQueueStats]:
    start, end = moscow_today_bounds(now)
    rows = get_operational_lead_report_rows(
        limit=None,
        report_created_from=start,
        report_created_to=end,
    )
    today_rows = [
        row
        for row in rows
        if lead_report_created_in_range(row, start=start, end=end)
    ]
    return split_operational_lead_queue_rows(today_rows, limit=limit)


def operational_lead_queue_summary_text(stats: OperationalLeadQueueStats) -> str:
    lines = [
        "AI Tender Radar - новые клиентские лиды сегодня",
        f"Новых лидов сегодня: {stats.shown}",
        f"Скрыто как \"в работе\": {stats.hidden_in_work}",
        f"Скрыто негативным feedback: {stats.hidden_negative_feedback}",
        f"Скрыто latest lead triage reject ({HIDDEN_BY_LEAD_TRIAGE_REJECT}): {stats.hidden_by_lead_triage_reject}",
        f"{HIDDEN_BY_LEAD_HARD_NOISE}: {stats.hidden_by_lead_hard_noise}",
    ]
    if stats.shown == 0:
        lines.append("Новых клиентских лидов за сегодня пока нет.")
    return "\n".join(lines)


def build_operational_lead_queue_cards_with_stats(
    limit: int = 15,
    *,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], OperationalLeadQueueStats]:
    rows, stats = select_operational_lead_queue_rows(limit=limit, now=now)
    cards = [
        tender_card_from_row(tender, format_caller_lead_card(tender, index=index))
        for index, tender in enumerate(rows, start=1)
    ]
    return cards, stats


def format_all_lead_card(tender: dict, index: int | None = None) -> str:
    lines = format_caller_lead_card(tender, index=index).splitlines()
    status_line = f"Статус: {operational_lead_status_label(tender)}"
    if len(lines) > 1:
        lines.insert(1, status_line)
    else:
        lines.append(status_line)
    return "\n".join(lines)


def build_operational_lead_all_cards_with_stats(
    limit: int | None = 100,
    *,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], OperationalLeadAllStats]:
    rows, stats = select_operational_lead_all_rows(limit=limit, now=now)
    cards = [
        tender_card_from_row(tender, format_all_lead_card(tender, index=index))
        for index, tender in enumerate(rows, start=1)
    ]
    return cards, stats


def get_operational_lead_report_in_work_rows(limit: int = 100) -> list[dict[str, Any]]:
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) OVER() AS lead_in_work_total_found,
                    t.id AS tender_id,
                    t.*,
                    COALESCE(rb.score, 0) AS score,
                    COALESCE(rb.recommendation, 'maybe') AS recommendation,
                    COALESCE(rb.confidence, 'low') AS confidence,
                    COALESCE(rb.result, '{{}}'::jsonb) AS result,
                    COALESCE(rb.result, '{{}}'::jsonb) AS rule_based_result,
                    lr.result AS llm_report_result,
                    lr.recommendation AS llm_report_recommendation,
                    lr.confidence AS llm_report_confidence,
                    lr.created_at AS llm_report_created_at,
                    lr.analysis_type AS llm_report_analysis_type,
                    triage.result AS lead_triage_result,
                    triage.recommendation AS lead_triage_recommendation,
                    triage.confidence AS lead_triage_confidence,
                    triage.created_at AS lead_triage_created_at,
                    triage.analysis_type AS lead_triage_analysis_type,
                    triage.lead_decision AS lead_triage_decision,
                    CASE
                      WHEN triage.created_at IS NOT NULL
                       AND triage.lead_decision = 'go'
                       AND lr.created_at >= triage.created_at THEN 'go'
                      WHEN triage.created_at IS NOT NULL
                       AND triage.lead_decision = 'maybe' THEN 'maybe'
                      ELSE lower(replace(coalesce(lr.recommendation, ''), '-', '_'))
                    END AS queue_decision,
                    CASE
                      WHEN triage.created_at IS NOT NULL THEN 'triage'
                      ELSE 'legacy_report'
                    END AS queue_decision_source,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment,
                    COALESCE(docs_stats.docs_count, 0) AS docs_count,
                    COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT a.score, a.recommendation, a.confidence, a.result
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) rb ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        result,
                        recommendation,
                        confidence,
                        created_at,
                        analysis_type,
                        lower(replace(coalesce(
                            result #>> '{{report,lead_decision}}',
                            result #>> '{{lead_decision}}',
                            result #>> '{{report,decision}}',
                            result #>> '{{decision}}',
                            recommendation,
                            ''
                        ), '-', '_')) AS lead_decision
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) triage ON TRUE
                JOIN LATERAL (
                    SELECT result, recommendation, confidence, created_at, analysis_type
                    FROM (
                      SELECT result, recommendation, confidence, created_at, analysis_type
                      FROM analysis_results lr_report
                      WHERE lr_report.tender_id = t.id
                        AND lr_report.analysis_type = %s
                      ORDER BY lr_report.created_at DESC
                      LIMIT 1
                    ) lr_report
                    WHERE (
                        (
                          triage.created_at IS NOT NULL
                          AND triage.lead_decision = 'go'
                          AND created_at >= triage.created_at
                        )
                        OR (
                          triage.created_at IS NOT NULL
                          AND triage.lead_decision = 'maybe'
                        )
                        OR (
                          triage.created_at IS NULL
                          AND lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)
                        )
                        OR (
                          triage.created_at IS NOT NULL
                          AND created_at > triage.created_at
                          AND lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)
                        )
                      )
                    LIMIT 1
                ) lr ON TRUE
                JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN ({sales_feedback_placeholders})
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                LEFT JOIN LATERAL (
                    SELECT
                        COUNT(*) AS docs_count,
                        COUNT(*) FILTER (
                            WHERE d.extracted_text IS NOT NULL
                              AND length(d.extracted_text) > 0
                        ) AS docs_with_text
                    FROM documents d
                    WHERE d.tender_id = t.id
                ) docs_stats ON TRUE
                WHERE sales_feedback.status = %s
                ORDER BY sales_feedback.created_at DESC, lr.created_at DESC, t.id DESC
                LIMIT %s;
                """,
                (
                    LEAD_TRIAGE_ANALYSIS_TYPE,
                    LEAD_LLM_REPORT_ANALYSIS_TYPE,
                    "go",
                    "maybe",
                    "go",
                    "maybe",
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    SALES_IN_WORK_STATUS,
                    limit,
                ),
            )
            return cur.fetchall()


def select_operational_lead_in_work_rows(
    limit: int = 100,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], OperationalLeadInWorkStats]:
    del now
    source_rows = dedupe_by_tender_or_external_id(
        get_operational_lead_report_in_work_rows(limit=max(limit * 2, 100))
    )
    rows = [
        row
        for row in source_rows
        if is_operational_lead_report_row(row)
        and latest_sales_workflow_status(row) == SALES_IN_WORK_STATUS
    ]
    selected = sorted(
        rows,
        key=lambda row: (sales_feedback_timestamp(row), _lead_report_created_sort_value(row)),
        reverse=True,
    )[:limit]
    total_found = operational_lead_total_found(
        rows,
        len(rows),
    )
    stats = OperationalLeadInWorkStats(total_found=total_found, shown=len(selected))
    return selected, stats


def operational_lead_in_work_summary_text(stats: OperationalLeadInWorkStats) -> str:
    lines = [
        "AI Tender Radar - лиды в работе",
        f"Лидов в работе: {stats.shown}",
    ]
    if stats.hidden_expired:
        lines.append(f"Скрыто просроченных: {stats.hidden_expired}")
    if stats.shown == 0:
        lines.append("Лидов в работе пока нет.")
    return "\n".join(lines)


def build_operational_lead_in_work_cards_with_stats(
    limit: int = 100,
    *,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], OperationalLeadInWorkStats]:
    rows, stats = select_operational_lead_in_work_rows(limit=limit, now=now)
    cards = [
        tender_card_from_row(tender, format_caller_lead_card(tender, index=index))
        for index, tender in enumerate(rows, start=1)
    ]
    return cards, stats


def recent_llm_recommendation(tender: dict) -> str | None:
    result = tender.get("llm_report_result") or {}
    if (
        isinstance(result, dict)
        and llm_report_is_stale_after_primary_technical_document(tender, result)
    ):
        return None

    report = result_report(result) if isinstance(result, dict) else {}
    recommendation = report.get("recommendation")

    candidates = []
    if isinstance(recommendation, dict):
        candidates.append(recommendation.get("decision"))
    else:
        candidates.append(recommendation)

    candidates.extend(
        [
            report.get("verdict"),
            report.get("decision"),
            tender.get("llm_report_recommendation"),
        ]
    )

    normalized_candidates = []
    for candidate in candidates:
        normalized = normalized_recommendation(candidate)
        if normalized:
            normalized_candidates.append(normalized)

    if "no_go" in normalized_candidates:
        return "no_go"

    recommendation = normalized_candidates[0] if normalized_candidates else None
    if recommendation is None and has_customer_lead_report(tender):
        recommendation = {
            "high": "go",
            "medium": "maybe",
            "low": "maybe",
        }.get(lead_report_priority_from_row(tender))

    if (
        recommendation == "go"
        and isinstance(result, dict)
        and llm_report_is_preliminary_without_technical_spec(tender, result)
    ):
        return "maybe"

    return recommendation


def with_recent_llm_recommendation(tender: dict) -> dict:
    recommendation = recent_llm_recommendation(tender)
    if not recommendation:
        return tender

    row = dict(tender)
    row[RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY] = recommendation
    return row


def get_recent_llm_digest_rows(
    hours: float,
    limit: int = 15,
    *,
    cutoff: datetime | None = None,
    include_labeled: bool = False,
) -> list[dict]:
    report_cutoff = cutoff or recent_llm_cutoff(hours)
    analysis_type_predicate = (
        "lr.analysis_type LIKE %s" if include_labeled else "lr.analysis_type = %s"
    )
    analysis_type_param = (
        f"{LEAD_LLM_REPORT_ANALYSIS_TYPE}%" if include_labeled else LEAD_LLM_REPORT_ANALYSIS_TYPE
    )
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    t.id AS tender_id,
                    t.*,
                    a.score,
                    a.recommendation,
                    a.confidence,
                    a.result,
                    dr.result AS document_risk_result,
                    dr.created_at AS document_risk_created_at,
                    lpr.result AS llm_report_result,
                    lpr.recommendation AS llm_report_recommendation,
                    lpr.created_at AS llm_report_created_at,
                    lpr.analysis_type AS llm_report_analysis_type,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment,
                    primary_docs.latest_primary_technical_document_created_at,
                    partial_download_event.message AS partial_document_download_event
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT a.score, a.recommendation, a.confidence, a.result
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('rule_based_score', 'rule_based_scoring')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) a ON TRUE
                LEFT JOIN analysis_results dr
                  ON dr.tender_id = t.id
                 AND dr.analysis_type = 'document_risk_analysis'
                JOIN LATERAL (
                    SELECT lr.result, lr.recommendation, lr.created_at, lr.analysis_type
                    FROM analysis_results lr
                    WHERE lr.tender_id = t.id
                      AND {analysis_type_predicate}
                      AND lr.created_at >= %s
                    ORDER BY lr.created_at DESC
                    LIMIT 1
                ) lpr ON TRUE
                LEFT JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN ({sales_feedback_placeholders})
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                LEFT JOIN LATERAL (
                    SELECT max(d.created_at) AS latest_primary_technical_document_created_at
                    FROM documents d
                    WHERE d.tender_id = t.id
                      AND d.extracted_text IS NOT NULL
                      AND length(d.extracted_text) > 0
                      AND {PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION}
                ) primary_docs ON TRUE
                LEFT JOIN LATERAL (
                    SELECT pe.message
                    FROM processing_events pe
                    WHERE pe.tender_id = t.id
                      AND pe.event_type IN (%s, %s)
                    ORDER BY pe.created_at DESC
                    LIMIT 1
                ) partial_download_event ON TRUE
                ORDER BY lpr.created_at DESC, a.score DESC, t.deadline_at ASC NULLS LAST
                LIMIT %s;
                """,
                (
                    analysis_type_param,
                    report_cutoff,
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                    DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                    limit,
                ),
            )
            return cur.fetchall()


def select_recent_llm_digest_rows(
    hours: float,
    limit: int = 15,
    *,
    include_no_go: bool = False,
    include_labeled: bool = False,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict], int, int]:
    del include_deadline_risk
    cutoff = recent_llm_cutoff(hours, now=now)
    pool_limit = max(limit * 5, 50)
    rows = get_recent_llm_digest_rows(
        hours,
        limit=pool_limit,
        cutoff=cutoff,
        include_labeled=include_labeled,
    )
    rows = dedupe_by_tender_or_external_id(rows)

    visible_rows: list[dict] = []
    hidden_no_go = 0

    for row in rows:
        if not llm_report_is_recent(row, cutoff):
            continue

        if hidden_by_sales_feedback_for_lead_queue(row):
            continue

        row = with_recent_llm_recommendation(row)
        if row.get(RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY) == "no_go":
            if include_no_go:
                visible_rows.append(row)
            else:
                hidden_no_go += 1
            continue

        if row.get(RECENT_DIGEST_RECOMMENDATION_OVERRIDE_KEY) in LEAD_DIGEST_VISIBLE_RECOMMENDATIONS:
            visible_rows.append(row)

    selected = sorted(
        visible_rows,
        key=_lead_report_created_sort_value,
        reverse=True,
    )[:limit]
    deadline_warning_count = sum(
        1 for row in selected if lead_deadline_needs_warning(row, deadline_min_days, now=now)
    )
    return selected, hidden_no_go, deadline_warning_count


def get_visible_recent_llm_digest_rows(
    hours: float,
    limit: int = 15,
    *,
    include_no_go: bool = False,
    include_labeled: bool = False,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    rows, _, _ = select_recent_llm_digest_rows(
        hours,
        limit=limit,
        include_no_go=include_no_go,
        include_labeled=include_labeled,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    return rows


def format_recent_llm_card(tender: dict, index: int | None = None) -> str:
    if has_customer_lead_report(tender):
        return format_caller_lead_card(tender, index=index)
    return format_card(tender, index=index)


def build_recent_llm_digest_cards_with_stats(
    hours: float,
    limit: int = 15,
    *,
    include_no_go: bool = False,
    include_labeled: bool = False,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], int, int]:
    visible_rows, hidden_no_go, deadline_warning_count = select_recent_llm_digest_rows(
        hours,
        limit=limit,
        include_no_go=include_no_go,
        include_labeled=include_labeled,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )

    cards: list[TenderDigestCard] = []
    for index, tender in enumerate(visible_rows, start=1):
        cards.append(tender_card_from_row(tender, format_recent_llm_card(tender, index=index)))

    return cards, hidden_no_go, deadline_warning_count


def build_recent_llm_digest_cards(
    hours: float,
    limit: int = 15,
    *,
    include_no_go: bool = False,
    include_labeled: bool = False,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[TenderDigestCard]:
    cards, _, _ = build_recent_llm_digest_cards_with_stats(
        hours,
        limit=limit,
        include_no_go=include_no_go,
        include_labeled=include_labeled,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    return cards


def build_recent_llm_digest_messages(
    hours: float,
    limit: int = 15,
    *,
    include_no_go: bool = False,
    include_labeled: bool = False,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[str]:
    visible_rows, hidden_no_go, deadline_warning_count = select_recent_llm_digest_rows(
        hours,
        limit=limit,
        include_no_go=include_no_go,
        include_labeled=include_labeled,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    cards = [
        tender_card_from_row(tender, format_recent_llm_card(tender, index=index))
        for index, tender in enumerate(visible_rows, start=1)
    ]
    hours_text = format_recent_hours(hours)

    if not cards:
        if include_no_go:
            return [
                f"AI Tender Radar: диагностический recent LLM digest за последние {hours_text} ч "
                "(debug: включая no_go). Свежих LLM-отчетов нет. "
                f"С близким/истекшим сроком: {deadline_warning_count}."
            ]

        return [
            f"AI Tender Radar: свежих рабочих LLM-отчетов за последние {hours_text} ч нет. "
            f"Скрыто no_go: {hidden_no_go}. "
            f"С близким/истекшим сроком: {deadline_warning_count}."
        ]

    if include_no_go:
        header = (
            f"AI Tender Radar - диагностический recent LLM digest за последние {hours_text} ч "
            "(debug: включая no_go)\n"
            "Диагностический режим: это не рабочий digest; no_go показаны только для проверки.\n"
            f"Найдено свежих LLM-отчетов: {len(cards)}\n"
            f"С близким/истекшим сроком: {deadline_warning_count}"
        )
    else:
        header = (
            f"AI Tender Radar - свежие LLM-отчеты за последние {hours_text} ч\n"
            f"Найдено свежих рабочих LLM-отчетов: {len(cards)}\n"
            f"Скрыто no_go: {hidden_no_go}\n"
            f"С близким/истекшим сроком: {deadline_warning_count}"
        )
    return [header] + [card.message_text for card in cards]


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.digest")
    parser.add_argument(
        "--recent-llm-hours",
        type=positive_recent_hours,
        help="показать только тендеры со свежими LLM-отчетами за N часов",
    )
    parser.add_argument(
        "--deadline-min-days",
        type=non_negative_days,
        default=DEFAULT_DEADLINE_MIN_DAYS,
        help="минимум дней до окончания подачи для рабочих digest-выдач",
    )
    parser.add_argument(
        "--include-deadline-risk",
        action="store_true",
        help="debug/admin: не скрывать просроченные и близкие дедлайны",
    )
    parser.add_argument(
        "--debug-include-no-go",
        dest="include_no_go",
        action="store_true",
        help="debug/admin: в recent-режиме также показать свежие LLM-отчеты с verdict no_go",
    )
    parser.add_argument(
        "--include-no-go",
        dest="include_no_go",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--include-labeled",
        action="store_true",
        help="debug/admin: include labeled LLM reports such as smoke/compare runs in recent digest",
    )
    args = parser.parse_args()

    if args.recent_llm_hours is None:
        messages = build_digest_messages(
            deadline_min_days=args.deadline_min_days,
            include_deadline_risk=args.include_deadline_risk,
        )
    else:
        messages = build_recent_llm_digest_messages(
            args.recent_llm_hours,
            include_no_go=args.include_no_go,
            include_labeled=args.include_labeled,
            deadline_min_days=args.deadline_min_days,
            include_deadline_risk=args.include_deadline_risk,
        )

    for message in messages:
        print(message)
        print()
        print("---")
        print()


def processing_status_block(tender: dict) -> str:
    docs_count = _non_negative_int(tender.get("docs_count"))
    docs_with_text = _non_negative_int(tender.get("docs_with_text"))
    api_count = api_documents_count(tender)

    has_document_risk = bool(tender.get("document_risk_result"))

    llm_result = tender.get("llm_report_result") or {}
    has_llm = bool(isinstance(llm_result, dict) and result_report(llm_result))

    lines: list[str] = []

    if api_count is not None and api_count > docs_count:
        if docs_count == 0:
            lines.append(f"📄 Документы не скачаны; в карточке API доступно: {api_count}")
        elif docs_with_text == 0:
            lines.append(
                "📄 Документы скачаны, но текст не извлечён: "
                f"0 из {api_count} доступных; скачано {docs_count}"
            )
        elif docs_with_text < docs_count:
            lines.append(
                "📄 Извлечён текст: "
                f"{docs_with_text} из {api_count} доступных; скачано {docs_count}"
            )
        else:
            lines.append(f"📄 Документы обработаны: {docs_with_text} из {api_count} доступных")
    elif docs_count == 0:
        lines.append("📄 Документы не скачаны")
    elif docs_with_text == 0:
        lines.append("📄 Документы скачаны, но текст не извлечён")
    elif docs_with_text < docs_count:
        lines.append(f"📄 Извлечён текст не по всем документам ({docs_with_text}/{docs_count})")
    else:
        lines.append(f"📄 Документы обработаны ({docs_with_text}/{docs_count})")

    if has_unprocessed_important_api_documents(tender, docs_count):
        lines.append("⚠️ Есть необработанные документы, похожие на КД/ТЗ/спецификацию.")

    partial_warning = partial_document_download_warning_text(tender)
    if partial_warning:
        lines.append(partial_warning)

    if not has_document_risk:
        lines.append("🔎 Анализ документов не выполнялся")

    if not has_llm:
        lines.append("🤖 LLM-анализ ещё не выполнялся")

    return "\n".join(lines)


def parse_processing_event_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def today_preparation_skip_reason(tender: dict) -> str | None:
    direct_values = {
        str(tender.get(key) or "").strip().lower()
        for key in (
            "preparation_skip_reason",
            "preparation_status",
            "preparation_reason",
            "technical_preparation_status",
            "document_state",
            "warning_reason",
        )
    }

    if (
        "preparation_blocked=marketplace_auth" in direct_values
        or "blocked_by_marketplace_auth" in direct_values
        or "marketplace_auth" in direct_values
    ):
        return "preparation_blocked=marketplace_auth"
    if "preparation_no_valid_documents" in direct_values or "no_valid_documents" in direct_values:
        return "preparation_no_valid_documents"
    if "preparation_exhausted=documents_missing" in direct_values or "documents_missing" in direct_values:
        return "preparation_exhausted=documents_missing"
    if "preparation_exhausted=not_ready_for_llm" in direct_values or "not_ready_for_llm" in direct_values:
        return "preparation_exhausted=not_ready_for_llm"
    if (
        "preparation_missing_primary_technical_document" in direct_values
        or "primary_technical_document_missing" in direct_values
        or "primary_technical_document_not_processed" in direct_values
    ):
        return "preparation_missing_primary_technical_document"

    blocked_payload = parse_processing_event_payload(tender.get("preparation_blocked_event"))
    blocked_status = str(blocked_payload.get("status") or "").lower()
    blocked_reason = str(blocked_payload.get("reason") or "").lower()

    if blocked_status == "blocked_by_marketplace_auth" or blocked_reason in {
        "marketplace_auth",
        "external_marketplace_auth_required",
    }:
        return "preparation_blocked=marketplace_auth"

    if blocked_status == "no_valid_documents" or blocked_reason == "no_valid_documents":
        return "preparation_no_valid_documents"

    requeue_payload = parse_processing_event_payload(tender.get("preparation_requeue_event"))
    if not requeue_payload.get("exhausted"):
        return None

    requeue_reason = str(requeue_payload.get("reason") or "").lower()
    if requeue_reason in {"documents_missing", "preparation_exhausted=documents_missing"}:
        return "preparation_exhausted=documents_missing"
    if requeue_reason in {"not_ready_for_llm", "preparation_exhausted=not_ready_for_llm"}:
        return "preparation_exhausted=not_ready_for_llm"
    if requeue_reason in {
        "primary_technical_document_missing",
        "primary_technical_document_not_processed",
        "preparation_missing_primary_technical_document",
    }:
        return "preparation_missing_primary_technical_document"

    documents_downloaded = _non_negative_int(requeue_payload.get("documents_downloaded"))
    documents_with_text = _non_negative_int(requeue_payload.get("documents_with_text"))

    if documents_downloaded <= 0:
        return "preparation_exhausted=documents_missing"
    if documents_with_text <= 0:
        return "preparation_exhausted=not_ready_for_llm"

    return None


def today_manual_document_reason(preparation_reason: str | None) -> str | None:
    if preparation_reason in MANUAL_DOCUMENT_REASONS:
        return preparation_reason

    return None


def digest_documents_with_text_count(tender: dict) -> int:
    containers: list[Any] = [tender, tender.get("document_risk_result")]
    result = tender.get("llm_report_result")
    if isinstance(result, dict):
        containers.append(result.get("meta"))
        containers.append(result.get("llm_metadata"))

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in (
            "docs_with_text",
            "docs_with_text_count",
            "documents_with_text",
            "documents_with_text_count",
        ):
            if key in container:
                count = _non_negative_int(container.get(key))
                if count > 0:
                    return count

    return 0


def tender_has_extracted_document_text(tender: dict) -> bool:
    return digest_documents_with_text_count(tender) > 0


def manual_document_reason_for_tender(tender: dict) -> str | None:
    if tender_has_extracted_document_text(tender):
        return None

    return today_manual_document_reason(today_preparation_skip_reason(tender))


def mark_manual_document_reason(tender: dict, reason: str) -> dict:
    row = dict(tender)
    row[MANUAL_DOCUMENT_REASON_KEY] = reason
    row[TODAY_MANUAL_DOCUMENT_REASON_KEY] = reason
    return row


def working_digest_exclusion_reason(
    tender: dict,
    assessment: dict[str, Any] | None = None,
) -> str | None:
    if assessment is None:
        assessment = business_assessment(tender)

    action = str(assessment.get("action") or "").strip()
    if action in WORKING_DIGEST_EXCLUDED_ACTIONS:
        return f"business_action={action}"

    market_access = str(assessment.get("market_access") or "").strip()
    if market_access in WORKING_DIGEST_EXCLUDED_MARKET_ACCESS:
        return f"market_access={market_access}"

    target_category_reason = working_digest_target_category_skip_reason(
        tender,
        assessment,
    )
    if target_category_reason:
        return target_category_reason

    return None


def working_digest_target_category_skip_reason(
    tender: dict,
    assessment: dict[str, Any],
) -> str | None:
    market_access = str(assessment.get("market_access") or "").strip()
    if market_access != "unknown":
        return None

    try:
        from app.business_profile import load_business_profile, match_target_category

        category_name, category_cfg = match_target_category(
            tender,
            load_business_profile(),
        )
    except Exception:
        return None

    if not category_name or not category_cfg:
        return "no_target_category"

    return None


def is_working_digest_candidate(
    tender: dict,
    assessment: dict[str, Any] | None = None,
) -> bool:
    return working_digest_exclusion_reason(tender, assessment) is None


def today_manual_document_reason_text(reason: str | None) -> str:
    return {
        "preparation_blocked=marketplace_auth": "документы требуют авторизации на внешней площадке.",
        "preparation_no_valid_documents": "документы не найдены автоматически.",
        "preparation_exhausted=documents_missing": "документы отсутствуют / не готовы.",
        "preparation_missing_primary_technical_document": "основной технический документ не обработан.",
    }.get(reason, "документы отсутствуют / не готовы.")


def manual_document_action_text(reason: str | None) -> str:
    if reason == "preparation_blocked=marketplace_auth":
        return (
            "открыть площадку по ссылке, скачать КД/ТЗ/спецификацию "
            "и загрузить документ вручную."
        )

    if reason == "preparation_no_valid_documents":
        return (
            "найти КД/ТЗ на площадке или запросить у заказчика, "
            "затем загрузить документ вручную."
        )

    if reason == "preparation_missing_primary_technical_document":
        return (
            "скачать и обработать основной ТЗ/описание объекта закупки, "
            "затем запустить разбор заново."
        )

    return (
        "открыть площадку по ссылке или запросить КД/ТЗ/спецификацию "
        "у заказчика, затем загрузить документ вручную."
    )


def manual_document_not_ready_message(reason: str | None) -> str:
    if reason == "preparation_blocked=marketplace_auth":
        detail = "требуется авторизация на внешней площадке"
    elif reason == "preparation_no_valid_documents":
        detail = "документы не найдены автоматически"
    elif reason == "preparation_missing_primary_technical_document":
        detail = "основной технический документ не обработан"
    else:
        detail = "документы отсутствуют или не готовы"

    return (
        f"Документы для LLM-разбора не готовы: {detail}. "
        "Скачайте КД/ТЗ/спецификацию и загрузите документ вручную."
    )


def manual_document_user_reason_text(reason: str | None) -> str:
    if reason == "preparation_blocked=marketplace_auth":
        return "документы закрыты на площадке, нужно скачать КД/ТЗ вручную."
    return "документы закрыты на площадке или не отдаются автоматически, нужно скачать КД/ТЗ вручную."


def manual_document_importance_text(tender: dict) -> str:
    return outreach_category_cfg(outreach_category_key(tender))["reason"]


def normalize_today_digest_row(row: dict) -> dict:
    tender = dict(row)
    rule_based_result = tender.get("rule_based_result")

    if tender.get("result") is None:
        tender["result"] = rule_based_result if isinstance(rule_based_result, dict) else {}

    if tender.get("recommendation") is None and isinstance(tender.get("result"), dict):
        tender["recommendation"] = tender["result"].get("recommendation")

    if tender.get("score") is None:
        tender["score"] = 0

    return tender


def today_rows_total_found(rows: list[dict]) -> int:
    if not rows:
        return 0

    try:
        return int(rows[0].get("today_total_found") or len(rows))
    except (TypeError, ValueError):
        return len(rows)


def today_audit_price(tender: dict, full: dict[str, Any]) -> Any:
    return (
        tender.get("initial_price")
        or tender.get("max_price")
        or tender.get("price")
        or full.get("maxPrice")
        or full.get("nmck")
    )


def today_audit_deadline(tender: dict, full: dict[str, Any]) -> Any:
    return (
        tender.get("application_deadline")
        or tender.get("end_date")
        or tender.get("bidding_deadline")
        or tender.get("deadline_at")
        or full.get("endDate")
        or full.get("filingEndDate")
    )


def one_line_text(value: Any, limit: int = 90) -> str:
    return " ".join(short_text(str(value) if value is not None else None, limit).split())


def has_llm_report(tender: dict) -> bool:
    result = tender.get("llm_report_result") or {}
    if not isinstance(result, dict):
        return False

    report = result.get("report") or {}
    if not isinstance(report, dict) or not report:
        return False

    return not llm_report_is_stale_after_primary_technical_document(tender, result)


def direct_today_filter_reason(tender: dict) -> str | None:
    raw_values = []
    for key in (
        "filter_reason",
        "skip_reason",
        "selection_skip_reason",
        "debug_skip_reason",
        "preparation_skip_reason",
        "preparation_reason",
    ):
        raw_values.append(str(tender.get(key) or "").strip().lower())

    joined = " ".join(raw_values)
    for marker in (
        "preparation_blocked=marketplace_auth",
        "preparation_no_valid_documents",
        "documents_missing",
        NON_CORE_TRANSPORT_SECURITY_REASON,
        "service_noise",
        "domestic_restricted",
        "price_below_min",
        "already_has_llm_report",
    ):
        if marker in joined:
            return marker

    return None


def today_no_go_filter_reason(tender: dict, assessment: dict[str, Any]) -> str:
    direct_reason = direct_today_filter_reason(tender)
    if direct_reason in {
        NON_CORE_TRANSPORT_SECURITY_REASON,
        "service_noise",
        "domestic_restricted",
        "price_below_min",
        "already_has_llm_report",
    }:
        return direct_reason

    market_access = assessment.get("market_access")
    if market_access in {NON_CORE_TRANSPORT_SECURITY_REASON, "service_noise", "domestic_restricted"}:
        return str(market_access)
    if market_access == "low_priority_deal" or assessment.get("action") == "skip_low_priority":
        return "low_priority_deal"

    return "no_go"


def today_shown_reason(tender: dict, assessment: dict[str, Any]) -> str:
    parts: list[str] = []
    market_access = str(assessment.get("market_access") or "").strip()

    if market_access == "target_hardware":
        parts.append("рабочий hardware-case")
    elif market_access and market_access != "unknown":
        parts.append(market_access)
    else:
        parts.append("рабочий today-case")

    parts.append("срок подачи справочно")

    if has_llm_report(tender):
        parts.append("already_has_llm_report")

    docs_with_text = _non_negative_int(tender.get("docs_with_text"))
    docs_count = _non_negative_int(tender.get("docs_count"))
    if docs_with_text > 0:
        parts.append("документы готовы")
    elif docs_count > 0:
        parts.append("документы без extracted_text")
    else:
        parts.append("документы не найдены")

    return ", ".join(parts)


def today_audit_documents_text(tender: dict, reason: str | None = None) -> str:
    if reason == "preparation_blocked=marketplace_auth":
        return "требуют авторизации на внешней площадке"
    if reason == "preparation_no_valid_documents":
        return "не найдены автоматически"
    if reason == "preparation_exhausted=documents_missing" or reason == "documents_missing":
        return "не скачались автоматически"
    if reason == "preparation_exhausted=not_ready_for_llm":
        return "есть, extracted_text нет"

    docs_count = _non_negative_int(tender.get("docs_count"))
    docs_with_text = _non_negative_int(tender.get("docs_with_text"))

    if docs_count <= 0:
        return "не найдены автоматически"
    if docs_with_text <= 0:
        return f"есть, extracted_text нет (0/{docs_count})"

    return f"есть, extracted_text есть ({docs_with_text}/{docs_count})"


def today_filter_audit_decision(
    tender: dict,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> TodayFilterAuditEntry:
    del deadline_min_days, include_deadline_risk
    if hidden_by_sales_feedback_for_lead_queue(tender):
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=HIDDEN_BY_FEEDBACK,
            bucket="hidden_by_feedback",
            documents=today_audit_documents_text(tender, HIDDEN_BY_FEEDBACK),
        )

    if hidden_by_latest_lead_triage_reject(tender):
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=HIDDEN_BY_LEAD_TRIAGE_REJECT,
            bucket="hidden_by_lead_triage_reject",
            documents=today_audit_documents_text(tender, HIDDEN_BY_LEAD_TRIAGE_REJECT),
        )

    if recent_llm_recommendation(tender) == "no_go":
        reason = "no_go"
        try:
            reason = today_no_go_filter_reason(tender, business_assessment(tender))
        except Exception:
            pass
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=reason,
            bucket="hidden_no_go",
            documents=today_audit_documents_text(tender, reason),
        )

    if hidden_by_lead_hard_noise(tender):
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=HIDDEN_BY_LEAD_HARD_NOISE,
            bucket="hidden_by_lead_hard_noise",
            documents=today_audit_documents_text(tender, HIDDEN_BY_LEAD_HARD_NOISE),
        )

    try:
        assessment = business_assessment(tender)
        recommendation = effective_recommendation(tender)
    except Exception:
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason="other",
            bucket="skipped_unclassified",
            documents=today_audit_documents_text(tender, "other"),
        )

    if hidden_by_lead_hard_noise(tender, assessment):
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=HIDDEN_BY_LEAD_HARD_NOISE,
            bucket="hidden_by_lead_hard_noise",
            documents=today_audit_documents_text(tender, HIDDEN_BY_LEAD_HARD_NOISE),
        )

    if recommendation == "no_go" or not is_working_digest_candidate(tender, assessment):
        reason = today_no_go_filter_reason(tender, assessment)
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=reason,
            bucket="hidden_no_go",
            documents=today_audit_documents_text(tender, reason),
        )

    preparation_reason = today_preparation_skip_reason(tender)
    manual_reason = manual_document_reason_for_tender(tender)
    if manual_reason:
        return TodayFilterAuditEntry(
            tender=tender,
            status="НУЖНЫ ДОКУМЕНТЫ",
            reason=manual_reason,
            bucket="manual_documents",
            documents=today_audit_documents_text(tender, manual_reason),
        )

    if preparation_reason:
        return TodayFilterAuditEntry(
            tender=tender,
            status="СКРЫТО",
            reason=preparation_reason,
            bucket="hidden_other",
            documents=today_audit_documents_text(tender, preparation_reason),
        )

    return TodayFilterAuditEntry(
        tender=tender,
        status="ПОКАЗАНО",
        reason=today_shown_reason(tender, assessment),
        bucket="shown",
        documents=today_audit_documents_text(tender),
    )


def today_audit_business_category(tender: dict) -> str:
    containers: list[Any] = [
        tender,
        tender.get("result"),
        tender.get("rule_based_result"),
    ]

    llm_result = tender.get("llm_report_result") or {}
    if isinstance(llm_result, dict):
        containers.append(llm_result.get("report"))
        meta = llm_result.get("meta")
        containers.append(meta)
        if isinstance(meta, dict):
            containers.append(meta.get("spec_facts"))

    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in (
            "_llm_category_label",
            "_llm_category",
            "business_category",
            "detected_category",
            "category",
            "target_category_label",
            "target_category",
        ):
            value = str(container.get(key) or "").strip()
            if value:
                return value

    try:
        assessment = business_assessment(tender)
    except Exception:
        return "не указана"

    return str(assessment.get("market_access") or "не указана")


def today_filter_audit_status_text(status: str) -> str:
    if status == "ПОКАЗАНО":
        return 'ПОКАЗАНО в "Сегодня"'
    if status == "НУЖНЫ ДОКУМЕНТЫ":
        return "НУЖНЫ ДОКУМЕНТЫ"
    return 'СКРЫТО из "Сегодня"'


def today_filter_reason_text(reason: str) -> str:
    if reason == NON_CORE_TRANSPORT_SECURITY_REASON:
        return (
            "нецелевая вертикаль: транспортная безопасность / дорожная инфраструктура "
            f"({NON_CORE_TRANSPORT_SECURITY_REASON})"
        )

    return reason


def today_filter_audit_card(entry: TodayFilterAuditEntry) -> str:
    tender = entry.tender
    full = raw_full(tender)
    external_id = (
        full.get("orderNumber")
        or tender.get("external_id")
        or tender.get("tender_id")
        or tender.get("id")
        or "не указан"
    )
    title = one_line_text(tender.get("title") or "Без названия", 95)
    url = procedure_url(tender, full)

    lines = [
        f"{external_id} | {title} | {money(today_audit_price(tender, full))}",
        f"Категория: {today_audit_business_category(tender)}",
        f"Срок подачи: {date_text(today_audit_deadline(tender, full))}",
        f"Статус: {today_filter_audit_status_text(entry.status)}",
        f"Причина: {today_filter_reason_text(entry.reason)}",
        f"Документы: {entry.documents}",
        f"Ссылка: {url or 'не найдена'}",
    ]

    return "\n".join(lines)


def lead_triage_report_body(row: dict[str, Any]) -> dict[str, Any]:
    return _report_from_result(row.get("lead_triage_result") or row.get("llm_report_result"))


def lead_triage_decision_from_row(row: dict[str, Any]) -> str:
    report = lead_triage_report_body(row)
    raw = (
        report.get("lead_decision")
        or report.get("decision")
        or row.get("lead_triage_decision")
        or row.get("lead_triage_recommendation")
        or row.get("llm_report_recommendation")
    )
    normalized = str(raw or "").strip().lower().replace("-", "_")
    if normalized in {"go", "maybe"}:
        return normalized
    if normalized in {"reject", "rejected", "no_go", "nogo", "no"}:
        return "reject"
    return ""


def lead_triage_reason_from_row(row: dict[str, Any]) -> str:
    report = lead_triage_report_body(row)
    decision = lead_triage_decision_from_row(row)
    if decision == "reject":
        reason = report.get("reject_reason") or report.get("lead_summary")
    elif decision == "maybe":
        reason = (
            report.get("reject_reason")
            or report.get("lead_summary")
            or report.get("likely_customer_story")
        )
    else:
        reason = report.get("lead_summary") or report.get("likely_customer_story")
    return short_text(str(reason or "причина не указана"), 360)


def get_lead_triage_audit_rows(limit: int = 100) -> list[dict[str, Any]]:
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    t.id AS tender_id,
                    t.*,
                    COALESCE(rb.score, 0) AS score,
                    COALESCE(rb.recommendation, 'maybe') AS recommendation,
                    COALESCE(rb.confidence, 'low') AS confidence,
                    COALESCE(rb.result, '{{}}'::jsonb) AS result,
                    COALESCE(rb.result, '{{}}'::jsonb) AS rule_based_result,
                    triage.result AS lead_triage_result,
                    triage.recommendation AS lead_triage_recommendation,
                    triage.confidence AS lead_triage_confidence,
                    triage.created_at AS lead_triage_created_at,
                    triage.analysis_type AS lead_triage_analysis_type,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT a.score, a.recommendation, a.confidence, a.result
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) rb ON TRUE
                JOIN LATERAL (
                    SELECT result, recommendation, confidence, created_at, analysis_type
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) triage ON TRUE
                LEFT JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN ({sales_feedback_placeholders})
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                ORDER BY triage.created_at DESC, t.id DESC
                LIMIT %s;
                """,
                (
                    LEAD_TRIAGE_ANALYSIS_TYPE,
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    limit,
                ),
            )
            return cur.fetchall()


def get_lead_processed_audit_rows(limit: int = 100) -> list[dict[str, Any]]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    t.id AS tender_id,
                    t.*,
                    lr.result AS llm_report_result,
                    lr.recommendation AS llm_report_recommendation,
                    lr.confidence AS llm_report_confidence,
                    lr.created_at AS llm_report_created_at,
                    lr.analysis_type AS llm_report_analysis_type,
                    sales_feedback.status AS latest_sales_status,
                    sales_feedback.created_at AS latest_sales_status_created_at,
                    sales_feedback.user_name AS latest_sales_user_name,
                    sales_feedback.comment AS latest_sales_comment
                FROM tenders t
                JOIN LATERAL (
                    SELECT result, recommendation, confidence, created_at, analysis_type
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                      AND lower(replace(coalesce(recommendation, ''), '-', '_')) IN (%s, %s)
                    ORDER BY created_at DESC
                    LIMIT 1
                ) lr ON TRUE
                JOIN LATERAL (
                    SELECT f.status, f.created_at, f.user_name, f.comment
                    FROM feedback f
                    WHERE f.tender_id = t.id
                      AND f.status IN (%s, %s)
                    ORDER BY f.created_at DESC, f.id DESC
                    LIMIT 1
                ) sales_feedback ON TRUE
                ORDER BY sales_feedback.created_at DESC, lr.created_at DESC
                LIMIT %s;
                """,
                (
                    LEAD_LLM_REPORT_ANALYSIS_TYPE,
                    "go",
                    "maybe",
                    SALES_IN_WORK_STATUS,
                    SALES_NOT_RELEVANT_STATUS,
                    limit,
                ),
            )
            return cur.fetchall()


def latest_lead_hard_noise_summary() -> dict[str, int] | None:
    return None


def lead_audit_row_title(row: dict[str, Any]) -> str:
    full = raw_full(row)
    external_id = (
        full.get("orderNumber")
        or row.get("external_id")
        or row.get("tender_id")
        or row.get("id")
        or "не указан"
    )
    title = one_line_text(row.get("title") or "Без названия", 95)
    return f"{external_id} | {title}"


def lead_audit_card_text(
    row: dict[str, Any],
    *,
    section: str,
    decision_text: str,
    reason: str | None = None,
) -> str:
    full = raw_full(row)
    url = procedure_url(row, full)
    lines = [
        section,
        lead_audit_row_title(row),
        f"Решение: {decision_text}",
    ]
    if reason:
        lines.append(f"Причина: {reason}")

    report = lead_report_body(row)
    if section == "Отобраны как лиды":
        signal = lead_report_signal_text(row, report)
        needs = lead_report_possible_needs(report)
        if signal:
            lines.append(f"Сигнал по заказчику: {signal}")
        if needs:
            lines.append(f"Возможные потребности: {', '.join(needs[:3])}")

    lines.append(f"Ссылка: {url or 'не найдена'}")
    return "\n".join(lines)


def lead_audit_card_from_row(
    row: dict[str, Any],
    *,
    section: str,
    status: str,
    decision_text: str,
    reason: str | None = None,
) -> TodayFilterAuditCard:
    card = tender_card_from_row(
        row,
        lead_audit_card_text(
            row,
            section=section,
            decision_text=decision_text,
            reason=reason,
        ),
    )
    return TodayFilterAuditCard(
        tender_id=card.tender_id,
        message_text=card.message_text,
        status=status,
        reason=reason or "",
        external_id=card.external_id,
        title=card.title,
    )


def today_filter_audit_summary_text(stats: TodayFilterAuditStats) -> str:
    hard_noise_line = (
        f"Отсечены hard-noise: {stats.hard_noise_total}"
        if stats.hard_noise_diagnostics_available
        else "Отсечены hard-noise: diagnostics доступны в run log"
    )
    lines = [
        "AI Tender Radar - Аудит отбора",
        "Показывает, почему сигналы попали или не попали в очередь клиентских лидов.",
        "",
        f"Отобраны как лиды: {stats.go_leads}",
        f"Отложены: {stats.maybe_leads}",
        f"Отклонены triage: {stats.rejected_triage}",
        f"Скрыто latest lead triage reject ({HIDDEN_BY_LEAD_TRIAGE_REJECT}): {stats.hidden_by_lead_triage_reject}",
        hard_noise_line,
        (
            "Уже обработаны: "
            f"{stats.processed_total} "
            f"(уже в работе: {stats.processed_in_work}, "
            f"отклонен пользователем: {stats.processed_not_relevant})"
        ),
    ]

    if stats.total_found > stats.limit:
        lines.append(f"Показаны первые {stats.limit} из {stats.total_found}")

    return "\n".join(lines)


def pack_today_filter_audit_messages(
    summary: str,
    cards: list[str],
    *,
    message_limit: int = TODAY_FILTER_AUDIT_MESSAGE_LIMIT,
) -> list[str]:
    messages = [summary]
    current = ""

    for card in cards:
        candidate = card if not current else f"{current}\n\n{card}"
        if current and len(candidate) > message_limit:
            messages.append(current)
            current = card
        else:
            current = candidate

    if current:
        messages.append(current)

    return messages


def build_today_filter_audit_entries_with_stats(
    limit: int = TODAY_FILTER_AUDIT_DEFAULT_LIMIT,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[TodayFilterAuditEntry], TodayFilterAuditStats]:
    rows = [
        normalize_today_digest_row(row)
        for row in get_digest_rows_today(limit=None)
    ]
    entries = [
        today_filter_audit_decision(
            row,
            deadline_min_days=deadline_min_days,
            include_deadline_risk=include_deadline_risk,
            now=now,
        )
        for row in rows
    ]
    total_found = today_rows_total_found(rows)
    listed_entries = entries[:limit]

    stats = TodayFilterAuditStats(
        total_found=total_found,
        would_show=sum(1 for entry in entries if entry.bucket == "shown"),
        manual_documents_needed=sum(1 for entry in entries if entry.bucket == "manual_documents"),
        hidden_no_go=sum(1 for entry in entries if entry.bucket == "hidden_no_go"),
        hidden_deadline=sum(1 for entry in entries if entry.bucket == "hidden_deadline"),
        hidden_by_lead_triage_reject=sum(
            1 for entry in entries if entry.bucket == "hidden_by_lead_triage_reject"
        ),
        hidden_by_lead_hard_noise=sum(
            1 for entry in entries if entry.bucket == "hidden_by_lead_hard_noise"
        ),
        hidden_by_feedback=sum(
            1 for entry in entries if entry.bucket == "hidden_by_feedback"
        ),
        hidden_other=sum(
            1
            for entry in entries
            if entry.bucket in {"hidden_other", "skipped_unclassified"}
        ),
        listed=len(listed_entries),
        limit=limit,
    )

    return listed_entries, stats


def today_filter_audit_card_from_entry(entry: TodayFilterAuditEntry) -> TodayFilterAuditCard:
    tender = entry.tender
    card = tender_card_from_row(tender, today_filter_audit_card(entry))
    return TodayFilterAuditCard(
        tender_id=card.tender_id,
        message_text=card.message_text,
        status=entry.status,
        reason=entry.reason,
        external_id=card.external_id,
        title=card.title,
    )


def build_today_filter_audit_cards_with_stats(
    limit: int = TODAY_FILTER_AUDIT_DEFAULT_LIMIT,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[TodayFilterAuditCard], TodayFilterAuditStats]:
    del deadline_min_days, include_deadline_risk
    go_rows, go_stats = select_operational_lead_all_rows(limit=limit, now=now)
    triage_rows = get_lead_triage_audit_rows(limit=max(limit * 2, 100))
    processed_rows = get_lead_processed_audit_rows(limit=max(limit, 100))
    hard_noise_summary = latest_lead_hard_noise_summary()
    hard_noise_total = sum(hard_noise_summary.values()) if hard_noise_summary else 0

    maybe_rows = [
        row for row in triage_rows if lead_triage_decision_from_row(row) == "maybe"
    ]
    reject_rows = [
        row for row in triage_rows if lead_triage_decision_from_row(row) == "reject"
    ]
    processed_in_work = sum(
        1
        for row in processed_rows
        if latest_sales_workflow_status(row) == SALES_IN_WORK_STATUS
    )
    processed_not_relevant = sum(
        1
        for row in processed_rows
        if latest_sales_workflow_status(row) == SALES_NOT_RELEVANT_STATUS
    )

    cards: list[TodayFilterAuditCard] = []

    def append_card(card: TodayFilterAuditCard) -> None:
        if len(cards) < limit:
            cards.append(card)

    for row in go_rows:
        append_card(
            lead_audit_card_from_row(
                row,
                section="Отобраны как лиды",
                status="ОТОБРАН",
                decision_text="отобран как клиентский лид",
            )
        )
    for row in maybe_rows:
        append_card(
            lead_audit_card_from_row(
                row,
                section="Отложены",
                status="ОТЛОЖЕН",
                decision_text="отложен для ручной проверки",
                reason=lead_triage_reason_from_row(row),
            )
        )
    for row in reject_rows:
        append_card(
            lead_audit_card_from_row(
                row,
                section="Отклонены triage",
                status="ОТКЛОНЕН",
                decision_text="отклонен как нерелевантный сигнал",
                reason=lead_triage_reason_from_row(row),
            )
        )
    for row in processed_rows:
        workflow_status = latest_sales_workflow_status(row)
        if workflow_status == SALES_IN_WORK_STATUS:
            decision_text = "уже в работе"
        elif workflow_status == SALES_NOT_RELEVANT_STATUS:
            decision_text = "отклонен пользователем"
        else:
            continue
        append_card(
            lead_audit_card_from_row(
                row,
                section="Уже обработаны",
                status="ОБРАБОТАН",
                decision_text=decision_text,
                reason=HIDDEN_BY_FEEDBACK,
            )
        )

    total_found = (
        go_stats.shown
        + len(maybe_rows)
        + len(reject_rows)
        + len(processed_rows)
        + hard_noise_total
    )
    stats = TodayFilterAuditStats(
        total_found=total_found,
        would_show=go_stats.shown,
        hidden_no_go=len(reject_rows),
        hidden_by_lead_triage_reject=len(reject_rows),
        hidden_other=hard_noise_total,
        listed=len(cards),
        limit=limit,
        go_leads=go_stats.shown,
        maybe_leads=len(maybe_rows),
        rejected_triage=len(reject_rows),
        hard_noise_total=hard_noise_total,
        hard_noise_diagnostics_available=hard_noise_summary is not None,
        processed_in_work=processed_in_work,
        processed_not_relevant=processed_not_relevant,
        processed_total=processed_in_work + processed_not_relevant,
    )
    return cards, stats


def build_today_filter_audit_messages(
    limit: int = TODAY_FILTER_AUDIT_DEFAULT_LIMIT,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
    message_limit: int = TODAY_FILTER_AUDIT_MESSAGE_LIMIT,
) -> list[str]:
    cards, stats = build_today_filter_audit_cards_with_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    summary = today_filter_audit_summary_text(stats)
    return pack_today_filter_audit_messages(
        summary,
        [card.message_text for card in cards],
        message_limit=message_limit,
    )


def get_digest_rows_today(limit: int | None = 100) -> list[dict]:
    sales_feedback_placeholders = sql_placeholders(len(SALES_FEEDBACK_LOOKUP_STATUSES))
    sales_workflow_placeholders = sql_placeholders(len(SALES_WORKFLOW_STATUSES))
    query = f"""
        SELECT
            COUNT(*) OVER() AS today_total_found,
            t.id AS tender_id,
            t.*,
            rb.score,
            rb.recommendation,
            rb.confidence,
            rb.result,
            rb.result AS rule_based_result,
            dr.result AS document_risk_result,
            dr.created_at AS document_risk_created_at,
            ll.result AS llm_report_result,
            ll.recommendation AS llm_report_recommendation,
            ll.created_at AS llm_report_created_at,
            ll.analysis_type AS llm_report_analysis_type,
            triage.result AS lead_triage_result,
            triage.recommendation AS lead_triage_recommendation,
            triage.confidence AS lead_triage_confidence,
            triage.created_at AS lead_triage_created_at,
            triage.analysis_type AS lead_triage_analysis_type,
            triage.lead_decision AS lead_triage_decision,
            operational_lr.created_at AS operational_lead_report_created_at,
            operational_lr.analysis_type AS operational_lead_report_analysis_type,
            operational_lr.recommendation AS operational_lead_report_recommendation,
            sales_feedback.status AS latest_sales_status,
            sales_feedback.created_at AS latest_sales_status_created_at,
            sales_feedback.user_name AS latest_sales_user_name,
            sales_feedback.comment AS latest_sales_comment,
{EXISTING_CLIENT_CUSTOMER_SELECT_SQL}
            COALESCE(docs_stats.docs_count, 0) AS docs_count,
            COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text,
            primary_docs.latest_primary_technical_document_created_at,
            blocked_event.message AS preparation_blocked_event,
            requeue_event.message AS preparation_requeue_event,
            partial_download_event.message AS partial_document_download_event
        FROM tenders t
        JOIN LATERAL (
            SELECT a.score, a.recommendation, a.confidence, a.result
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
            ORDER BY a.created_at DESC
            LIMIT 1
        ) rb ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.result, a.created_at
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type IN ('document_risk_analysis', 'document_risk')
            ORDER BY a.created_at DESC
            LIMIT 1
        ) dr ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.result, a.recommendation, a.created_at, a.analysis_type
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type LIKE %s
            ORDER BY a.created_at DESC
            LIMIT 1
        ) ll ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                result,
                recommendation,
                confidence,
                created_at,
                analysis_type,
                lower(replace(coalesce(
                    result #>> '{{report,lead_decision}}',
                    result #>> '{{lead_decision}}',
                    result #>> '{{report,decision}}',
                    result #>> '{{decision}}',
                    recommendation,
                    ''
                ), '-', '_')) AS lead_decision
            FROM analysis_results
            WHERE tender_id = t.id
              AND analysis_type = %s
            ORDER BY created_at DESC
            LIMIT 1
        ) triage ON TRUE
        LEFT JOIN LATERAL (
            SELECT result, recommendation, confidence, created_at, analysis_type
            FROM analysis_results
            WHERE tender_id = t.id
              AND analysis_type = %s
            ORDER BY created_at DESC
            LIMIT 1
        ) operational_lr ON TRUE
        LEFT JOIN LATERAL (
            SELECT f.status, f.created_at, f.user_name, f.comment
            FROM feedback f
            WHERE f.tender_id = t.id
              AND f.status IN ({sales_feedback_placeholders})
            ORDER BY f.created_at DESC, f.id DESC
            LIMIT 1
        ) sales_feedback ON TRUE
        {existing_client_customer_feedback_sql(sales_workflow_placeholders)}
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) AS docs_count,
                COUNT(*) FILTER (
                    WHERE d.extracted_text IS NOT NULL
                      AND length(d.extracted_text) > 0
                ) AS docs_with_text
            FROM documents d
            WHERE d.tender_id = t.id
        ) docs_stats ON TRUE
        LEFT JOIN LATERAL (
            SELECT max(d.created_at) AS latest_primary_technical_document_created_at
            FROM documents d
            WHERE d.tender_id = t.id
              AND d.extracted_text IS NOT NULL
              AND length(d.extracted_text) > 0
              AND {PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION}
        ) primary_docs ON TRUE
        LEFT JOIN LATERAL (
            SELECT pe.message
            FROM processing_events pe
            WHERE pe.tender_id = t.id
              AND pe.event_type = %s
            ORDER BY pe.created_at DESC
            LIMIT 1
        ) blocked_event ON TRUE
        LEFT JOIN LATERAL (
            SELECT pe.message
            FROM processing_events pe
            WHERE pe.tender_id = t.id
              AND pe.event_type = %s
            ORDER BY pe.created_at DESC
            LIMIT 1
        ) requeue_event ON TRUE
        LEFT JOIN LATERAL (
            SELECT pe.message
            FROM processing_events pe
            WHERE pe.tender_id = t.id
              AND pe.event_type IN (%s, %s)
            ORDER BY pe.created_at DESC
            LIMIT 1
        ) partial_download_event ON TRUE
        WHERE t.created_at::date = CURRENT_DATE
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT %s
    """

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                (
                    "llm_presales_report%",
                    LEAD_TRIAGE_ANALYSIS_TYPE,
                    LEAD_LLM_REPORT_ANALYSIS_TYPE,
                    *SALES_FEEDBACK_LOOKUP_STATUSES,
                    *SALES_WORKFLOW_STATUSES,
                    SALES_EXISTING_CLIENT_STATUS,
                    PREPARATION_BLOCKED_EVENT,
                    PREPARATION_REQUEUE_EVENT,
                    PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
                    DOCUMENT_DOWNLOAD_RATE_LIMITED_EVENT,
                    limit,
                ),
            )
            return cur.fetchall()


def select_digest_rows_today(
    limit: int = 100,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict], TodayDigestStats]:
    del include_deadline_risk
    pool_limit = max(limit * 5, 100)
    rows = get_digest_rows_today(limit=pool_limit)

    selected: list[dict[str, Any]] = []
    manual_document_rows: list[dict[str, Any]] = []
    hidden_no_go = 0
    hidden_preparation = 0
    hidden_by_lead_triage_reject = 0
    hidden_lead_hard_noise_count = 0
    hidden_by_feedback = 0

    for row in rows:
        tender = normalize_today_digest_row(row)

        if hidden_by_sales_feedback_for_lead_queue(tender):
            hidden_by_feedback += 1
            continue

        if hidden_by_latest_lead_triage_reject(tender):
            hidden_by_lead_triage_reject += 1
            continue

        if recent_llm_recommendation(tender) == "no_go":
            hidden_no_go += 1
            continue

        try:
            assessment = business_assessment(tender)
            recommendation = effective_recommendation(tender)
        except Exception:
            if hidden_by_lead_hard_noise(tender):
                hidden_lead_hard_noise_count += 1
            continue

        if hidden_by_lead_hard_noise(tender, assessment):
            hidden_lead_hard_noise_count += 1
            continue

        if recommendation == "no_go" or not is_working_digest_candidate(tender, assessment):
            hidden_no_go += 1
            continue

        preparation_reason = today_preparation_skip_reason(tender)
        manual_reason = manual_document_reason_for_tender(tender)
        if is_operational_lead_report_row(tender):
            selected.append(tender)
            continue
        if manual_reason:
            manual_document_rows.append(mark_manual_document_reason(tender, manual_reason))
            continue

        if preparation_reason:
            hidden_preparation += 1
            continue

        selected.append(tender)

    try:
        deduped_rows = dedupe_by_tender_or_external_id([*selected, *manual_document_rows])
        selected = [
            row
            for row in deduped_rows
            if not (row.get(MANUAL_DOCUMENT_REASON_KEY) or row.get(TODAY_MANUAL_DOCUMENT_REASON_KEY))
        ]
        manual_document_rows = [
            row
            for row in deduped_rows
            if row.get(MANUAL_DOCUMENT_REASON_KEY) or row.get(TODAY_MANUAL_DOCUMENT_REASON_KEY)
        ]
        selected = sorted(selected, key=digest_sort_key)
        manual_document_rows = sorted(manual_document_rows, key=digest_sort_key)
    except Exception:
        pass

    selected = selected[:limit]
    manual_document_rows = manual_document_rows[:limit]
    deadline_warning_count = sum(
        1
        for tender in [*selected, *manual_document_rows]
        if lead_deadline_needs_warning(tender, deadline_min_days, now=now)
    )
    total_found = len(rows)
    if rows:
        try:
            total_found = int(rows[0].get("today_total_found") or total_found)
        except (TypeError, ValueError):
            total_found = len(rows)

    return selected, manual_document_rows, TodayDigestStats(
        total_found=total_found,
        shown=len(selected),
        manual_documents_needed=len(manual_document_rows),
        hidden_no_go=hidden_no_go,
        deadline_warning=deadline_warning_count,
        hidden_preparation=hidden_preparation,
        hidden_by_lead_triage_reject=hidden_by_lead_triage_reject,
        hidden_by_lead_hard_noise=hidden_lead_hard_noise_count,
        hidden_by_feedback=hidden_by_feedback,
    )


def today_digest_summary_text(
    stats: TodayDigestStats,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
) -> str:
    lines = ["AI Tender Radar - найдено сегодня"]
    if stats.shown == 0 and stats.manual_documents_needed == 0:
        lines.append("Рабочих кандидатов за сегодня нет.")

    deadline_warning_count = getattr(
        stats,
        "deadline_warning",
        getattr(stats, "hidden_deadline", 0),
    )
    lines.extend(
        [
            f"Найдено сегодня всего: {stats.total_found}",
            f"Показано рабочих кандидатов: {stats.shown}",
            f"Нужны документы для LLM-разбора: {stats.manual_documents_needed}",
            f"Скрыто no_go: {stats.hidden_no_go}",
            f"Скрыто latest lead triage reject ({HIDDEN_BY_LEAD_TRIAGE_REJECT}): {stats.hidden_by_lead_triage_reject}",
            f"{HIDDEN_BY_LEAD_HARD_NOISE}: {getattr(stats, 'hidden_by_lead_hard_noise', 0)}",
            f"{HIDDEN_BY_FEEDBACK}: {getattr(stats, 'hidden_by_feedback', 0)}",
            f"С близким/истекшим сроком: {deadline_warning_count}",
            f"Скрыто по документам/доступу: {stats.hidden_preparation}",
        ]
    )
    return "\n".join(lines)


def today_card_from_row(tender: dict) -> TenderDigestCard:
    return tender_card_from_row(tender, compact_working_card(tender))


def manual_document_needed_card_from_row(tender: dict) -> TenderDigestCard:
    full = raw_full(tender)
    reason = tender.get(MANUAL_DOCUMENT_REASON_KEY) or tender.get(TODAY_MANUAL_DOCUMENT_REASON_KEY)

    title = tender.get("title") or "Без названия"
    customer = get_customer_name(tender, full)

    price = (
        tender.get("initial_price")
        or tender.get("max_price")
        or tender.get("price")
        or full.get("maxPrice")
        or full.get("nmck")
    )

    deadline = (
        tender.get("application_deadline")
        or tender.get("end_date")
        or tender.get("bidding_deadline")
        or tender.get("deadline_at")
        or full.get("endDate")
        or full.get("filingEndDate")
    )

    url = procedure_url(tender, full)

    sections = [
        "📄 Нужны документы для LLM-разбора",
        short_text(title, 240),
        "",
        f"НМЦК: {money(price)}",
        f"Срок подачи: {date_text(deadline)}",
        f"Заказчик: {customer}",
        f"Ссылка: {url or 'не найдена'}",
        "",
        f"Причина: {manual_document_user_reason_text(reason)}",
        f"Почему важно: {manual_document_importance_text(tender)}",
        "",
        compact_outreach_block(tender),
    ]
    if deadline_value_needs_lead_warning(deadline):
        sections.insert(5, DEADLINE_LEAD_WARNING_TEXT)

    message_text = "\n".join(str(x) for x in sections if x is not None)
    return tender_card_from_row(tender, message_text)


def build_digest_cards_today_with_stats(
    limit: int = 100,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> tuple[list[TenderDigestCard], list[TenderDigestCard], TodayDigestStats]:
    selected, manual_document_rows, stats = select_digest_rows_today(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    cards = [today_card_from_row(tender) for tender in selected]
    manual_cards = [
        manual_document_needed_card_from_row(tender)
        for tender in manual_document_rows
    ]
    return cards, manual_cards, stats


def build_digest_cards_today(
    limit: int = 100,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[TenderDigestCard]:
    cards, manual_cards, _ = build_digest_cards_today_with_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )

    return cards + manual_cards


def build_digest_messages_today(
    limit: int = 100,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    include_deadline_risk: bool = False,
    now: datetime | None = None,
) -> list[str]:
    cards, manual_cards, stats = build_digest_cards_today_with_stats(
        limit=limit,
        deadline_min_days=deadline_min_days,
        include_deadline_risk=include_deadline_risk,
        now=now,
    )
    messages = [today_digest_summary_text(stats, deadline_min_days=deadline_min_days)]
    messages.extend(card.message_text for card in cards)
    if manual_cards:
        messages.append(f"Нужны документы для LLM-разбора: {len(manual_cards)}")
        messages.extend(card.message_text for card in manual_cards)
    return messages


if __name__ == "__main__":
    main()
