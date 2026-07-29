from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

MARKETPLACE_AUTH_HINT = "external_marketplace_auth_required"
DOCUMENTS_MISSING = "documents_missing"
DOCUMENTS_DOWNLOADED_WITHOUT_TEXT = "documents_downloaded_without_text"
DOCUMENTS_READY_WITH_TEXT = "documents_ready_with_text"
PRIMARY_TECHNICAL_DOCUMENT_MISSING = "primary_technical_document_missing"
PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED = "primary_technical_document_not_processed"
PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT = (
    "preparation_missing_primary_technical_document"
)
PRIMARY_TECHNICAL_DOCUMENT_READINESS_REASONS = {
    PRIMARY_TECHNICAL_DOCUMENT_MISSING,
    PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED,
    PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT,
}
TARGETED_DOWNLOAD_REQUEUE_EVENT = "targeted_document_download_requeue"
TARGETED_PREPARATION_BLOCKED_EVENT = "targeted_document_preparation_blocked"
PARTIAL_DOCUMENT_DOWNLOAD_EVENT = "targeted_document_download_partial"
TARGETED_REQUEUE_BACKOFF_SECONDS = 300
TARGETED_REQUEUE_MAX_ATTEMPTS = 3
TARGETED_PREPARATION_BLOCKED_COOLDOWN_SECONDS = 24 * 60 * 60
PRIMARY_TENDER_CONTEXT_KEYS = (
    "category",
    "target_category",
    "business_category",
    "detected_category",
    "_llm_category",
    "_llm_category_label",
    "market_access",
    "result",
    "rule_based_result",
    "document_risk_result",
    "llm_report_result",
)
PRIMARY_TECHNICAL_TITLE_PATTERNS = (
    "техническое задание",
    "описание предмета закупки",
    "описание объекта закупки",
    "описание объекта",
    "описание предмета",
    "техническая спецификация",
    "спецификац",
    "перечень оборудования",
    "ведомость оборудования",
    "ооз",
    "specification",
)
PRIMARY_TZ_ABBREV_RE = re.compile(
    r"(?<![а-яa-z0-9])тз(?![а-яa-z0-9])",
    re.IGNORECASE,
)
TARGET_HARDWARE_CONTEXT_VALUES = {
    "server",
    "servers",
    "storage",
    "network",
    "security_hardware",
    "target_hardware",
    "infra_project",
}
TARGET_HARDWARE_TEXT_PATTERNS = (
    "сервер",
    "серверы",
    "серверное оборудование",
    "схд",
    "система хранения",
    "системы хранения",
    "коммутатор",
    "коммутаторы",
    "маршрутизатор",
    "сетевое оборудование",
    "межсетевой экран",
    "firewall",
    "программно-аппаратный комплекс",
)


def normalize_document_priority_text(value: Any) -> str:
    return str(value or "").casefold().replace("ё", "е")


def document_priority_text(doc: dict[str, Any]) -> str:
    return " ".join(
        str(doc.get(key) or "")
        for key in ("title", "filename", "fileName", "name")
    )


def document_has_primary_technical_hint(normalized: str) -> bool:
    return bool(
        PRIMARY_TZ_ABBREV_RE.search(normalized)
        or any(pattern in normalized for pattern in PRIMARY_TECHNICAL_TITLE_PATTERNS)
    )


def document_is_primary_technical_document(doc: dict[str, Any]) -> bool:
    try:
        from app.collector.documents import (
            document_is_primary_technical_document as collector_checker,
        )

        return bool(collector_checker(doc))
    except Exception:
        return document_has_primary_technical_hint(
            normalize_document_priority_text(document_priority_text(doc))
        )


def document_url(doc_id: str | int) -> str:
    return f"z360://document/{doc_id}"


def tender_has_target_hardware_context(tender: dict[str, Any] | None) -> bool:
    if not tender:
        return False

    chunks: list[str] = []
    for key in (
        "title",
        "category",
        "target_category",
        "business_category",
        "detected_category",
        "_llm_category",
        "_llm_category_label",
        "market_access",
    ):
        value = tender.get(key)
        if value:
            chunks.append(str(value))

    result = tender.get("result")
    if isinstance(result, dict):
        for key in (
            "detected_category",
            "category",
            "market_access",
            "summary",
            "why_relevant",
            "positive_matches",
        ):
            value = result.get(key)
            if isinstance(value, list):
                chunks.extend(str(item) for item in value)
            elif value:
                chunks.append(str(value))

    raw = tender.get("raw")
    full = raw.get("full") if isinstance(raw, dict) else None
    if isinstance(full, dict):
        for key in ("orderName", "purchaseName", "subject"):
            value = full.get(key)
            if value:
                chunks.append(str(value))
        for doc in full.get("documents") or []:
            if isinstance(doc, dict):
                chunks.append(document_priority_text(doc))

    normalized = normalize_document_priority_text("\n".join(chunks))
    if any(value in normalized for value in TARGET_HARDWARE_CONTEXT_VALUES):
        return True
    if any(pattern in normalized for pattern in TARGET_HARDWARE_TEXT_PATTERNS):
        return True

    try:
        from app.business_rules import business_assessment

        assessment = business_assessment(tender)
    except Exception:
        assessment = {}

    return str(assessment.get("market_access") or "").strip() in {
        "target_hardware",
        "infra_project",
    }


def merge_primary_tender_context(
    tender: dict[str, Any],
    tender_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not tender_context:
        return tender

    merged = dict(tender)
    for key in PRIMARY_TENDER_CONTEXT_KEYS:
        value = tender_context.get(key)
        if value not in (None, ""):
            merged[key] = value

    if not merged.get("raw") and tender_context.get("raw"):
        merged["raw"] = tender_context["raw"]

    return merged


class PreparationStatus(str, Enum):
    TENDER_NOT_FOUND = "tender_not_found"
    READY_FOR_LLM = "ready_for_llm"
    BLOCKED_BY_MARKETPLACE_AUTH = "blocked_by_marketplace_auth"
    NO_VALID_DOCUMENTS = "no_valid_documents"
    DOCUMENTS_DOWNLOADED_BUT_NO_TEXT = "documents_downloaded_but_no_text"
    PARTIAL_PREPARATION = "partial_preparation"


@dataclass
class DocumentCounts:
    found: int = 0
    downloaded: int = 0
    rejected: int = 0
    with_extracted_text: int = 0


@dataclass
class StepOutcome:
    attempted: bool = False
    succeeded: bool = False
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TenderPreparationResult:
    tender_id: str | None
    external_id: str | None
    status: PreparationStatus
    summary: str
    document_counts: DocumentCounts
    marketplace_auth_blocked: bool
    document_download_was_run: bool
    document_risk_was_run: bool
    document_risk_ready: bool
    document_readiness: str
    ready_for_llm: bool
    partial_preparation: bool
    steps: dict[str, StepOutcome] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass
class TargetedDownloadRetryState:
    attempts: int = 0
    max_attempts: int = TARGETED_REQUEUE_MAX_ATTEMPTS
    next_retry_after: datetime | None = None
    exhausted: bool = False
    reason: str = ""
    last_event_at: datetime | None = None


T = TypeVar("T")


def capture_stdout(callback: Callable[[], T]) -> tuple[T, list[str]]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = callback()

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    return result, lines


def db_connect(*, row_factory: Any | None = None):
    import psycopg

    from app.config import settings

    if row_factory is None:
        return psycopg.connect(settings.database_url)
    return psycopg.connect(settings.database_url, row_factory=row_factory)


def db_now() -> datetime:
    from psycopg.rows import dict_row

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT now() AS value;")
            row = cur.fetchone()
            return row["value"]


def load_downloaded_documents(tender_id: str) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    filename,
                    NULL AS document_url,
                    storage_path,
                    mime_type,
                    size_bytes,
                    extracted_text,
                    created_at
                FROM documents
                WHERE tender_id = %s
                ORDER BY created_at ASC;
                """,
                (tender_id,),
            )
            return list(cur.fetchall())


def load_processing_events_since(
    tender_id: str,
    since: datetime,
) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT event_type, message, created_at
                FROM processing_events
                WHERE tender_id = %s
                  AND created_at >= %s
                ORDER BY created_at ASC;
                """,
                (tender_id, since),
            )
            return list(cur.fetchall())


