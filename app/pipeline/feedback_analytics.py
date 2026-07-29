from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.procedure_url import procedure_url_from_sources


DEFAULT_DAYS = 7
DEFAULT_LIMIT = 50
PREPARATION_REQUEUE_EVENT = "targeted_document_download_requeue"
PREPARATION_BLOCKED_EVENT = "targeted_document_preparation_blocked"

SALES_STATUS_LABELS = {
    "sales_in_work": "В работе",
    "sales_not_relevant": "Мимо",
    "sales_returned": "Возвращено в список",
    "sales_existing_client": "Существующий клиент",
    "interesting": "Интересно",
    "in_work": "В работу",
    "skip": "Мимо",
    "not_interesting": "Мимо",
    "tailored": "Заточено",
    "not_profile": "Не наш профиль",
    "not_our_profile": "Не наш профиль",
    "bad_region": "Не наш регион",
    "bad_price": "Маленький чек",
    "service_only": "Услуги/ПО без инфраструктуры",
    "low_value_one_off": "Разовая мелкая поставка",
    "bad_customer_fit": "Заказчик не подходит",
    "bad_timing": "Поздно / не сейчас",
    "duplicate_or_processed": "Дубль / уже обработали",
    "other_reject": "Другое",
}

SALES_STATUS_ORDER = (
    "sales_in_work",
    "sales_not_relevant",
    "sales_returned",
    "sales_existing_client",
    "interesting",
    "in_work",
    "skip",
    "not_interesting",
    "tailored",
    "not_profile",
    "not_our_profile",
    "bad_region",
    "bad_price",
    "service_only",
    "low_value_one_off",
    "bad_customer_fit",
    "bad_timing",
    "duplicate_or_processed",
    "other_reject",
)

SALES_WORKFLOW_STATUS_LABELS = {
    "sales_in_work": "В работе",
    "sales_not_relevant": "Мимо",
    "sales_returned": "Возвращено в список",
    "sales_existing_client": "Существующий клиент",
}

SALES_WORKFLOW_STATUS_ORDER = (
    "sales_in_work",
    "sales_not_relevant",
    "sales_returned",
    "sales_existing_client",
)

FILTER_STATUS_LABELS = {
    "filter_hidden_correct": "Фильтр верно скрыл",
    "filter_hidden_wrong": "Фильтр ошибочно скрыл",
    "filter_docs_correct": 'Верно отправлено в "Нужны документы"',
    "filter_docs_wrong": 'Ошибочно отправлено в "Нужны документы"',
}

FILTER_STATUS_ORDER = (
    "filter_hidden_correct",
    "filter_hidden_wrong",
    "filter_docs_correct",
    "filter_docs_wrong",
)

POSITIVE_SALES_STATUSES = {"interesting", "in_work", "sales_in_work"}
NEGATIVE_SALES_STATUSES = {
    "sales_not_relevant",
    "sales_existing_client",
    "skip",
    "not_interesting",
    "not_profile",
    "not_our_profile",
    "bad_region",
    "bad_price",
    "service_only",
    "low_value_one_off",
    "bad_customer_fit",
    "bad_timing",
    "duplicate_or_processed",
    "other_reject",
}
FILTER_WRONG_STATUSES = {"filter_hidden_wrong", "filter_docs_wrong"}
FILTER_CORRECT_STATUSES = {"filter_hidden_correct", "filter_docs_correct"}


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be a non-negative integer")
    return parsed