def load_processing_events(
    tender_id: str,
    *,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    params: list[Any] = [tender_id]
    event_filter = ""
    if event_type:
        event_filter = "AND event_type = %s"
        params.append(event_type)

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT event_type, message, created_at
                FROM processing_events
                WHERE tender_id = %s
                  {event_filter}
                ORDER BY created_at ASC;
                """,
                tuple(params),
            )
            return list(cur.fetchall())


def count_documents_with_text(docs: list[dict[str, Any]]) -> int:
    return sum(1 for doc in docs if str(doc.get("extracted_text") or "").strip())


def compact_document_match_text(value: Any) -> str:
    text = normalize_document_priority_text(str(value or ""))
    text = re.sub(r"\.[a-z0-9]{1,8}\b", " ", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def api_document_identifier(doc: dict[str, Any]) -> str:
    for key in ("id", "document_id", "documentId"):
        value = doc.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def api_document_ref(doc: dict[str, Any]) -> dict[str, str | None]:
    return {
        "document_id": api_document_identifier(doc) or None,
        "title": (
            doc.get("title")
            or doc.get("filename")
            or doc.get("fileName")
            or doc.get("name")
        ),
    }


def api_document_name_values(doc: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("title", "filename", "fileName", "name"):
        value = str(doc.get(key) or "").strip()
        if value:
            values.append(value)
    return values


def downloaded_document_matches_api_doc(
    downloaded: dict[str, Any],
    api_doc: dict[str, Any],
) -> bool:
    api_id = api_document_identifier(api_doc)
    if api_id:
        if str(downloaded.get("document_url") or "").strip() == document_url(api_id):
            return True

        filename = str(downloaded.get("filename") or "").strip()
        storage_path = str(downloaded.get("storage_path") or "").strip()
        if filename.startswith(f"{api_id}_") or filename.startswith(f"{api_id}-"):
            return True
        if f"/{api_id}_" in storage_path.replace("\\", "/"):
            return True

    downloaded_names = [
        compact_document_match_text(downloaded.get("filename")),
        compact_document_match_text(downloaded.get("storage_path")),
    ]
    downloaded_names = [name for name in downloaded_names if name]
    if not downloaded_names:
        return False

    for raw_name in api_document_name_values(api_doc):
        api_name = compact_document_match_text(raw_name)
        if not api_name:
            continue
        for downloaded_name in downloaded_names:
            if api_name in downloaded_name or downloaded_name in api_name:
                return True

    return False


def primary_technical_api_documents(
    tender: dict[str, Any],
    api_docs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    documents = api_docs
    if documents is None:
        raw = tender.get("raw") or {}
        full = raw.get("full") if isinstance(raw, dict) else {}
        documents = full.get("documents") if isinstance(full, dict) else []

    return [
        doc
        for doc in documents or []
        if isinstance(doc, dict) and document_is_primary_technical_document(doc)
    ]


def primary_technical_document_status(
    tender: dict[str, Any],
    downloaded_docs: list[dict[str, Any]],
    *,
    api_docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    primary_docs = primary_technical_api_documents(tender, api_docs)
    if not primary_docs:
        return {
            "required": False,
            "ready": True,
            "reason": None,
            "total": 0,
            "processed": [],
            "missing": [],
            "not_processed": [],
        }

    processed: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    not_processed: list[dict[str, Any]] = []

    for api_doc in primary_docs:
        matches = [
            doc
            for doc in downloaded_docs
            if downloaded_document_matches_api_doc(doc, api_doc)
        ]
        if not matches:
            missing.append(api_document_ref(api_doc))
            continue

        ready_match = next(
            (
                doc
                for doc in matches
                if str(doc.get("extracted_text") or "").strip()
            ),
            None,
        )
        if ready_match is not None:
            item = api_document_ref(api_doc)
            item["filename"] = ready_match.get("filename")
            processed.append(item)
            continue

        item = api_document_ref(api_doc)
        item["filename"] = matches[0].get("filename")
        not_processed.append(item)

    reason = None
    if missing:
        reason = PRIMARY_TECHNICAL_DOCUMENT_MISSING
    elif not_processed:
        reason = PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED

    return {
        "required": True,
        "ready": reason is None,
        "reason": reason,
        "total": len(primary_docs),
        "processed": processed,
        "missing": missing,
        "not_processed": not_processed,
    }


def primary_technical_documents_ready(status: dict[str, Any]) -> bool:
    return not status.get("required") or bool(status.get("ready"))


def parse_retry_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def parse_targeted_retry_payload(message: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(message or "{}"))
    except json.JSONDecodeError:
        return {}

    return payload if isinstance(payload, dict) else {}


def targeted_download_retry_state_from_events(
    events: list[dict[str, Any]],
) -> TargetedDownloadRetryState:
    state = TargetedDownloadRetryState()

    for event in events:
        if event.get("event_type") != TARGETED_DOWNLOAD_REQUEUE_EVENT:
            continue

        payload = parse_targeted_retry_payload(event.get("message"))
        if not payload:
            continue

        try:
            attempts = int(payload.get("attempts") or state.attempts)
        except (TypeError, ValueError):
            attempts = state.attempts
        try:
            max_attempts = int(
                payload.get("max_attempts") or TARGETED_REQUEUE_MAX_ATTEMPTS
            )
        except (TypeError, ValueError):
            max_attempts = TARGETED_REQUEUE_MAX_ATTEMPTS

        state = TargetedDownloadRetryState(
            attempts=max(0, attempts),
            max_attempts=max(1, max_attempts),
            next_retry_after=parse_retry_datetime(payload.get("next_retry_after")),
            exhausted=bool(payload.get("exhausted")),
            reason=str(payload.get("reason") or ""),
            last_event_at=event.get("created_at"),
        )

    return state


def load_targeted_download_retry_state(tender_id: str) -> TargetedDownloadRetryState:
    return targeted_download_retry_state_from_events(
        load_processing_events(
            tender_id,
            event_type=TARGETED_DOWNLOAD_REQUEUE_EVENT,
        )
    )


def seconds_until_retry(next_retry_after: datetime | None, now: datetime) -> int:
    if next_retry_after is None:
        return 0

    try:
        remaining = (next_retry_after - now).total_seconds()
    except TypeError:
        remaining = (
            next_retry_after.replace(tzinfo=None) - now.replace(tzinfo=None)
        ).total_seconds()

    return max(0, int(remaining))


def requeue_reason_for_counts(
    counts: DocumentCounts,
    *,
    primary_technical_not_ready: bool = False,
) -> str:
    if primary_technical_not_ready:
        return PRIMARY_TECHNICAL_DOCUMENT_NOT_PROCESSED

    if counts.downloaded <= 0:
        return "rate_limited_before_any_document_saved"

    if counts.with_extracted_text <= 0:
        return "rate_limited_before_any_document_text_extracted"

    return "rate_limited_before_documents_ready"


def should_requeue_after_rate_limit(
    *,
    stopped_due_to_rate_limit: bool,
    document_counts: DocumentCounts,
    primary_technical_not_ready: bool = False,
) -> bool:
    return (
        stopped_due_to_rate_limit
        and document_counts.found > 0
        and (
            document_counts.downloaded <= 0
            or document_counts.with_extracted_text <= 0
            or primary_technical_not_ready
        )
    )


def record_targeted_retry_decision(
    *,
    tender_id: str,
    external_id: str,
    attempts: int,
    requeued: bool,
    exhausted: bool,
    next_retry_after: datetime | None,
    reason: str,
    document_counts: DocumentCounts,
) -> None:
    from app.collector.documents import record_processing_event

    payload = {
        "external_id": external_id,
        "attempts": attempts,
        "max_attempts": TARGETED_REQUEUE_MAX_ATTEMPTS,
        "requeued": requeued,
        "exhausted": exhausted,
        "reason": reason,
        "documents_found": document_counts.found,
        "documents_downloaded": document_counts.downloaded,
        "documents_with_text": document_counts.with_extracted_text,
    }
    if next_retry_after is not None:
        payload["next_retry_after"] = next_retry_after.isoformat()

    record_processing_event(
        tender_id,
        TARGETED_DOWNLOAD_REQUEUE_EVENT,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def document_readiness_state(document_counts: DocumentCounts) -> str:
    if document_counts.downloaded <= 0:
        return DOCUMENTS_MISSING

    if document_counts.with_extracted_text <= 0:
        return DOCUMENTS_DOWNLOADED_WITHOUT_TEXT

    return DOCUMENTS_READY_WITH_TEXT


def count_rejected_downloads(events: list[dict[str, Any]]) -> int:
    return sum(
        1 for event in events if event.get("event_type") == "document_download_rejected"
    )


def _event_unquoted_field(message: str, field_name: str) -> str | None:
    pattern = re.compile(rf"{re.escape(field_name)}=([^,\s]+)")
    match = pattern.search(message)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"")
    return value or None


def document_failure_items_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in {"document_download_failed", "document_download_rejected"}:
            continue
        message = str(event.get("message") or "")
        item: dict[str, Any] = {"event_type": event_type}
        document_id = _event_unquoted_field(message, "document_id")
        reason = _event_unquoted_field(message, "reason")
        if document_id:
            item["document_id"] = document_id
        if reason:
            item["reason"] = reason
        if not reason and event_type == "document_download_rejected":
            item["reason"] = "rejected"
        items.append(item)
    return items


def has_marketplace_auth_block(events: list[dict[str, Any]]) -> bool:
    return any(MARKETPLACE_AUTH_HINT in str(event.get("message") or "") for event in events)


def event_message_field(events: list[dict[str, Any]], field_name: str) -> str | None:
    pattern = re.compile(rf"{re.escape(field_name)}=(['\"])(.*?)\1")
    for event in reversed(events):
        match = pattern.search(str(event.get("message") or ""))
        if match:
            value = match.group(2).strip()
            if value and value.lower() != "none":
                return value

    return None


def record_preparation_blocked_state(
    *,
    tender_id: str,
    external_id: str,
    status: PreparationStatus,
    document_counts: DocumentCounts,
    started_at: datetime,
    events: list[dict[str, Any]],
    download_details: dict[str, Any],
) -> None:
    if status not in {
        PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH,
        PreparationStatus.NO_VALID_DOCUMENTS,
    }:
        return

    try:
        from app.collector.documents import record_processing_event
    except Exception as exc:
        logger.warning("Failed to import processing event writer: %s", exc)
        return

    reason = (
        "marketplace_auth"
        if status == PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH
        else status.value
    )
    cooldown_until = started_at + timedelta(
        seconds=TARGETED_PREPARATION_BLOCKED_COOLDOWN_SECONDS
    )
    payload: dict[str, Any] = {
        "external_id": external_id,
        "status": status.value,
        "reason": reason,
        "documents_found": document_counts.found,
        "documents_selected": int(download_details.get("documents_selected") or 0),
        "documents_downloaded": document_counts.downloaded,
        "documents_with_text": document_counts.with_extracted_text,
        "documents_failed": int(download_details.get("documents_failed") or 0),
        "created_at": started_at.isoformat(),
        "cooldown_until": cooldown_until.isoformat(),
    }
    failed_items = download_details.get("documents_failed_items") or []
    if failed_items:
        payload["documents_failed_items"] = failed_items

    html_hint = event_message_field(events, "html_hint")
    html_title = event_message_field(events, "html_title")
    final_missing_reason = download_details.get("final_missing_reason")
    if html_hint:
        payload["html_hint"] = html_hint
    if html_title:
        payload["html_title"] = html_title
    if final_missing_reason:
        payload["final_missing_reason"] = str(final_missing_reason)

    record_processing_event(
        tender_id,
        TARGETED_PREPARATION_BLOCKED_EVENT,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def record_partial_document_download_state(
    *,
    tender_id: str,
    external_id: str,
    document_counts: DocumentCounts,
    started_at: datetime,
    download_details: dict[str, Any],
) -> None:
    skipped_due_to_rate_limit = int(
        download_details.get("documents_skipped_due_to_rate_limit") or 0
    )
    if skipped_due_to_rate_limit <= 0:
        return

    try:
        from app.collector.documents import record_processing_event
    except Exception as exc:
        logger.warning("Failed to import processing event writer: %s", exc)
        return

    payload = {
        "external_id": external_id,
        "status": "partial_document_download",
        "reason": "rate_limit_429",
        "documents_found": document_counts.found,
        "documents_downloaded": document_counts.downloaded,
        "documents_with_text": document_counts.with_extracted_text,
        "documents_selected": int(download_details.get("documents_selected") or 0),
        "documents_skipped_due_to_rate_limit": skipped_due_to_rate_limit,
        "documents_skipped_due_to_rate_limit_items": (
            download_details.get("documents_skipped_due_to_rate_limit_items") or []
        ),
        "missing_high_value_technical_document": bool(
            download_details.get("missing_high_value_technical_document")
        ),
        "missing_high_value_technical_document_title": (
            download_details.get("missing_high_value_technical_document_title")
        ),
        "document_download_planner_used": bool(
            download_details.get("document_download_planner_used")
        ),
        "document_download_planner_confidence": (
            download_details.get("document_download_planner_confidence")
        ),
        "rate_limit_retries": int(download_details.get("rate_limit_retries") or 0),
        "rate_limit_backoffs_seconds": (
            download_details.get("rate_limit_backoffs_seconds") or []
        ),
        "stopped_due_to_rate_limit": bool(
            download_details.get("stopped_due_to_rate_limit")
        ),
        "created_at": started_at.isoformat(),
    }
    download_details["partial_document_download_warning"] = (
        "documents_skipped_due_to_rate_limit"
    )
    record_processing_event(
        tender_id,
        PARTIAL_DOCUMENT_DOWNLOAD_EVENT,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def targeted_download_details(
    download_report: Any | None,
    *,
    limit_docs: int,
    logs: list[str],
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "limit_docs": limit_docs,
        "logs": logs,
    }

    if download_report is None:
        return details

    if hasattr(download_report, "to_dict"):
        details.update(download_report.to_dict())
    else:
        details["total_saved"] = int(download_report or 0)

    details["saved_documents"] = int(details.get("total_saved") or 0)
    details.setdefault("documents_found", 0)
    details.setdefault("documents_selected", 0)
    details.setdefault("documents_downloaded", details["saved_documents"])
    details.setdefault("documents_failed", 0)
    details.setdefault("documents_failed_items", [])
    details.setdefault("documents_skipped_due_to_rate_limit", 0)
    details.setdefault("documents_skipped_due_to_rate_limit_items", [])
    details.setdefault("missing_high_value_technical_document", False)
    details.setdefault("missing_high_value_technical_document_title", None)
    details.setdefault("document_download_planner_used", False)
    details.setdefault("document_download_planner_confidence", None)
    details.setdefault("rate_limit_retries", 0)
    details.setdefault("rate_limit_backoffs_seconds", [])
    details.setdefault("stopped_due_to_rate_limit", False)
    return details


def run_targeted_document_download(
    *,
    external_id: str | None,
    tender_id: str | None,
    tender_context: dict[str, Any] | None = None,
    limit_docs: int,
    force_redownload: bool,
    use_llm_planner: bool = True,
) -> tuple[Any, list[str]]:
    def run() -> Any:
        from app.collector.documents import (
            get_document_rate_limit_seconds,
            run_targeted_download_report,
        )
        from app.collector.zakupki360 import Zakupki360Client

        client = Zakupki360Client()
        try:
            client.login()
            return run_targeted_download_report(
                client,
                external_id=external_id,
                tender_id=tender_id,
                tender_context=tender_context,
                limit_docs=limit_docs,
                document_rate_limit_seconds=get_document_rate_limit_seconds(),
                force_redownload=force_redownload,
                use_llm_planner=use_llm_planner,
            )
        finally:
            client.client.close()

    return capture_stdout(run)


def extract_text_for_tender(
    tender_id: str,
    *,
    overwrite: bool = False,
) -> StepOutcome:
    from app.document_text_extractor import (
        APP_DIR,
        clean_text,
        extract_file,
        save_extracted_text,
    )

    docs = load_downloaded_documents(tender_id)
    selected_docs = [
        doc
        for doc in docs
        if overwrite or not str(doc.get("extracted_text") or "").strip()
    ]

    processed = 0
    extracted = 0
    skipped_missing_file = 0
    errors: list[str] = []
    document_results: list[dict[str, Any]] = []

    def mark_document(
        doc: dict[str, Any],
        *,
        status: str,
        reason: str | None = None,
        path: Path | None = None,
        chars: int = 0,
    ) -> None:
        item = {
            "document_id": str(doc.get("id") or ""),
            "filename": doc.get("filename"),
            "storage_path": doc.get("storage_path"),
            "status": status,
            "chars": chars,
        }
        if reason:
            item["reason"] = reason
        if path:
            item["path"] = str(path)
            item["suffix"] = path.suffix.lower()
        document_results.append(item)

    for doc in selected_docs:
        storage_path = doc.get("storage_path")
        if not storage_path:
            skipped_missing_file += 1
            message = (
                f"{doc.get('filename') or doc.get('id')}: missing_storage_path"
            )
            logger.warning(
                "Document text extraction skipped: tender_id=%s reason=%s",
                tender_id,
                message,
            )
            errors.append(message)
            mark_document(doc, status="failed", reason="missing_storage_path")
            continue

        path = APP_DIR / str(storage_path)
        if not path.exists():
            skipped_missing_file += 1
            message = (
                f"{doc.get('filename') or doc.get('id')}: missing_storage_file "
                f"path={path}"
            )
            logger.warning(
                "Document text extraction skipped: tender_id=%s reason=%s",
                tender_id,
                message,
            )
            errors.append(message)
            mark_document(doc, status="failed", reason="missing_storage_file", path=path)
            continue

        processed += 1

        try:
            text = clean_text(extract_file(Path(path)))
            save_extracted_text(str(doc["id"]), text)
            if text:
                extracted += 1
                mark_document(doc, status="extracted", path=path, chars=len(text))
            else:
                try:
                    from app.document_text_extractor import empty_extraction_reason

                    reason = empty_extraction_reason(Path(path))
                except Exception:
                    reason = "no_text_extracted"
                message = (
                    f"{doc.get('filename') or doc.get('id')}: {reason} "
                    f"path={path}"
                )
                logger.warning(
                    "Document text extraction produced no text: "
                    "tender_id=%s reason=%s",
                    tender_id,
                    message,
                )
                errors.append(message)
                mark_document(doc, status="failed", reason=reason, path=path)
        except Exception as exc:
            message = f"{doc.get('filename') or doc.get('id')}: {exc}"
            logger.warning(
                "Document text extraction failed: tender_id=%s reason=%s",
                tender_id,
                message,
            )
            errors.append(message)
            mark_document(doc, status="failed", reason=str(exc), path=path)

    failure_suffix_counts: dict[str, int] = {}
    for item in document_results:
        if item.get("status") != "failed":
            continue
        suffix = str(item.get("suffix") or "unknown")
        failure_suffix_counts[suffix] = failure_suffix_counts.get(suffix, 0) + 1

    failure_suffix_summary = ", ".join(
        f"{suffix}:{count}" for suffix, count in sorted(failure_suffix_counts.items())
    ) or "none"

    succeeded = not errors and extracted == len(selected_docs)
    readiness_after_step = (
        DOCUMENTS_READY_WITH_TEXT
        if extracted > 0
        else (
            DOCUMENTS_DOWNLOADED_WITHOUT_TEXT
            if docs
            else DOCUMENTS_MISSING
        )
    )
    return StepOutcome(
        attempted=bool(selected_docs),
        succeeded=succeeded,
        summary=(
            f"selected={len(selected_docs)}, attempted={processed}, "
            f"extracted={extracted}, "
            f"failures_by_suffix={failure_suffix_summary}, "
            f"skipped_missing_file={skipped_missing_file}, "
            f"readiness_after_step={readiness_after_step}"
        ),
        details={
            "documents_selected": len(selected_docs),
            "documents_processed": processed,
            "documents_attempted_extraction": processed,
            "documents_extracted": extracted,
            "documents_failed": sum(failure_suffix_counts.values()),
            "failure_suffix_counts": failure_suffix_counts,
            "skipped_missing_file": skipped_missing_file,
            "readiness_after_step": readiness_after_step,
            "document_results": document_results,
            "errors": errors,
        },
    )


def run_document_risk_analysis(
    tender: dict[str, Any],
    docs: list[dict[str, Any]],
) -> tuple[StepOutcome, dict[str, Any] | None]:
    from app.document_risk_analyzer import analyze_tender_docs, save_analysis

    if not docs:
        return (
            StepOutcome(
                attempted=False,
                succeeded=False,
                summary="skipped: no downloaded documents",
            ),
            None,
        )

    try:
        result = analyze_tender_docs(tender, docs)
        save_analysis(str(tender["id"]), result)
    except Exception as exc:
        logger.warning("Document risk analysis failed: %s", exc)
        return (
            StepOutcome(
                attempted=True,
                succeeded=False,
                summary=f"failed: {exc}",
                details={"error": str(exc)},
            ),
            None,
        )

    documents_with_text = int(result.get("documents_with_text") or 0)
    tailoring_risk = result.get("tailoring_risk")

    return (
        StepOutcome(
            attempted=True,
            succeeded=True,
            summary=(
                f"tailoring_risk={tailoring_risk}, "
                f"risk_score={result.get('risk_score')}, "
                f"documents_with_text={documents_with_text}"
            ),
            details={
                "tailoring_risk": tailoring_risk,
                "risk_score": result.get("risk_score"),
                "recommendation": result.get("recommendation"),
                "documents_with_text": documents_with_text,
            },
        ),
        result,
    )


def determine_preparation_status(
    *,
    tender_found: bool,
    document_counts: DocumentCounts,
    marketplace_auth_blocked: bool,
    document_risk_ready: bool,
    download_failed: bool = False,
    primary_technical_required: bool = False,
    primary_technical_ready: bool = True,
) -> PreparationStatus:
    if not tender_found:
        return PreparationStatus.TENDER_NOT_FOUND

    if marketplace_auth_blocked:
        return PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH

    if document_counts.found == 0 and document_counts.downloaded == 0:
        return PreparationStatus.NO_VALID_DOCUMENTS

    if document_counts.downloaded == 0:
        if download_failed:
            return PreparationStatus.PARTIAL_PREPARATION
        return PreparationStatus.NO_VALID_DOCUMENTS

    if document_counts.with_extracted_text == 0:
        if download_failed:
            return PreparationStatus.PARTIAL_PREPARATION
        return PreparationStatus.DOCUMENTS_DOWNLOADED_BUT_NO_TEXT

    if primary_technical_required and not primary_technical_ready:
        return PreparationStatus.PARTIAL_PREPARATION

    if document_risk_ready:
        return PreparationStatus.READY_FOR_LLM

    return PreparationStatus.PARTIAL_PREPARATION


def is_partial_preparation(
    *,
    status: PreparationStatus,
    document_counts: DocumentCounts,
    errors: list[str],
) -> bool:
    if status == PreparationStatus.PARTIAL_PREPARATION:
        return True

    if status == PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH:
        return True

    if errors:
        return True

    if document_counts.rejected > 0:
        return True

    if document_counts.found > 0 and document_counts.downloaded < document_counts.found:
        return True

    return False


def format_rate_limited_documents(items: list[dict[str, Any]]) -> str:
    if not items:
        return "none"

    formatted: list[str] = []
    for item in items[:5]:
        doc_id = item.get("document_id") or "-"
        title = str(item.get("title") or "-").strip()
        if len(title) > 80:
            title = f"{title[:77]}..."
        formatted.append(f"{doc_id}:{title}")

    remaining = len(items) - len(formatted)
    if remaining > 0:
        formatted.append(f"+{remaining} more")

    return "; ".join(formatted)


def format_failed_documents(items: list[dict[str, Any]]) -> str:
    if not items:
        return "none"

    formatted: list[str] = []
    for item in items[:5]:
        doc_id = item.get("document_id") or "-"
        title = str(item.get("title") or "-").strip()
        reason = str(item.get("reason") or "unknown").strip()
        if len(title) > 80:
            title = f"{title[:77]}..."
        formatted.append(f"{doc_id}:{title}:{reason}")

    remaining = len(items) - len(formatted)
    if remaining > 0:
        formatted.append(f"+{remaining} more")

    return "; ".join(formatted)


def build_preparation_summary(
    status: PreparationStatus,
    *,
    document_counts: DocumentCounts,
    document_risk_ready: bool,
    download_details: dict[str, Any] | None = None,
) -> str:
    if status == PreparationStatus.TENDER_NOT_FOUND:
        return "Tender was not found."

    if status == PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH:
        summary = (
            "Preparation is blocked: at least one document requires external "
            "marketplace authorization."
        )
    elif status == PreparationStatus.NO_VALID_DOCUMENTS:
        summary = "No valid downloaded documents are available for LLM preparation."
    elif status == PreparationStatus.DOCUMENTS_DOWNLOADED_BUT_NO_TEXT:
        summary = (
            "Documents are downloaded, but text extraction produced no usable text."
        )
    elif status == PreparationStatus.READY_FOR_LLM:
        summary = (
            "Tender is ready for the next LLM step: "
            f"{document_counts.with_extracted_text} document(s) have extracted text "
            f"and document risk is {'ready' if document_risk_ready else 'not ready'}."
        )
    else:
        summary = (
            "Tender preparation is partial: some data is available, but the case is "
            "not ready for the next LLM step."
        )

    details = download_details or {}
    documents_selected = int(details.get("documents_selected") or 0)
    skipped_due_to_rate_limit = int(
        details.get("documents_skipped_due_to_rate_limit") or 0
    )
    rate_limited_items = details.get("documents_skipped_due_to_rate_limit_items") or []
    documents_failed = int(details.get("documents_failed") or 0)
    failed_items = details.get("documents_failed_items") or []
    retry_attempts = int(details.get("persistent_retry_attempts") or 0)
    retry_max_attempts = int(
        details.get("persistent_retry_max_attempts")
        or TARGETED_REQUEUE_MAX_ATTEMPTS
    )
    requeue_scheduled = bool(details.get("persistent_requeue_scheduled"))
    requeue_waiting = bool(details.get("persistent_requeue_waiting"))
    retry_exhausted = bool(details.get("persistent_retry_exhausted"))
    diagnostics = (
        f"documents_found={document_counts.found}, "
        f"documents_selected={documents_selected}, "
        f"documents_downloaded={document_counts.downloaded}, "
        f"documents_failed={documents_failed}, "
        "documents_skipped_due_to_rate_limit="
        f"{skipped_due_to_rate_limit}, "
        f"persistent_retry_attempts={retry_attempts}/{retry_max_attempts}, "
        f"targeted_requeue_scheduled={str(requeue_scheduled).lower()}"
    )
    if requeue_waiting:
        diagnostics = (
            f"{diagnostics}, targeted_requeue_waiting=true, "
            f"retry_wait_seconds={int(details.get('retry_wait_seconds') or 0)}"
        )
    if requeue_scheduled and details.get("next_retry_after"):
        diagnostics = (
            f"{diagnostics}, next_retry_after={details['next_retry_after']}"
        )
    if retry_exhausted:
        diagnostics = (
            f"{diagnostics}, persistent_retry_exhausted=true, "
            "final_missing_reason="
            f"{details.get('final_missing_reason') or 'targeted_retry_exhausted'}"
        )
    if skipped_due_to_rate_limit:
        diagnostics = (
            f"{diagnostics}, "
            "rate_limited_documents="
            f"{format_rate_limited_documents(rate_limited_items)}"
        )
    if documents_failed:
        diagnostics = (
            f"{diagnostics}, failed_documents="
            f"{format_failed_documents(failed_items)}"
        )
    if details.get("missing_high_value_technical_document"):
        diagnostics = (
            f"{diagnostics}, "
            "missing_high_value_technical_document=true"
        )
        if details.get("missing_high_value_technical_document_title"):
            diagnostics = (
                f"{diagnostics}, "
                "missing_high_value_technical_document_title="
                f"{details['missing_high_value_technical_document_title']}"
            )
    if details.get("document_download_planner_used"):
        diagnostics = (
            f"{diagnostics}, document_download_planner_used=true, "
            "document_download_planner_confidence="
            f"{details.get('document_download_planner_confidence') or 'unknown'}"
        )
    if details.get("primary_technical_documents_required"):
        diagnostics = (
            f"{diagnostics}, "
            "primary_technical_documents_ready="
            f"{str(bool(details.get('primary_technical_documents_ready'))).lower()}"
        )
        if details.get("primary_technical_document_reason"):
            diagnostics = (
                f"{diagnostics}, "
                "primary_technical_document_reason="
                f"{details['primary_technical_document_reason']}"
            )

    return f"{summary} Download diagnostics: {diagnostics}."


def prepare_tender_for_analysis(
    *,
    external_id: str | None = None,
    tender_id: str | None = None,
    tender_context: dict[str, Any] | None = None,
    limit_docs: int = 5,
    force_redownload: bool = False,
    overwrite_text: bool = False,
    use_llm_planner: bool = True,
) -> TenderPreparationResult:
    from app.collector.documents import get_target_tender, tender_documents

    if bool(external_id) == bool(tender_id):
        raise ValueError("Use exactly one of external_id or tender_id")

    started_at = db_now()
    errors: list[str] = []
    steps: dict[str, StepOutcome] = {}

    tender = get_target_tender(external_id=external_id, tender_id=tender_id)
    if not tender:
        counts = DocumentCounts()
        status = determine_preparation_status(
            tender_found=False,
            document_counts=counts,
            marketplace_auth_blocked=False,
            document_risk_ready=False,
        )
        return TenderPreparationResult(
            tender_id=tender_id,
            external_id=external_id,
            status=status,
            summary=build_preparation_summary(
                status,
                document_counts=counts,
                document_risk_ready=False,
            ),
            document_counts=counts,
            marketplace_auth_blocked=False,
            document_download_was_run=False,
            document_risk_was_run=False,
            document_risk_ready=False,
            document_readiness=document_readiness_state(counts),
            ready_for_llm=False,
            partial_preparation=False,
            steps=steps,
            errors=[],
        )

    tender_id_value = str(tender["id"])
    external_id_value = str(tender["external_id"])
    primary_context_tender = merge_primary_tender_context(tender, tender_context)
    api_documents = tender_documents(primary_context_tender)
    found_documents = len(api_documents)

    download_failed = False
    download_stopped_due_to_rate_limit = False
    download_report: Any | None = None
    saved_count = 0
    download_logs: list[str] = []
    download_retry_state = TargetedDownloadRetryState()
    download_requeue_waiting = False
    download_requeue_scheduled = False
    download_retry_exhausted = False
    final_missing_reason: str | None = None

    if found_documents:
        docs_before_download = load_downloaded_documents(tender_id_value)
        primary_status_before_download = primary_technical_document_status(
            primary_context_tender,
            docs_before_download,
            api_docs=api_documents,
        )
        primary_not_ready_before_download = (
            primary_status_before_download.get("required")
            and not primary_status_before_download.get("ready")
        )
        counts_before_download = DocumentCounts(
            found=found_documents,
            downloaded=len(docs_before_download),
            with_extracted_text=count_documents_with_text(docs_before_download),
        )
        download_retry_state = load_targeted_download_retry_state(tender_id_value)
        retry_wait_seconds = seconds_until_retry(
            download_retry_state.next_retry_after,
            started_at,
        )
        skip_for_retry_wait = (
            not force_redownload
            and (
                counts_before_download.with_extracted_text <= 0
                or primary_not_ready_before_download
            )
            and download_retry_state.next_retry_after is not None
            and retry_wait_seconds > 0
            and not download_retry_state.exhausted
        )
        skip_for_retry_exhaustion = (
            not force_redownload
            and (
                counts_before_download.with_extracted_text <= 0
                or primary_not_ready_before_download
            )
            and download_retry_state.exhausted
        )
        skip_for_existing_ready = (
            not force_redownload
            and counts_before_download.with_extracted_text > 0
            and (
                (
                    primary_status_before_download.get("required")
                    and primary_status_before_download.get("ready")
                )
                or counts_before_download.downloaded >= found_documents
            )
        )

        if skip_for_existing_ready:
            steps["targeted_document_download"] = StepOutcome(
                attempted=False,
                succeeded=True,
                summary="skipped: existing extracted documents are ready",
                details={
                    "limit_docs": limit_docs,
                    "logs": download_logs,
                    "documents_found": found_documents,
                    "documents_selected": 0,
                    "documents_downloaded": counts_before_download.downloaded,
                    "documents_failed": 0,
                    "documents_failed_items": [],
                    "saved_documents": 0,
                    "documents_skipped_due_to_rate_limit": 0,
                    "documents_skipped_due_to_rate_limit_items": [],
                    "missing_high_value_technical_document": False,
                    "missing_high_value_technical_document_title": None,
                    "document_download_planner_used": False,
                    "document_download_planner_confidence": None,
                    "rate_limit_retries": 0,
                    "rate_limit_backoffs_seconds": [],
                    "stopped_due_to_rate_limit": False,
                    "persistent_retry_attempts": download_retry_state.attempts,
                    "persistent_retry_max_attempts": download_retry_state.max_attempts,
                    "persistent_requeue_waiting": False,
                    "persistent_requeue_scheduled": False,
                    "persistent_retry_exhausted": False,
                    "final_missing_reason": None,
                    "primary_technical_documents_required": bool(
                        primary_status_before_download.get("required")
                    ),
                    "primary_technical_documents_ready": bool(
                        primary_status_before_download.get("ready")
                    ),
                    "primary_technical_document_reason": (
                        primary_status_before_download.get("reason")
                    ),
                    "primary_technical_documents_missing": (
                        primary_status_before_download.get("missing") or []
                    ),
                    "primary_technical_documents_not_processed": (
                        primary_status_before_download.get("not_processed") or []
                    ),
                },
            )
        elif skip_for_retry_wait or skip_for_retry_exhaustion:
            download_requeue_waiting = skip_for_retry_wait
            download_retry_exhausted = skip_for_retry_exhaustion
            final_missing_reason = None
            if skip_for_retry_exhaustion:
                final_missing_reason = (
                    PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT
                    if primary_not_ready_before_download
                    else "targeted_retry_exhausted_after_rate_limit"
                )
            next_retry_after = download_retry_state.next_retry_after
            reason = download_retry_state.reason or "targeted_download_rate_limited"
            summary = (
                "skipped: persistent retry exhausted "
                if skip_for_retry_exhaustion
                else "skipped: persistent retry waiting "
            )
            summary = (
                f"{summary}attempts={download_retry_state.attempts}/"
                f"{download_retry_state.max_attempts}, reason={reason}"
            )
            if skip_for_retry_wait:
                summary = f"{summary}, retry_wait_seconds={retry_wait_seconds}"

            logger.warning(
                "Targeted document preparation retry skipped: "
                "external_id=%s attempts=%s/%s waiting=%s exhausted=%s "
                "retry_wait_seconds=%s reason=%s",
                external_id_value,
                download_retry_state.attempts,
                download_retry_state.max_attempts,
                skip_for_retry_wait,
                skip_for_retry_exhaustion,
                retry_wait_seconds,
                reason,
            )
            steps["targeted_document_download"] = StepOutcome(
                attempted=False,
                succeeded=False,
                summary=summary,
                details={
                    "limit_docs": limit_docs,
                    "logs": download_logs,
                    "documents_found": found_documents,
                    "documents_selected": 0,
                    "documents_downloaded": counts_before_download.downloaded,
                    "documents_failed": 0,
                    "documents_failed_items": [],
                    "saved_documents": 0,
                    "documents_skipped_due_to_rate_limit": 0,
                    "documents_skipped_due_to_rate_limit_items": [],
                    "missing_high_value_technical_document": False,
                    "missing_high_value_technical_document_title": None,
                    "document_download_planner_used": False,
                    "document_download_planner_confidence": None,
                    "rate_limit_retries": 0,
                    "rate_limit_backoffs_seconds": [],
                    "stopped_due_to_rate_limit": False,
                    "persistent_retry_attempts": download_retry_state.attempts,
                    "persistent_retry_max_attempts": download_retry_state.max_attempts,
                    "persistent_requeue_waiting": skip_for_retry_wait,
                    "persistent_requeue_scheduled": False,
                    "persistent_retry_exhausted": skip_for_retry_exhaustion,
                    "next_retry_after": (
                        next_retry_after.isoformat()
                        if next_retry_after is not None
                        else None
                    ),
                    "retry_wait_seconds": retry_wait_seconds,
                    "retry_reason": reason,
                    "final_missing_reason": final_missing_reason,
                    "primary_technical_documents_required": bool(
                        primary_status_before_download.get("required")
                    ),
                    "primary_technical_documents_ready": bool(
                        primary_status_before_download.get("ready")
                    ),
                    "primary_technical_document_reason": (
                        primary_status_before_download.get("reason")
                    ),
                    "primary_technical_documents_missing": (
                        primary_status_before_download.get("missing") or []
                    ),
                    "primary_technical_documents_not_processed": (
                        primary_status_before_download.get("not_processed") or []
                    ),
                },
            )
        else:
            try:
                download_report, download_logs = run_targeted_document_download(
                    external_id=external_id if external_id else None,
                    tender_id=tender_id if tender_id else None,
                    tender_context=tender_context,
                    limit_docs=limit_docs,
                    force_redownload=force_redownload,
                    use_llm_planner=use_llm_planner,
                )
                details = targeted_download_details(
                    download_report,
                    limit_docs=limit_docs,
                    logs=download_logs,
                )
                saved_count = int(details.get("saved_documents") or 0)
                download_stopped_due_to_rate_limit = bool(
                    details.get("stopped_due_to_rate_limit")
                )
                rate_limit_skipped = int(
                    details.get("documents_skipped_due_to_rate_limit") or 0
                )
                rate_limit_retries = int(details.get("rate_limit_retries") or 0)
                details["persistent_retry_attempts"] = download_retry_state.attempts
                details["persistent_retry_max_attempts"] = (
                    download_retry_state.max_attempts
                )
                details.setdefault("persistent_requeue_scheduled", False)
                details.setdefault("persistent_requeue_waiting", False)
                details.setdefault("persistent_retry_exhausted", False)
                steps["targeted_document_download"] = StepOutcome(
                    attempted=True,
                    succeeded=True,
                    summary=(
                        f"saved={saved_count}, "
                        f"rate_limit_retries={rate_limit_retries}, "
                        f"skipped_due_to_rate_limit={rate_limit_skipped}, "
                        "persistent_retry_attempts="
                        f"{download_retry_state.attempts}/"
                        f"{download_retry_state.max_attempts}"
                    ),
                    details=details,
                )
            except Exception as exc:
                download_failed = True
                message = f"targeted document download failed: {exc}"
                logger.warning(message)
                errors.append(message)
                steps["targeted_document_download"] = StepOutcome(
                    attempted=True,
                    succeeded=False,
                    summary=str(exc),
                    details={
                        "error": str(exc),
                        "logs": download_logs,
                        "persistent_retry_attempts": download_retry_state.attempts,
                        "persistent_retry_max_attempts": (
                            download_retry_state.max_attempts
                        ),
                        "persistent_requeue_scheduled": False,
                        "persistent_requeue_waiting": False,
                        "persistent_retry_exhausted": False,
                    },
                )
    else:
        steps["targeted_document_download"] = StepOutcome(
            attempted=False,
            succeeded=False,
            summary="skipped: tender has no document inventory",
        )

    extraction_step = extract_text_for_tender(
        tender_id_value,
        overwrite=overwrite_text,
    )
    steps["text_extraction"] = extraction_step
    errors.extend(extraction_step.details.get("errors") or [])

    docs_after_extraction = load_downloaded_documents(tender_id_value)
    risk_step, risk_result = run_document_risk_analysis(tender, docs_after_extraction)
    steps["document_risk_analysis"] = risk_step
    if not risk_step.succeeded and risk_step.details.get("error"):
        errors.append(f"document risk analysis failed: {risk_step.details['error']}")

    events = load_processing_events_since(tender_id_value, started_at)
    document_failure_items = document_failure_items_from_events(events)
    counts = DocumentCounts(
        found=found_documents,
        downloaded=len(docs_after_extraction),
        rejected=count_rejected_downloads(events),
        with_extracted_text=count_documents_with_text(docs_after_extraction),
    )
    primary_status = primary_technical_document_status(
        primary_context_tender,
        docs_after_extraction,
        api_docs=api_documents,
    )
    primary_not_ready = (
        primary_status.get("required")
        and not primary_status.get("ready")
    )
    readiness = (
        str(primary_status.get("reason"))
        if primary_not_ready and primary_status.get("reason")
        else document_readiness_state(counts)
    )

    download_details = steps["targeted_document_download"].details
    if document_failure_items:
        existing_failure_items = download_details.get("documents_failed_items") or []
        if not existing_failure_items:
            download_details["documents_failed_items"] = document_failure_items
        download_details["documents_failed"] = max(
            int(download_details.get("documents_failed") or 0),
            len(document_failure_items),
        )
    if should_requeue_after_rate_limit(
        stopped_due_to_rate_limit=download_stopped_due_to_rate_limit,
        document_counts=counts,
        primary_technical_not_ready=bool(primary_not_ready),
    ):
        next_attempts = download_retry_state.attempts + 1
        download_retry_exhausted = next_attempts >= TARGETED_REQUEUE_MAX_ATTEMPTS
        download_requeue_scheduled = not download_retry_exhausted
        final_missing_reason = None
        if download_retry_exhausted:
            final_missing_reason = (
                PREPARATION_MISSING_PRIMARY_TECHNICAL_DOCUMENT
                if primary_not_ready
                else "targeted_retry_exhausted_after_rate_limit"
            )
        next_retry_after = (
            None
            if download_retry_exhausted
            else started_at + timedelta(seconds=TARGETED_REQUEUE_BACKOFF_SECONDS)
        )
        retry_reason = requeue_reason_for_counts(
            counts,
            primary_technical_not_ready=bool(primary_not_ready),
        )
        record_targeted_retry_decision(
            tender_id=tender_id_value,
            external_id=external_id_value,
            attempts=next_attempts,
            requeued=download_requeue_scheduled,
            exhausted=download_retry_exhausted,
            next_retry_after=next_retry_after,
            reason=retry_reason,
            document_counts=counts,
        )
        download_details["persistent_retry_attempts"] = next_attempts
        download_details["persistent_retry_max_attempts"] = (
            TARGETED_REQUEUE_MAX_ATTEMPTS
        )
        download_details["persistent_requeue_scheduled"] = download_requeue_scheduled
        download_details["persistent_requeue_waiting"] = False
        download_details["persistent_retry_exhausted"] = download_retry_exhausted
        download_details["retry_reason"] = retry_reason
        download_details["retry_wait_seconds"] = (
            0 if next_retry_after is None else TARGETED_REQUEUE_BACKOFF_SECONDS
        )
        download_details["next_retry_after"] = (
            next_retry_after.isoformat() if next_retry_after is not None else None
        )
        download_details["final_missing_reason"] = final_missing_reason
        logger.warning(
            "Targeted document preparation retry decision: "
            "external_id=%s requeued=%s exhausted=%s attempts=%s/%s "
            "reason=%s documents_found=%s documents_downloaded=%s "
            "documents_with_text=%s next_retry_after=%s",
            external_id_value,
            download_requeue_scheduled,
            download_retry_exhausted,
            next_attempts,
            TARGETED_REQUEUE_MAX_ATTEMPTS,
            retry_reason,
            counts.found,
            counts.downloaded,
            counts.with_extracted_text,
            download_details["next_retry_after"],
        )

    marketplace_auth_blocked = has_marketplace_auth_block(events)
    document_risk_ready = bool(
        risk_result
        and int(risk_result.get("documents_with_text") or 0) > 0
        and risk_result.get("tailoring_risk") != "unknown"
    )
    download_failed_for_status = (
        download_failed
        or download_stopped_due_to_rate_limit
        or download_requeue_waiting
        or download_requeue_scheduled
    )
    if download_retry_exhausted and not download_failed:
        download_failed_for_status = False
    status = determine_preparation_status(
        tender_found=True,
        document_counts=counts,
        marketplace_auth_blocked=marketplace_auth_blocked,
        document_risk_ready=document_risk_ready,
        download_failed=download_failed_for_status,
        primary_technical_required=bool(primary_status.get("required")),
        primary_technical_ready=bool(primary_status.get("ready")),
    )
    ready_for_llm = status == PreparationStatus.READY_FOR_LLM
    partial = is_partial_preparation(
        status=status,
        document_counts=counts,
        errors=errors,
    )
    download_details.setdefault("documents_found", found_documents)
    download_details.setdefault("documents_selected", 0)
    download_details["documents_downloaded"] = counts.downloaded
    download_details.setdefault("documents_failed", 0)
    download_details.setdefault("documents_failed_items", [])
    download_details.setdefault("documents_skipped_due_to_rate_limit", 0)
    download_details.setdefault("documents_skipped_due_to_rate_limit_items", [])
    download_details.setdefault("missing_high_value_technical_document", False)
    download_details.setdefault("missing_high_value_technical_document_title", None)
    download_details.setdefault("document_download_planner_used", False)
    download_details.setdefault("document_download_planner_confidence", None)
    download_details.setdefault(
        "persistent_retry_attempts",
        download_retry_state.attempts,
    )
    download_details.setdefault(
        "persistent_retry_max_attempts",
        download_retry_state.max_attempts,
    )
    download_details.setdefault("persistent_requeue_scheduled", False)
    download_details.setdefault("persistent_requeue_waiting", download_requeue_waiting)
    download_details.setdefault("persistent_retry_exhausted", download_retry_exhausted)
    download_details.setdefault("final_missing_reason", final_missing_reason)
    download_details["primary_technical_documents_required"] = bool(
        primary_status.get("required")
    )
    download_details["primary_technical_documents_ready"] = bool(
        primary_status.get("ready")
    )
    download_details["primary_technical_document_reason"] = primary_status.get("reason")
    download_details["primary_technical_documents_total"] = int(
        primary_status.get("total") or 0
    )
    download_details["primary_technical_documents_processed"] = (
        primary_status.get("processed") or []
    )
    download_details["primary_technical_documents_missing"] = (
        primary_status.get("missing") or []
    )
    download_details["primary_technical_documents_not_processed"] = (
        primary_status.get("not_processed") or []
    )

    record_partial_document_download_state(
        tender_id=tender_id_value,
        external_id=external_id_value,
        document_counts=counts,
        started_at=started_at,
        download_details=download_details,
    )

    record_preparation_blocked_state(
        tender_id=tender_id_value,
        external_id=external_id_value,
        status=status,
        document_counts=counts,
        started_at=started_at,
        events=events,
        download_details=download_details,
    )

    return TenderPreparationResult(
        tender_id=tender_id_value,
        external_id=external_id_value,
        status=status,
        summary=build_preparation_summary(
            status,
            document_counts=counts,
            document_risk_ready=document_risk_ready,
            download_details=download_details,
        ),
        document_counts=counts,
        marketplace_auth_blocked=marketplace_auth_blocked,
        document_download_was_run=steps["targeted_document_download"].attempted,
        document_risk_was_run=risk_step.attempted,
        document_risk_ready=document_risk_ready,
        document_readiness=readiness,
        ready_for_llm=ready_for_llm,
        partial_preparation=partial,
        steps=steps,
        errors=errors,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare one tender for future LLM analysis without calling an LLM"
    )
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--external-id", help="Tender external_id, e.g. 95447274")
    selector.add_argument("--tender-id", help="Tender UUID from the local database")
    parser.add_argument("--limit-docs", type=int, default=5)
    parser.add_argument("--force-redownload", action="store_true")
    parser.add_argument("--overwrite-text", action="store_true")
    parser.add_argument(
        "--no-llm-planner",
        action="store_true",
        help="Use deterministic document ordering without the LLM document planner",
    )
    parser.add_argument(
        "--compact-json",
        action="store_true",
        help="Print one-line JSON instead of pretty JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    args = build_parser().parse_args(argv)

    result = prepare_tender_for_analysis(
        external_id=args.external_id,
        tender_id=args.tender_id,
        limit_docs=args.limit_docs,
        force_redownload=args.force_redownload,
        overwrite_text=args.overwrite_text,
        use_llm_planner=not args.no_llm_planner,
    )

    print(result.to_json(indent=None if args.compact_json else 2))

    if result.status == PreparationStatus.TENDER_NOT_FOUND:
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