def analysis_period(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end - timedelta(days=days), end


def fetch_feedback_rows(days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    from app.config import settings

    period_start, _ = analysis_period(days, now=now)

    query = """
        SELECT
            f.id AS feedback_id,
            f.created_at AS feedback_created_at,
            f.user_name,
            f.status,
            f.comment,
            t.id AS tender_id,
            t.external_id,
            t.title,
            t.customer_name,
            t.initial_price,
            t.currency,
            t.deadline_at,
            t.url,
            t.raw,
            t.status AS tender_status,
            rb.recommendation AS rule_recommendation,
            rb.result AS rule_based_result,
            dr.result AS document_risk_result,
            lr.recommendation AS lead_report_recommendation,
            lr.confidence AS lead_report_confidence,
            lr.result AS lead_report_result,
            lt.recommendation AS lead_triage_recommendation,
            lt.confidence AS lead_triage_confidence,
            lt.result AS lead_triage_result,
            blocked_event.message AS preparation_blocked_event,
            requeue_event.message AS preparation_requeue_event
        FROM feedback f
        JOIN tenders t ON t.id = f.tender_id
        LEFT JOIN LATERAL (
            SELECT a.recommendation, a.result
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type IN ('rule_based_scoring', 'rule_based_score')
            ORDER BY a.created_at DESC
            LIMIT 1
        ) rb ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.result
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type IN ('document_risk_analysis', 'document_risk')
            ORDER BY a.created_at DESC
            LIMIT 1
        ) dr ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.recommendation, a.confidence, a.result
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type = 'llm_customer_lead_report'
            ORDER BY a.created_at DESC
            LIMIT 1
        ) lr ON TRUE
        LEFT JOIN LATERAL (
            SELECT a.recommendation, a.confidence, a.result
            FROM analysis_results a
            WHERE a.tender_id = t.id
              AND a.analysis_type = 'llm_customer_lead_triage'
            ORDER BY a.created_at DESC
            LIMIT 1
        ) lt ON TRUE
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
        WHERE f.created_at >= %s
        ORDER BY f.created_at DESC, f.id DESC;
    """

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, (PREPARATION_BLOCKED_EVENT, PREPARATION_REQUEUE_EVENT, period_start))
            return [dict(row) for row in cur.fetchall()]


def raw_full(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") or {}
    if not isinstance(raw, Mapping):
        return {}

    full = raw.get("full") or {}
    return dict(full) if isinstance(full, Mapping) else {}


def procedure_url(row: Mapping[str, Any], full: Mapping[str, Any] | None = None) -> str | None:
    full = full if full is not None else raw_full(row)
    return procedure_url_from_sources(row, full)


def customer_name(row: Mapping[str, Any], full: Mapping[str, Any] | None = None) -> str | None:
    value = str(row.get("customer_name") or "").strip()
    if value:
        return value

    full = full if full is not None else raw_full(row)
    placer = full.get("placerOrganization") or {}
    if isinstance(placer, Mapping):
        for key in ("fullName", "name"):
            value = str(placer.get(key) or "").strip()
            if value:
                return value

    customers = full.get("customers") or []
    if customers:
        first = customers[0] or {}
        if isinstance(first, Mapping):
            for key in ("fullName", "name"):
                value = str(first.get(key) or "").strip()
                if value:
                    return value

    return None


def tender_price(row: Mapping[str, Any], full: Mapping[str, Any] | None = None) -> Any:
    full = full if full is not None else raw_full(row)
    return (
        row.get("initial_price")
        or row.get("max_price")
        or row.get("price")
        or full.get("maxPrice")
        or full.get("nmck")
    )


def tender_deadline(row: Mapping[str, Any], full: Mapping[str, Any] | None = None) -> Any:
    full = full if full is not None else raw_full(row)
    return (
        row.get("application_deadline")
        or row.get("end_date")
        or row.get("bidding_deadline")
        or row.get("deadline_at")
        or full.get("endDate")
        or full.get("filingEndDate")
    )


def format_money(value: Any) -> str:
    if value is None:
        return "не указана"

    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)

    return f"{amount:,.0f} ₽".replace(",", " ")


def format_datetime(value: Any) -> str:
    if not value:
        return "не указан"

    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")

    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")

    return str(value)


def short_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return "не указано"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def status_code(row: Mapping[str, Any]) -> str:
    return str(row.get("status") or "").strip()


def status_label(status: str) -> str:
    if status in FILTER_STATUS_LABELS:
        return FILTER_STATUS_LABELS[status]
    if status in SALES_STATUS_LABELS:
        return SALES_STATUS_LABELS[status]
    return f"unknown/raw: {status or 'empty'}"


def is_filter_status(status: str) -> bool:
    return status in FILTER_STATUS_LABELS


def is_sales_workflow_status(status: str) -> bool:
    return status in SALES_WORKFLOW_STATUS_LABELS


def sorted_status_items(counts: Counter[str], order: Iterable[str]) -> list[tuple[str, int]]:
    order_index = {status: index for index, status in enumerate(order)}
    return sorted(
        counts.items(),
        key=lambda item: (order_index.get(item[0], len(order_index)), item[0]),
    )


def unique_tender_count(rows: Iterable[Mapping[str, Any]]) -> int:
    keys: set[str] = set()
    for row in rows:
        key = row.get("tender_id") or row.get("external_id")
        if key:
            keys.add(str(key))
    return len(keys)


def safe_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "n/a"
    return f"{numerator / denominator * 100:.1f}%"


def positive_sales_status(status: str) -> bool:
    return status in POSITIVE_SALES_STATUSES


def negative_sales_status(status: str) -> bool:
    return status in NEGATIVE_SALES_STATUSES


def latest_sales_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        status = status_code(row)
        if is_filter_status(status):
            continue
        key = str(row.get("tender_id") or row.get("external_id") or "")
        if not key:
            continue
        grouped[key].append(row)

    latest: list[Mapping[str, Any]] = []
    for items in grouped.values():
        latest.append(
            sorted(
                items,
                key=lambda row: (
                    str(row.get("feedback_created_at") or ""),
                    str(row.get("feedback_id") or ""),
                ),
                reverse=True,
            )[0]
        )

    return sorted(
        latest,
        key=lambda row: (
            str(row.get("feedback_created_at") or ""),
            str(row.get("feedback_id") or ""),
        ),
        reverse=True,
    )


def repeated_feedback_stats(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        status = status_code(row)
        if is_filter_status(status):
            continue
        key = str(row.get("tender_id") or row.get("external_id") or "")
        if key:
            grouped[key].append(row)

    repeated = 0
    flipped = 0
    negative_to_positive = 0
    positive_to_negative = 0

    for items in grouped.values():
        if len(items) <= 1:
            continue
        repeated += 1
        statuses = {status_code(row) for row in items}
        if len(statuses) <= 1:
            continue
        flipped += 1
        ordered = sorted(
            items,
            key=lambda row: (
                str(row.get("feedback_created_at") or ""),
                str(row.get("feedback_id") or ""),
            ),
        )
        first_status = status_code(ordered[0])
        latest_status = status_code(ordered[-1])
        if negative_sales_status(first_status) and positive_sales_status(latest_status):
            negative_to_positive += 1
        if positive_sales_status(first_status) and negative_sales_status(latest_status):
            positive_to_negative += 1

    return {
        "repeated": repeated,
        "flipped": flipped,
        "negative_to_positive": negative_to_positive,
        "positive_to_negative": positive_to_negative,
    }


def direct_category(row: Mapping[str, Any]) -> str | None:
    for key in (
        "business_category",
        "target_category",
        "target_category_label",
        "category",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return None


def target_category(row: Mapping[str, Any]) -> str:
    direct = direct_category(row)
    if direct:
        return direct

    try:
        from app.business_profile import load_business_profile, match_target_category

        category_name, _ = match_target_category(dict(row), load_business_profile())
    except Exception:
        category_name = None

    return str(category_name or "no_target_category")


def json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def report_payload(value: Any) -> dict[str, Any]:
    payload = json_mapping(value)
    report = payload.get("report")
    return dict(report) if isinstance(report, Mapping) else payload


def compact_scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("priority", "level", "decision", "value"):
            nested = str(value.get(key) or "").strip()
            if nested:
                return nested
        return "dict"
    return str(value or "").strip()


def row_lead_report_priority(row: Mapping[str, Any]) -> str:
    report = report_payload(row.get("lead_report_result"))
    return (
        compact_scalar(report.get("lead_priority"))
        or compact_scalar(row.get("lead_report_recommendation"))
        or "none"
    )


def row_lead_report_confidence(row: Mapping[str, Any]) -> str:
    report = report_payload(row.get("lead_report_result"))
    return (
        compact_scalar(report.get("confidence"))
        or compact_scalar(row.get("lead_report_confidence"))
        or "none"
    )


def row_lead_triage_decision(row: Mapping[str, Any]) -> str:
    report = report_payload(row.get("lead_triage_result"))
    return (
        compact_scalar(report.get("lead_decision"))
        or compact_scalar(row.get("lead_triage_recommendation"))
        or "none"
    )


def row_lead_triage_priority(row: Mapping[str, Any]) -> str:
    report = report_payload(row.get("lead_triage_result"))
    return (
        compact_scalar(report.get("lead_priority"))
        or compact_scalar(row.get("lead_triage_recommendation"))
        or "none"
    )


def price_bucket(row: Mapping[str, Any]) -> str:
    amount = tender_price(row)
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount or 0))
    except (InvalidOperation, ValueError):
        value = Decimal(0)

    if value <= 0:
        return "unknown"
    if value < Decimal("3000000"):
        return "<3M"
    if value < Decimal("10000000"):
        return "3-10M"
    if value < Decimal("50000000"):
        return "10-50M"
    return "50M+"


def render_quality_table(
    rows: list[Mapping[str, Any]],
    key_func,
    *,
    title_label: str,
    max_rows: int | None = None,
) -> list[str]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(key_func(row) or "none")].append(row)

    if not grouped:
        return [f"{title_label}: данных нет."]

    lines = [
        f"| {title_label} | Latest feedback | Positive | Negative | Positive rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    items = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    if max_rows is not None:
        items = items[:max_rows]

    for value, items_for_value in items:
        positive = sum(1 for row in items_for_value if positive_sales_status(status_code(row)))
        negative = sum(1 for row in items_for_value if negative_sales_status(status_code(row)))
        total = positive + negative
        lines.append(
            f"| `{value}` | {len(items_for_value)} | {positive} | {negative} | "
            f"{safe_percent(positive, total)} |"
        )

    return lines


def parse_event_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)

    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}

    return dict(payload) if isinstance(payload, Mapping) else {}


def non_negative_value(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def preparation_skip_reason(row: Mapping[str, Any]) -> str | None:
    direct_values = {
        str(row.get(key) or "").strip().lower()
        for key in (
            "preparation_skip_reason",
            "preparation_status",
            "preparation_reason",
            "technical_preparation_status",
            "document_state",
            "warning_reason",
        )
    }

    result = row.get("rule_based_result")
    if isinstance(result, Mapping):
        for key in (
            "preparation_status",
            "preparation_state",
            "preparation_reason",
            "document_state",
            "warning_reason",
            "skip_reason",
        ):
            direct_values.add(str(result.get(key) or "").strip().lower())

    direct_text = " ".join(value for value in direct_values if value)
    if (
        "preparation_blocked=marketplace_auth" in direct_values
        or "blocked_by_marketplace_auth" in direct_values
        or "marketplace_auth" in direct_values
        or "blocked_by_marketplace_auth" in direct_text
    ):
        return "preparation_blocked=marketplace_auth"

    if (
        "preparation_no_valid_documents" in direct_values
        or "no_valid_documents" in direct_values
        or "preparation_no_valid_documents" in direct_text
        or "no_valid_documents" in direct_text
    ):
        return "preparation_no_valid_documents"

    if (
        "preparation_exhausted=documents_missing" in direct_values
        or "documents_missing" in direct_values
        or "documents_missing_after_prepare" in direct_values
        or "preparation_exhausted=documents_missing" in direct_text
        or "documents_missing_after_prepare" in direct_text
    ):
        return "preparation_exhausted=documents_missing"

    if (
        "preparation_exhausted=not_ready_for_llm" in direct_values
        or "not_ready_for_llm" in direct_values
        or "preparation_exhausted=not_ready_for_llm" in direct_text
        or "not_ready_for_llm" in direct_text
    ):
        return "preparation_exhausted=not_ready_for_llm"

    blocked_payload = parse_event_payload(row.get("preparation_blocked_event"))
    blocked_status = str(blocked_payload.get("status") or "").lower()
    blocked_reason = str(blocked_payload.get("reason") or "").lower()
    if blocked_status == "blocked_by_marketplace_auth" or blocked_reason in {
        "marketplace_auth",
        "external_marketplace_auth_required",
    }:
        return "preparation_blocked=marketplace_auth"
    if blocked_status == "no_valid_documents" or blocked_reason == "no_valid_documents":
        return "preparation_no_valid_documents"

    requeue_payload = parse_event_payload(row.get("preparation_requeue_event"))
    if not requeue_payload.get("exhausted"):
        return None

    requeue_reason = str(requeue_payload.get("reason") or "").lower()
    if requeue_reason in {"documents_missing", "preparation_exhausted=documents_missing"}:
        return "preparation_exhausted=documents_missing"
    if requeue_reason in {"not_ready_for_llm", "preparation_exhausted=not_ready_for_llm"}:
        return "preparation_exhausted=not_ready_for_llm"

    documents_downloaded = non_negative_value(requeue_payload.get("documents_downloaded"))
    documents_with_text = non_negative_value(requeue_payload.get("documents_with_text"))
    if documents_downloaded <= 0:
        return "preparation_exhausted=documents_missing"
    if documents_with_text <= 0:
        return "preparation_exhausted=not_ready_for_llm"

    return None


def compact_mapping_reason(prefix: str, value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None

    parts: list[str] = []
    for key in (
        "filter_reason",
        "skip_reason",
        "selection_skip_reason",
        "debug_skip_reason",
        "preparation_skip_reason",
        "preparation_reason",
        "action",
        "market_access",
        "recommendation",
        "decision",
        "document_state",
        "warning_reason",
    ):
        raw = value.get(key)
        if raw is not None and str(raw).strip():
            parts.append(f"{key}={short_text(raw, 80)}")

    if not parts:
        return None
    return f"{prefix}: " + ", ".join(parts[:4])


def filter_reason_text(row: Mapping[str, Any]) -> str:
    parts: list[str] = []

    preparation_reason = preparation_skip_reason(row)
    if preparation_reason:
        parts.append(preparation_reason)

    for key in (
        "filter_reason",
        "skip_reason",
        "selection_skip_reason",
        "debug_skip_reason",
        "preparation_skip_reason",
        "preparation_reason",
        "tender_status",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            parts.append(f"{key}={short_text(value, 80)}")

    rule_recommendation = str(row.get("rule_recommendation") or "").strip()
    if rule_recommendation:
        parts.append(f"rule_recommendation={rule_recommendation}")

    for prefix, value in (
        ("rule_based", row.get("rule_based_result")),
        ("document_risk", row.get("document_risk_result")),
        ("blocked_event", parse_event_payload(row.get("preparation_blocked_event"))),
        ("requeue_event", parse_event_payload(row.get("preparation_requeue_event"))),
    ):
        mapped = compact_mapping_reason(prefix, value)
        if mapped:
            parts.append(mapped)

    seen: set[str] = set()
    unique_parts = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            unique_parts.append(part)

    return "; ".join(unique_parts) if unique_parts else "не найдена"


def render_status_table(counts: Counter[str], order: Iterable[str]) -> list[str]:
    if not counts:
        return ["За период событий не найдено."]

    lines = [
        "| Status | Label | Count |",
        "| --- | --- | ---: |",
    ]
    for status, count in sorted_status_items(counts, order):
        label = status_label(status)
        lines.append(f"| `{status or 'empty'}` | {label} | {count} |")

    return lines


def render_detail_row(
    row: Mapping[str, Any],
    *,
    include_reason: bool = False,
    include_raw: bool = False,
) -> str:
    full = raw_full(row)
    user_name = str(row.get("user_name") or "не указан")
    status = status_code(row)
    external_id = row.get("external_id") or "не указан"
    title = short_text(row.get("title") or "Без названия", 220)
    url = procedure_url(row, full)
    customer = customer_name(row, full)

    lines = [
        (
            f"- {format_datetime(row.get('feedback_created_at'))} | "
            f"user_name: {user_name} | status: {status_label(status)} | "
            f"external_id: {external_id}"
        ),
        f"  Название: {title}",
        f"  НМЦК: {format_money(tender_price(row, full))}",
        f"  Deadline: {format_datetime(tender_deadline(row, full))}",
    ]

    if customer:
        lines.append(f"  Заказчик: {customer}")

    if include_reason:
        lines.append(f"  Причина/статус: {filter_reason_text(row)}")

    lines.append(f"  Ссылка: {url or 'не найдена'}")

    if include_raw:
        lines.append(
            "  Raw: "
            f"feedback_id={row.get('feedback_id') or 'не указан'}, "
            f"tender_id={row.get('tender_id') or 'не указан'}, "
            f"status_code={status or 'empty'}"
        )
        comment = str(row.get("comment") or "").strip()
        if comment:
            lines.append(f"  Comment: {short_text(comment, 220)}")

    return "\n".join(lines)


def render_limited_details(
    rows: list[Mapping[str, Any]],
    *,
    limit: int,
    empty_text: str,
    include_reason: bool = False,
    include_raw: bool = False,
) -> list[str]:
    if not rows:
        return [empty_text]

    selected = rows[:limit] if limit else []
    lines = [
        render_detail_row(row, include_reason=include_reason, include_raw=include_raw)
        for row in selected
    ]
    if limit and len(rows) > limit:
        lines.append(f"Показаны первые {limit} из {len(rows)}.")
    elif limit == 0:
        lines.append(f"Список скрыт параметром --limit 0. Всего: {len(rows)}.")
    return lines


def render_unlimited_details(
    rows: list[Mapping[str, Any]],
    *,
    empty_text: str,
    include_reason: bool = False,
    include_raw: bool = False,
) -> list[str]:
    if not rows:
        return [empty_text]

    return [
        render_detail_row(row, include_reason=include_reason, include_raw=include_raw)
        for row in rows
    ]


def recommendation_threshold(count: int, total: int) -> bool:
    return count >= 3 or (count >= 2 and total > 0 and count / total >= 0.4)


def build_recommendations(sales_counts: Counter[str], filter_counts: Counter[str]) -> list[str]:
    lines: list[str] = []

    if filter_counts.get("filter_hidden_wrong", 0) > 0:
        lines.append("- Проверить business rules по ошибочно скрытым карточкам.")
    if filter_counts.get("filter_docs_wrong", 0) > 0:
        lines.append("- Проверить document readiness / manual-doc logic.")

    profile_miss_count = (
        sales_counts.get("skip", 0)
        + sales_counts.get("not_interesting", 0)
        + sales_counts.get("not_profile", 0)
        + sales_counts.get("not_our_profile", 0)
    )
    sales_total = sum(sales_counts.values())
    if recommendation_threshold(profile_miss_count, sales_total):
        lines.append("- Посмотреть search profile и stop patterns.")

    if recommendation_threshold(sales_counts.get("bad_price", 0), sales_total):
        lines.append("- Проверить price threshold и rerank: много отказов по маленькому чеку.")

    if recommendation_threshold(sales_counts.get("bad_region", 0), sales_total):
        lines.append("- Проверить региональный профиль: много отказов по региону.")

    low_value_count = (
        sales_counts.get("service_only", 0)
        + sales_counts.get("low_value_one_off", 0)
        + sales_counts.get("bad_customer_fit", 0)
    )
    if recommendation_threshold(low_value_count, sales_total):
        lines.append("- Проверить hard-noise, deal-type rules и lead prompt: много отказов по низкой коммерческой ценности.")

    positive_count = sum(sales_counts.get(status, 0) for status in POSITIVE_SALES_STATUSES)
    if positive_count > 0:
        lines.append("- Закрепить похожие признаки в профиле после ручной проверки.")

    if not lines:
        lines.append("- Критичных сигналов за период нет. Продолжать накапливать feedback.")

    lines.append("- Автоматические правки в business rules, scoring, search profile или LLM не выполнялись.")
    return lines


def build_feedback_report(
    rows: list[Mapping[str, Any]],
    *,
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    include_raw: bool = False,
    now: datetime | None = None,
) -> str:
    period_start, period_end = analysis_period(days, now=now)
    sales_rows = [row for row in rows if not is_filter_status(status_code(row))]
    filter_rows = [row for row in rows if is_filter_status(status_code(row))]
    sales_workflow_rows = [
        row for row in rows if is_sales_workflow_status(status_code(row))
    ]
    latest_sales_feedback_rows = latest_sales_rows(rows)

    sales_counts = Counter(status_code(row) for row in sales_rows)
    filter_counts = Counter(status_code(row) for row in filter_rows)
    sales_workflow_counts = Counter(status_code(row) for row in sales_workflow_rows)
    latest_sales_counts = Counter(status_code(row) for row in latest_sales_feedback_rows)
    all_counts = Counter(status_code(row) for row in rows)

    positive_sales_count = sum(sales_counts.get(status, 0) for status in POSITIVE_SALES_STATUSES)
    negative_sales_count = sum(sales_counts.get(status, 0) for status in NEGATIVE_SALES_STATUSES)
    latest_positive_sales_count = sum(
        latest_sales_counts.get(status, 0) for status in POSITIVE_SALES_STATUSES
    )
    latest_negative_sales_count = sum(
        latest_sales_counts.get(status, 0) for status in NEGATIVE_SALES_STATUSES
    )
    filter_wrong_count = sum(filter_counts.get(status, 0) for status in FILTER_WRONG_STATUSES)
    filter_correct_count = sum(filter_counts.get(status, 0) for status in FILTER_CORRECT_STATUSES)
    repeat_stats = repeated_feedback_stats(rows)

    hot_sales_rows = [
        row for row in sales_rows if status_code(row) in POSITIVE_SALES_STATUSES
    ]
    filter_wrong_rows = [
        row for row in filter_rows if status_code(row) in FILTER_WRONG_STATUSES
    ]
    filter_correct_rows = [
        row for row in filter_rows if status_code(row) in FILTER_CORRECT_STATUSES
    ]
    latest_sales_workflow_rows = sales_workflow_rows[:20]

    lines = [
        f"# Feedback analytics за последние {days} дней",
        "",
        "## Общая сводка",
        f"- Всего feedback-событий: {len(rows)}",
        f"- Sales feedback count: {len(sales_rows)}",
        f"- Filter feedback count: {len(filter_rows)}",
        f"- Уникальных тендеров с feedback: {unique_tender_count(rows)}",
        f"- Период анализа: {format_datetime(period_start)} - {format_datetime(period_end)}",
    ]

    if not rows:
        lines.extend(["", "За период feedback-событий не найдено."])

    lines.extend(
        [
            "",
            "## Sales feedback",
            *render_status_table(sales_counts, SALES_STATUS_ORDER),
            "",
            "## Latest sales status per tender",
            f"- Уникальных тендеров с sales feedback: {len(latest_sales_feedback_rows)}",
            f"- Latest positive sales feedback: {latest_positive_sales_count}",
            f"- Latest negative sales feedback: {latest_negative_sales_count}",
            (
                "- Latest positive rate: "
                f"{safe_percent(latest_positive_sales_count, latest_positive_sales_count + latest_negative_sales_count)}"
            ),
            *render_status_table(latest_sales_counts, SALES_STATUS_ORDER),
            "",
            "## Повторный feedback",
            f"- Тендеров с повторным sales feedback: {repeat_stats['repeated']}",
            f"- Тендеров со сменой sales status: {repeat_stats['flipped']}",
            f"- Смена negative -> positive: {repeat_stats['negative_to_positive']}",
            f"- Смена positive -> negative: {repeat_stats['positive_to_negative']}",
            "",
            "## Quality by category",
            *render_quality_table(
                latest_sales_feedback_rows,
                target_category,
                title_label="Category",
            ),
            "",
            "## Quality by price bucket",
            *render_quality_table(
                latest_sales_feedback_rows,
                price_bucket,
                title_label="Price bucket",
            ),
            "",
            "## LLM signal quality",
            "### Lead report priority",
            *render_quality_table(
                latest_sales_feedback_rows,
                row_lead_report_priority,
                title_label="Lead priority",
                max_rows=12,
            ),
            "",
            "### Lead report confidence",
            *render_quality_table(
                latest_sales_feedback_rows,
                row_lead_report_confidence,
                title_label="Lead confidence",
                max_rows=12,
            ),
            "",
            "### Lead triage decision",
            *render_quality_table(
                latest_sales_feedback_rows,
                row_lead_triage_decision,
                title_label="Lead decision",
                max_rows=12,
            ),
            "",
            "### Lead triage priority",
            *render_quality_table(
                latest_sales_feedback_rows,
                row_lead_triage_priority,
                title_label="Triage priority",
                max_rows=12,
            ),
            "",
            "## Sales workflow",
            f"- Взято в работу: {sales_workflow_counts.get('sales_in_work', 0)}",
            f"- Отмечено мимо: {sales_workflow_counts.get('sales_not_relevant', 0)}",
            f"- Существующий клиент: {sales_workflow_counts.get('sales_existing_client', 0)}",
            f"- Возвращено в список: {sales_workflow_counts.get('sales_returned', 0)}",
            *render_status_table(sales_workflow_counts, SALES_WORKFLOW_STATUS_ORDER),
            "",
            "### Последние 20 sales workflow events",
            *render_limited_details(
                latest_sales_workflow_rows,
                limit=20,
                empty_text="Sales workflow events за период не найдены.",
                include_raw=include_raw,
            ),
            "",
            "## Filter feedback",
            *render_status_table(filter_counts, FILTER_STATUS_ORDER),
            "",
            "## Важные карточки",
            "",
            '### "В работу" и "Интересно"',
            *render_limited_details(
                hot_sales_rows,
                limit=limit,
                empty_text='Feedback "В работу" и "Интересно" за период не найден.',
                include_raw=include_raw,
            ),
            "",
            '### "Фильтр ошибся"',
            *render_unlimited_details(
                filter_wrong_rows,
                empty_text='Feedback "Фильтр ошибся" за период не найден.',
                include_reason=True,
                include_raw=include_raw,
            ),
            "",
            '### "Фильтр сработал верно"',
            *render_limited_details(
                filter_correct_rows,
                limit=limit,
                empty_text='Feedback "Фильтр сработал верно" за период не найден.',
                include_reason=True,
                include_raw=include_raw,
            ),
            "",
            "## Простая аналитика по качеству",
            f"- Положительный sales feedback: {positive_sales_count}",
            f"- Отрицательный sales feedback: {negative_sales_count}",
            f"- Filter wrong count: {filter_wrong_count}",
            f"- Filter correct count: {filter_correct_count}",
            "",
            "## Что проверить",
            *build_recommendations(sales_counts, filter_counts),
        ]
    )

    unknown_statuses = sorted(
        status for status in all_counts if status not in SALES_STATUS_LABELS and status not in FILTER_STATUS_LABELS
    )
    if unknown_statuses:
        lines.extend(
            [
                "",
                "## Unknown/raw statuses",
                *(
                    f"- `{status or 'empty'}`: {all_counts[status]}"
                    for status in unknown_statuses
                ),
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def write_report(markdown: str, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a feedback analytics Markdown report.")
    parser.add_argument("--days", type=positive_int, default=DEFAULT_DAYS)
    parser.add_argument("--limit", type=non_negative_int, default=DEFAULT_LIMIT)
    parser.add_argument("--output-path")
    parser.add_argument("--include-raw", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    rows = fetch_feedback_rows(args.days, now=now)
    markdown = build_feedback_report(
        rows,
        days=args.days,
        limit=args.limit,
        include_raw=args.include_raw,
        now=now,
    )

    print(markdown, end="")
    if args.output_path:
        write_report(markdown, args.output_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
