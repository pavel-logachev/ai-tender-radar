from __future__ import annotations

import argparse
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import psycopg
from psycopg.rows import dict_row

from app.config import settings
from app.collector.document_rate_limit import resolve_document_rate_limit_seconds
from app.collector.download_validation import (
    extension_for_detected_file_type,
    validate_downloaded_document,
)
from app.collector.html_diagnostics import (
    EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT,
    extract_html_diagnostics,
)
from app.collector.safe_http_download import (
    BufferedDownloadResponse,
    download_bounded_https_document,
    is_allowed_https_url,
)
from app.collector.zakupki360 import Zakupki360Client
from app.business_rules import is_incumbent_extension, is_service_noise


DATA_DIR = Path("/app/data/documents")
APP_DIR = Path("/app")

logger = logging.getLogger(__name__)


class MarketplaceAuthRequiredError(RuntimeError):
    pass


TARGETED_RATE_LIMIT_MAX_RETRIES = 1
TARGETED_RATE_LIMIT_BACKOFF_MIN_SECONDS = 5.0
TARGETED_RATE_LIMIT_BACKOFF_MAX_SECONDS = 20.0
DIRECT_DOCUMENT_ALLOWED_HOST_SUFFIX = "zakupki.gov.ru"
DIRECT_DOCUMENT_URL_KEYS = ("url", "downloadUrl", "href", "link")
DIRECT_DOCUMENT_MAX_REDIRECTS = 3
DIRECT_DOCUMENT_MAX_BYTES = 64 * 1024 * 1024


PRIMARY_TECHNICAL_DOCUMENT_PATTERNS = (
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

PROCUREMENT_MATERIAL_PATTERNS = (
    "конкурсная документация",
    "закупочная документация",
    "документация",
    "материалы закупки",
    "материалы",
)

STRONG_HARDWARE_DOCUMENT_PATTERNS = (
    "сервер",
    "серверы",
    "серверное оборудование",
    "схд",
    "система хранения",
    "системы хранения",
    "storage",
    "коммутатор",
    "коммутаторы",
    "маршрутизатор",
    "маршрутизаторы",
    "сетевое оборудование",
    "межсетевой экран",
    "firewall",
    "программно-аппаратный комплекс",
    "мфу",
    "арм",
    "рабочая станция",
    "рабочие станции",
    "компьютер",
)

BROAD_HARDWARE_DOCUMENT_PATTERNS = (
    "оборудование",
    "оборудования",
    "онм",
)

TARGET_HARDWARE_CATEGORY_VALUES = {
    "servers",
    "server",
    "storage",
    "network",
    "security_hardware",
    "security",
    "target_hardware",
    "infra_project",
}

TENDER_PRIORITY_CONTEXT_KEYS = (
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

CONTRACT_DOCUMENT_PATTERNS = (
    "проект договора",
    "проект контракта",
    "договор",
    "контракт",
    "contract",
)

APPLICATION_REQUIREMENTS_PATTERNS = (
    "требован",
    "заявк",
    "application requirements",
)

PRICE_DOCUMENT_PATTERNS = (
    "нмцк",
    "нмцд",
    "расчет",
    "расчёт",
    "обоснован",
    "price calculation",
)

NOTICE_DOCUMENT_PATTERNS = (
    "извещение",
    "notice",
)

KD_ABBREV_RE = re.compile(r"(?<![а-яa-z0-9])кд(?![а-яa-z0-9])", re.IGNORECASE)
TZ_ABBREV_RE = re.compile(r"(?<![а-яa-z0-9])тз(?![а-яa-z0-9])", re.IGNORECASE)
PC_ABBREV_RE = re.compile(r"(?<![а-яa-z0-9])пк(?![а-яa-z0-9])", re.IGNORECASE)


@dataclass
class TargetedDownloadResult:
    total_saved: int = 0
    documents_found: int = 0
    documents_selected: int = 0
    documents_selected_items: list[dict[str, Any]] = (
        field(default_factory=list)
    )
    documents_downloaded: int = 0
    documents_failed: int = 0
    documents_failed_items: list[dict[str, Any]] = field(default_factory=list)
    documents_skipped_due_to_rate_limit: int = 0
    documents_skipped_due_to_rate_limit_items: list[dict[str, Any]] = (
        field(default_factory=list)
    )
    rate_limit_retries: int = 0
    rate_limit_backoffs_seconds: list[float] = field(default_factory=list)
    stopped_due_to_rate_limit: bool = False
    missing_high_value_technical_document: bool = False
    missing_high_value_technical_document_title: str | None = None
    document_download_planner_used: bool = False
    document_download_planner_confidence: str | None = None
    document_download_planner_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def get_document_rate_limit_seconds() -> float:
    return resolve_document_rate_limit_seconds(
        settings.z360_document_rate_limit_seconds,
        settings.z360_rate_limit_seconds,
    )


def is_rate_limit_error(exc: Exception) -> bool:
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 429
    )


RENEWAL_PATTERNS = [
    "продлен",
    "продление",
    "техническ",
    "техподдерж",
    "сертификат",
    "ключ",
    "активац",
    "обновлен",
    "неисключительн",
    "право использования",
    "прав использования",
    "лиценз",
    "ваучер",
    "подписк",
]


SERVICE_NOISE_PATTERNS = [
    "разработка",
    "развити",
    "сопровожд",
    "аттестаци",
    "мониторинг жилищного фонда",
    "мониторинг и реагирование",
    "реагировани на инцидент",
    "госсопка",
    "услуг по мониторингу",
    "система мониторинга событий",
    "системы мониторинга событий",
    "приобретение и монтаж систем видеонаблюдения",
    "монтаж систем видеонаблюдения",
    "оборудования для видеонаблюдения",
    "оргтехник",
    "запасных частей для вычислительной техники",
    "периферийн",
]


HARDWARE_HINTS = [
    "сервер",
    "серверное оборудование",
    "вычислительный комплекс",
    "схд",
    "система хранения",
    "дисковая полка",
    "коммутатор",
    "маршрутизатор",
    "межсетевой экран",
    "firewall",
    "программно-аппаратный комплекс",
    "поставка оборудования",
]


def norm(value: str | None) -> str:
    return str(value or "").lower().replace("ё", "е")


def contains_any(text: str, patterns: list[str]) -> bool:
    normalized = norm(text)
    return any(norm(pattern) in normalized for pattern in patterns)


def looks_like_renewal(title: str) -> bool:
    return contains_any(title, RENEWAL_PATTERNS)


def looks_like_service_noise(title: str) -> bool:
    normalized = norm(title)
    if contains_any(normalized, HARDWARE_HINTS):
        return False

    return contains_any(normalized, SERVICE_NOISE_PATTERNS)


def document_url(doc_id: str | int) -> str:
    return f"z360://document/{doc_id}"


def direct_document_url(doc: dict[str, Any]) -> str | None:
    for key in DIRECT_DOCUMENT_URL_KEYS:
        value = str(doc.get(key) or "").strip()
        if value:
            return value
    return None


def is_allowed_direct_document_url(value: str | None) -> bool:
    return is_allowed_https_url(
        value,
        allowed_host_suffixes=(DIRECT_DOCUMENT_ALLOWED_HOST_SUFFIX,),
    )


def download_direct_document(
    doc: dict[str, Any],
    *,
    document_rate_limit_seconds: float,
) -> BufferedDownloadResponse | None:
    url = direct_document_url(doc)
    if not is_allowed_direct_document_url(url):
        return None

    time.sleep(max(0.0, document_rate_limit_seconds))
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as direct_client:
            return download_bounded_https_document(
                direct_client,
                str(url),
                allowed_host_suffixes=(DIRECT_DOCUMENT_ALLOWED_HOST_SUFFIX,),
                max_redirects=DIRECT_DOCUMENT_MAX_REDIRECTS,
                max_bytes=DIRECT_DOCUMENT_MAX_BYTES,
            )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            print(
                "Direct document download hit 429. "
                "Stop current API call without retrying to protect source quota."
            )
        raise


def existing_document_info(tender_id: str, doc_id: str | int) -> dict | None:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, filename, mime_type, storage_path, size_bytes
                FROM documents
                WHERE tender_id = %s
                  AND url = %s
                LIMIT 1;
                """,
                (str(tender_id), document_url(doc_id)),
            )
            return cur.fetchone()


def is_inside_app_dir(file_path: Path) -> bool:
    try:
        file_path.resolve(strict=False).relative_to(APP_DIR.resolve(strict=False))
    except ValueError:
        return False

    return True


def remove_replaced_document_file(existing: dict | None, new_file_path: Path) -> None:
    if not existing:
        return

    storage_path = existing.get("storage_path")
    if not storage_path:
        return

    old_file_path = APP_DIR / storage_path
    if not is_inside_app_dir(old_file_path):
        logger.warning(
            "Refuse to remove replaced document outside app dir: storage_path=%r",
            storage_path,
        )
        return

    if old_file_path == new_file_path:
        return

    if not old_file_path.exists() or not old_file_path.is_file():
        return

    try:
        old_file_path.unlink()
        print(f"Removed replaced document file: {old_file_path}")
    except OSError as exc:
        logger.warning(
            "Failed to remove replaced document file: storage_path=%r error=%s",
            storage_path,
            exc,
        )


def record_processing_event(tender_id: str, event_type: str, message: str) -> None:
    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processing_events (tender_id, event_type, message)
                    VALUES (%s, %s, %s);
                    """,
                    (tender_id, event_type, message[:2000]),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to write processing event: %s", exc)


def delete_document_record(document_id: str) -> None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM documents
                WHERE id = %s;
                """,
                (document_id,),
            )
        conn.commit()


def cleanup_invalid_existing_document(
    tender_id: str,
    doc_id: str | int,
    existing: dict,
    reason: str,
) -> None:
    storage_path = existing.get("storage_path")
    filename = existing.get("filename") or ""
    mime_type = existing.get("mime_type")
    file_path = APP_DIR / storage_path if storage_path else None

    if file_path and file_path.exists():
        try:
            file_path.unlink()
        except OSError as exc:
            logger.warning(
                "Failed to remove invalid existing document file: "
                "tender_id=%s document_id=%s filename=%r storage_path=%r reason=%s error=%s",
                tender_id,
                doc_id,
                filename,
                storage_path,
                reason,
                exc,
            )

    document_db_id = existing.get("id")
    if document_db_id:
        delete_document_record(str(document_db_id))

    message = (
        "Rejected existing downloaded document: "
        f"document_id={doc_id}, filename={filename!r}, "
        f"content_type={mime_type!r}, storage_path={storage_path!r}, reason={reason}"
    )
    record_processing_event(tender_id, "document_download_rejected", message)


def document_already_downloaded(tender_id: str, doc_id: str | int) -> bool:
    existing = existing_document_info(tender_id, doc_id)
    if not existing:
        return False

    storage_path = existing.get("storage_path")
    size_bytes = existing.get("size_bytes") or 0

    if not storage_path or size_bytes <= 0:
        return False

    file_path = APP_DIR / storage_path
    if not file_path.exists() or file_path.stat().st_size <= 0:
        return False

    with file_path.open("rb") as fh:
        content_head = fh.read(2048)

    is_valid, detected_type, reason = validate_downloaded_document(
        content=content_head,
        filename=existing.get("filename") or file_path.name,
        content_type=existing.get("mime_type"),
    )

    if is_valid:
        return True

    logger.warning(
        "Invalid existing document removed: tender_id=%s document_id=%s "
        "filename=%r content_type=%r storage_path=%r detected_type=%r reason=%s",
        tender_id,
        doc_id,
        existing.get("filename"),
        existing.get("mime_type"),
        storage_path,
        detected_type,
        reason,
    )
    cleanup_invalid_existing_document(tender_id, doc_id, existing, reason)
    return False


def safe_filename(value: str | None, fallback: str) -> str:
    if not value:
        value = fallback

    value = unquote(value)
    value = value.replace("\n", " ").replace("\r", " ").strip()
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"\s+", " ", value)

    return value[:180] or fallback


def filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None

    # filename*=UTF-8''file.docx
    match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.IGNORECASE)
    if match:
        return unquote(match.group(1).strip().strip('"'))

    # filename="file.docx"
    match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    return None


def extension_from_content_type(content_type: str | None) -> str:
    if not content_type:
        return ""

    content_type = content_type.lower()

    mapping = {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/vnd.rar": ".rar",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }

    for key, ext in mapping.items():
        if key in content_type:
            return ext

    return ""


def get_candidate_tenders(
    limit_tenders: int,
    *,
    min_price: float | None = None,
    min_days_left: int | None = None,
    include_renewals: bool = False,
    include_service_noise: bool = False,
) -> list[dict]:
    filters = [
        "t.source = 'zakupki360'",
        "a.analysis_type = 'rule_based_score'",
        "a.recommendation IN ('go', 'maybe')",
    ]
    params: list[object] = []

    if min_price is not None and min_price > 0:
        filters.append("(t.initial_price IS NULL OR t.initial_price >= %s)")
        params.append(min_price)

    if min_days_left is not None and min_days_left > 0:
        min_deadline = datetime.now(timezone.utc) + timedelta(days=min_days_left)
        filters.append("(t.deadline_at IS NULL OR t.deadline_at >= %s)")
        params.append(min_deadline)

    where_sql = " AND ".join(filters)

    params.append(limit_tenders * 4)

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    t.id,
                    t.source,
                    t.external_id,
                    t.title,
                    t.initial_price,
                    t.deadline_at,
                    t.raw,
                    a.score,
                    a.recommendation,
                    a.result
                FROM tenders t
                JOIN analysis_results a ON a.tender_id = t.id
                WHERE {where_sql}
                ORDER BY a.score DESC, t.deadline_at ASC NULLS LAST
                LIMIT %s;
                """,
                params,
            )

            rows = cur.fetchall()

    result = []
    skipped_renewals = 0
    skipped_service_noise = 0
    skipped_no_docs = 0

    for row in rows:
        if not include_renewals and is_incumbent_extension(row):
            skipped_renewals += 1
            continue

        if not include_service_noise and is_service_noise(row):
            skipped_service_noise += 1
            continue

        raw = row.get("raw") or {}
        full = raw.get("full") or {}
        docs = full.get("documents") or []

        if not docs:
            skipped_no_docs += 1
            continue

        result.append(row)

        if len(result) >= limit_tenders:
            break

    print(
        "Candidate prefilter: "
        f"selected={len(result)}, "
        f"skipped_renewals={skipped_renewals}, "
        f"skipped_service_noise={skipped_service_noise}, "
        f"skipped_no_docs={skipped_no_docs}"
    )

    return result


def get_target_tender(
    *,
    external_id: str | None = None,
    tender_id: str | None = None,
) -> dict | None:
    filters = ["t.source = 'zakupki360'"]
    params: list[object] = []

    if external_id:
        filters.append("t.external_id = %s")
        params.append(external_id)

    if tender_id:
        filters.append("t.id = %s")
        params.append(tender_id)

    where_sql = " AND ".join(filters)

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT
                    t.id,
                    t.source,
                    t.external_id,
                    t.title,
                    t.initial_price,
                    t.deadline_at,
                    t.raw
                FROM tenders t
                WHERE {where_sql}
                LIMIT 1;
                """,
                params,
            )
            return cur.fetchone()


def tender_documents(tender: dict) -> list[dict]:
    raw = tender.get("raw") or {}
    full = raw.get("full") or {}
    docs = full.get("documents") or []

    return [doc for doc in docs if isinstance(doc, dict)]


def merge_tender_priority_context(
    tender: dict,
    tender_context: dict | None,
) -> dict:
    if not tender_context:
        return tender

    merged = dict(tender)
    for key in TENDER_PRIORITY_CONTEXT_KEYS:
        value = tender_context.get(key)
        if value not in (None, ""):
            merged[key] = value

    if not merged.get("raw") and tender_context.get("raw"):
        merged["raw"] = tender_context["raw"]

    return merged


def print_document_inventory(docs: list[dict]) -> None:
    print(f"Documents in API: {len(docs)}")

    for idx, doc in enumerate(docs, start=1):
        doc_id = doc.get("id") or "-"
        title = doc.get("title") or "-"
        print(f"  {idx}. id={doc_id} title={title}")


def normalize_document_priority_text(value: str | None) -> str:
    return str(value or "").casefold().replace("\u0451", "\u0435")


def document_priority_text(doc: dict) -> str:
    return " ".join(
        str(doc.get(key) or "")
        for key in ("title", "filename", "fileName", "name")
    )


def document_has_primary_technical_hint(normalized: str) -> bool:
    compact = re.sub(r"[^0-9a-zа-я]+", "", normalized)
    return bool(
        TZ_ABBREV_RE.search(normalized)
        or "техзад" in compact
        or ("техническ" in normalized and "задан" in normalized)
        or any(pattern in normalized for pattern in PRIMARY_TECHNICAL_DOCUMENT_PATTERNS)
    )


def document_has_explicit_technical_hint(normalized: str) -> bool:
    return document_has_primary_technical_hint(normalized)


def document_is_primary_technical_document(doc: dict) -> bool:
    return document_has_primary_technical_hint(
        normalize_document_priority_text(document_priority_text(doc))
    )


def document_has_procurement_material_hint(normalized: str) -> bool:
    return bool(
        KD_ABBREV_RE.search(normalized)
        or any(pattern in normalized for pattern in PROCUREMENT_MATERIAL_PATTERNS)
    )


def text_has_strong_hardware_hint(normalized: str) -> bool:
    return bool(
        PC_ABBREV_RE.search(normalized)
        or any(pattern in normalized for pattern in STRONG_HARDWARE_DOCUMENT_PATTERNS)
    )


def text_has_broad_hardware_hint(normalized: str) -> bool:
    return any(pattern in normalized for pattern in BROAD_HARDWARE_DOCUMENT_PATTERNS)


def text_has_target_hardware_category(normalized: str) -> bool:
    return any(value in normalized for value in TARGET_HARDWARE_CATEGORY_VALUES)


def tender_priority_text(tender: dict | None) -> str:
    if not tender:
        return ""

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
            "summary",
            "detected_category",
            "category",
            "market_access",
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
        for key in ("orderName", "purchaseName", "subject", "deliveryPlace"):
            value = full.get(key)
            if value:
                chunks.append(str(value))

        for doc in full.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            chunks.append(document_priority_text(doc))

    return "\n".join(chunks)


def tender_has_target_hardware_context(tender: dict | None) -> bool:
    normalized = normalize_document_priority_text(tender_priority_text(tender))
    if not normalized:
        return False

    if text_has_target_hardware_category(normalized):
        return True

    if text_has_strong_hardware_hint(normalized):
        return True

    try:
        from app.business_rules import business_assessment

        assessment = business_assessment(tender or {})
    except Exception:
        assessment = {}

    if not isinstance(assessment, dict):
        return False

    return str(assessment.get("market_access") or "").strip() in {
        "target_hardware",
        "infra_project",
    }


def document_priority(doc: dict, tender: dict | None = None) -> int:
    text = document_priority_text(doc)
    normalized = normalize_document_priority_text(text)

    if document_has_primary_technical_hint(normalized):
        return 0

    procurement_material = document_has_procurement_material_hint(normalized)
    strong_hardware_hint = text_has_strong_hardware_hint(normalized)
    broad_hardware_hint = text_has_broad_hardware_hint(normalized)
    target_hardware_tender = tender_has_target_hardware_context(tender)

    if procurement_material:
        return 1

    if any(pattern in normalized for pattern in PRICE_DOCUMENT_PATTERNS):
        return 2

    if any(pattern in normalized for pattern in APPLICATION_REQUIREMENTS_PATTERNS):
        return 3

    if any(pattern in normalized for pattern in CONTRACT_DOCUMENT_PATTERNS):
        return 4

    if any(pattern in normalized for pattern in NOTICE_DOCUMENT_PATTERNS):
        return 5

    if (
        strong_hardware_hint
        and broad_hardware_hint
        and target_hardware_tender
    ):
        return 6

    return 9


def prioritize_documents_for_download(
    docs: list[dict],
    tender: dict | None = None,
) -> list[dict]:
    return [
        doc
        for _, _, doc in sorted(
            (document_priority(doc, tender=tender), idx, doc)
            for idx, doc in enumerate(docs)
        )
    ]


def document_selection_ref(
    doc: dict,
    tender: dict | None = None,
) -> dict[str, Any]:
    return document_selection_ref_with_annotations(doc, tender=tender)


def document_selection_ref_with_annotations(
    doc: dict,
    tender: dict | None = None,
    *,
    planner_annotations: dict[str, dict[str, Any]] | None = None,
    all_docs: list[dict] | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "document_id": doc.get("id"),
        "title": doc.get("title"),
        "priority": document_priority(doc, tender=tender),
    }
    try:
        from app.llm.document_download_planner import document_annotation

        annotation = document_annotation(
            doc,
            all_docs=all_docs or [],
            plan_annotations=planner_annotations or {},
        )
    except Exception:
        annotation = {}

    expected_role = str(annotation.get("expected_role") or "").strip()
    if expected_role:
        ref["expected_role"] = expected_role
    if annotation.get("planner_bucket"):
        ref["planner_bucket"] = annotation.get("planner_bucket")
    if annotation.get("planner_priority") is not None:
        ref["planner_priority"] = annotation.get("planner_priority")
    if annotation.get("planner_reason"):
        ref["planner_reason"] = annotation.get("planner_reason")
    if annotation.get("high_value_technical"):
        ref["high_value_technical"] = True
    return ref


def document_rate_limit_ref(
    doc: dict,
    tender: dict | None = None,
    *,
    planner_annotations: dict[str, dict[str, Any]] | None = None,
    all_docs: list[dict] | None = None,
) -> dict[str, Any]:
    return document_selection_ref_with_annotations(
        doc,
        tender=tender,
        planner_annotations=planner_annotations,
        all_docs=all_docs,
    )


def document_failed_ref(
    doc: dict,
    tender: dict | None = None,
    *,
    reason: str,
    planner_annotations: dict[str, dict[str, Any]] | None = None,
    all_docs: list[dict] | None = None,
) -> dict[str, Any]:
    ref = document_selection_ref_with_annotations(
        doc,
        tender=tender,
        planner_annotations=planner_annotations,
        all_docs=all_docs,
    )
    ref["reason"] = reason
    return ref


def parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None

    try:
        seconds = float(value)
    except ValueError:
        return None

    if seconds <= 0:
        return None

    return seconds


def targeted_rate_limit_backoff_seconds(
    exc: Exception,
    *,
    document_rate_limit_seconds: float,
) -> float | None:
    retry_after = None
    if is_rate_limit_error(exc):
        retry_after = parse_retry_after_seconds(exc.response.headers.get("retry-after"))

    if retry_after is not None:
        if retry_after > TARGETED_RATE_LIMIT_BACKOFF_MAX_SECONDS:
            return None
        return max(document_rate_limit_seconds, retry_after)

    return min(
        TARGETED_RATE_LIMIT_BACKOFF_MAX_SECONDS,
        max(TARGETED_RATE_LIMIT_BACKOFF_MIN_SECONDS, document_rate_limit_seconds * 2),
    )


def add_rate_limited_documents(
    result: TargetedDownloadResult,
    docs: list[dict],
    *,
    tender: dict | None = None,
    planner_annotations: dict[str, dict[str, Any]] | None = None,
    all_docs: list[dict] | None = None,
) -> None:
    seen = {
        str(item.get("document_id"))
        for item in result.documents_skipped_due_to_rate_limit_items
    }
    for doc in docs:
        doc_id = str(doc.get("id") or "")
        if doc_id in seen:
            continue
        result.documents_skipped_due_to_rate_limit_items.append(
            document_rate_limit_ref(
                doc,
                tender=tender,
                planner_annotations=planner_annotations,
                all_docs=all_docs,
            )
        )
        seen.add(doc_id)

    result.documents_skipped_due_to_rate_limit = len(
        result.documents_skipped_due_to_rate_limit_items
    )
    for item in result.documents_skipped_due_to_rate_limit_items:
        if not item.get("high_value_technical") and item.get("expected_role") not in {
            "technical_requirements",
            "specification",
            "object_description",
        }:
            continue
        result.missing_high_value_technical_document = True
        if not result.missing_high_value_technical_document_title:
            result.missing_high_value_technical_document_title = str(
                item.get("title") or item.get("document_id") or ""
            ).strip()
        break


def add_failed_document(
    result: TargetedDownloadResult,
    doc: dict,
    *,
    reason: str,
    tender: dict | None = None,
    planner_annotations: dict[str, dict[str, Any]] | None = None,
    all_docs: list[dict] | None = None,
) -> None:
    doc_id = str(doc.get("id") or "")
    seen = {
        str(item.get("document_id"))
        for item in result.documents_failed_items
    }
    if doc_id in seen:
        return
    result.documents_failed_items.append(
        document_failed_ref(
            doc,
            tender=tender,
            reason=reason,
            planner_annotations=planner_annotations,
            all_docs=all_docs,
        )
    )
    result.documents_failed = len(result.documents_failed_items)


def save_document_record(
    tender_id: str,
    document_url: str,
    filename: str,
    mime_type: str | None,
    size_bytes: int,
    storage_path: str,
) -> None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id
                FROM documents
                WHERE tender_id = %s
                  AND url = %s
                LIMIT 1;
                """,
                (tender_id, document_url),
            )
            existing = cur.fetchone()

            if existing:
                cur.execute(
                    """
                    UPDATE documents
                    SET
                        filename = %s,
                        mime_type = %s,
                        size_bytes = %s,
                        storage_path = %s
                    WHERE id = %s;
                    """,
                    (
                        filename,
                        mime_type,
                        size_bytes,
                        storage_path,
                        existing["id"],
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO documents (
                        tender_id,
                        filename,
                        url,
                        mime_type,
                        size_bytes,
                        storage_path
                    )
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """,
                    (
                        tender_id,
                        filename,
                        document_url,
                        mime_type,
                        size_bytes,
                        storage_path,
                    ),
                )

        conn.commit()


def download_document(
    client: Zakupki360Client,
    tender: dict,
    doc: dict,
    *,
    document_rate_limit_seconds: float,
    force_redownload: bool = False,
    stop_on_marketplace_auth_required: bool = False,
) -> bool:
    doc_id = doc.get("id")
    if not doc_id:
        record_processing_event(
            str(tender["id"]),
            "document_download_failed",
            (
                "Document download skipped: "
                f"external_id={tender.get('external_id')}, "
                "document_id=missing, reason=missing_document_id"
            ),
        )
        return False

    external_id = tender["external_id"]
    tender_dir = DATA_DIR / f"zakupki360_{external_id}"
    tender_dir.mkdir(parents=True, exist_ok=True)

    existing = existing_document_info(str(tender["id"]), doc_id)

    if force_redownload:
        print(
            f"Force redownload document {doc_id} for tender {external_id}: "
            f"{doc.get('title')}"
        )
    elif document_already_downloaded(str(tender["id"]), doc_id):
        print(f"Skip existing document {doc_id} for tender {external_id}: {doc.get('title')}")
        return False

    print(f"Downloading document {doc_id} for tender {external_id}: {doc.get('title')}")

    response = client.request(
        "GET",
        f"/api/orders/document/{doc_id}",
        rate_limit_seconds=document_rate_limit_seconds,
    )
    download_source = "z360"

    if response.status_code == 204:
        direct_response = download_direct_document(
            doc,
            document_rate_limit_seconds=document_rate_limit_seconds,
        )
        if direct_response is not None:
            print(
                "Z360 document endpoint returned no content; "
                f"using direct source URL for document {doc_id}"
            )
            response = direct_response
            download_source = "direct_url"
        else:
            message = (
                "Document download returned no content: "
                f"external_id={external_id}, document_id={doc_id}, "
                "status_code=204, reason=no_content"
            )
            print(f"Document {doc_id}: no content")
            record_processing_event(str(tender["id"]), "document_download_failed", message)
            return False

    content = response.content
    if not content and download_source != "direct_url":
        direct_response = download_direct_document(
            doc,
            document_rate_limit_seconds=document_rate_limit_seconds,
        )
        if direct_response is not None:
            print(
                "Z360 document endpoint returned empty response; "
                f"using direct source URL for document {doc_id}"
            )
            response = direct_response
            download_source = "direct_url"
            content = response.content

    if not content:
        message = (
            "Document download returned empty response: "
            f"external_id={external_id}, document_id={doc_id}, "
            f"status_code={response.status_code}, reason=empty_response, "
            f"download_source={download_source}"
        )
        print(f"Document {doc_id}: empty response")
        record_processing_event(str(tender["id"]), "document_download_failed", message)
        return False

    content_type = response.headers.get("content-type")
    cd_filename = filename_from_content_disposition(
        response.headers.get("content-disposition")
    )

    fallback_ext = extension_from_content_type(content_type)
    fallback_name = f"{doc_id}_{doc.get('title') or 'document'}{fallback_ext}"

    filename = safe_filename(cd_filename or doc.get("title"), fallback_name)

    if "." not in Path(filename).name and fallback_ext:
        filename = f"{filename}{fallback_ext}"

    filename = safe_filename(f"{doc_id}_{filename}", f"{doc_id}_document{fallback_ext}")
    file_path = tender_dir / filename

    is_valid, detected_type, reason = validate_downloaded_document(
        content=content,
        filename=filename,
        content_type=content_type,
    )

    if not is_valid:
        html_diagnostics = None
        if detected_type == "html" or reason == "html_response":
            html_diagnostics = extract_html_diagnostics(content, content_type)

        message = (
            "Rejected document download: "
            f"external_id={external_id}, document_id={doc_id}, "
            f"filename={filename!r}, content_type={content_type!r}, "
            f"detected_type={detected_type!r}, reason={reason}, "
            f"download_source={download_source}"
        )

        if html_diagnostics:
            message = (
                f"{message}, html_hint={html_diagnostics.hint!r}, "
                f"html_title={html_diagnostics.title!r}, "
                f"html_preview={html_diagnostics.preview!r}, "
                f"html_form_action={html_diagnostics.form_action!r}, "
                f"html_meta_refresh={html_diagnostics.meta_refresh!r}, "
                f"html_redirect_hint={html_diagnostics.redirect_hint!r}, "
                f"response_url={str(response.url)!r}"
            )
            logger.warning(
                "Rejected document download: tender_id=%s external_id=%s document_id=%s "
                "filename=%r content_type=%r detected_type=%r reason=%s "
                "html_hint=%r html_title=%r html_preview=%r html_form_action=%r "
                "html_meta_refresh=%r html_redirect_hint=%r response_url=%r",
                tender["id"],
                external_id,
                doc_id,
                filename,
                content_type,
                detected_type,
                reason,
                html_diagnostics.hint,
                html_diagnostics.title,
                html_diagnostics.preview,
                html_diagnostics.form_action,
                html_diagnostics.meta_refresh,
                html_diagnostics.redirect_hint,
                str(response.url),
            )
        else:
            logger.warning(
                "Rejected document download: tender_id=%s external_id=%s document_id=%s "
                "filename=%r content_type=%r detected_type=%r reason=%s",
                tender["id"],
                external_id,
                doc_id,
                filename,
                content_type,
                detected_type,
                reason,
            )

        record_processing_event(
            str(tender["id"]),
            "document_download_rejected",
            message,
        )
        if (
            stop_on_marketplace_auth_required
            and html_diagnostics
            and html_diagnostics.hint == EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT
        ):
            raise MarketplaceAuthRequiredError(
                "documents require external marketplace authorization"
            )

        return False

    if not Path(filename).suffix:
        detected_ext = extension_for_detected_file_type(detected_type)
        if detected_ext:
            filename = safe_filename(
                f"{filename}{detected_ext}",
                f"{doc_id}_document{detected_ext}",
            )
            file_path = tender_dir / filename

    file_path.write_bytes(content)

    storage_path = str(file_path.relative_to(APP_DIR))

    save_document_record(
        tender_id=str(tender["id"]),
        document_url=document_url(doc_id),
        filename=filename,
        mime_type=content_type,
        size_bytes=len(content),
        storage_path=storage_path,
    )
    remove_replaced_document_file(existing, file_path)

    print(
        f"Saved: {storage_path} "
        f"({len(content)} bytes, {content_type}, detected={detected_type}, "
        f"source={download_source})"
    )
    return True


def run_targeted_download_report(
    client: Zakupki360Client,
    *,
    external_id: str | None = None,
    tender_id: str | None = None,
    tender_context: dict | None = None,
    limit_docs: int,
    document_rate_limit_seconds: float,
    force_redownload: bool = False,
    use_llm_planner: bool = False,
) -> TargetedDownloadResult:
    tender = get_target_tender(external_id=external_id, tender_id=tender_id)
    if not tender:
        selector = f"external_id={external_id}" if external_id else f"tender_id={tender_id}"
        raise RuntimeError(f"Target tender not found: {selector}")

    priority_tender = merge_tender_priority_context(tender, tender_context)
    docs = tender_documents(tender)
    result = TargetedDownloadResult(documents_found=len(docs))

    print("Targeted document download mode")
    print(f"Tender {tender['external_id']}: {tender['title']}")
    print_document_inventory(docs)

    if not docs:
        return result

    deterministic_docs = prioritize_documents_for_download(
        docs,
        tender=priority_tender,
    )
    ordered_docs = deterministic_docs
    planner_annotations: dict[str, dict[str, Any]] = {}

    if use_llm_planner:
        try:
            from app.llm.document_download_planner import (
                merge_planner_document_order,
                plan_document_download,
            )

            planner_plan = plan_document_download(priority_tender, docs)
            result.document_download_planner_used = bool(
                planner_plan.get("planner_used")
            )
            result.document_download_planner_confidence = str(
                planner_plan.get("confidence") or "low"
            )
            if planner_plan.get("planner_error"):
                result.document_download_planner_error = str(
                    planner_plan.get("planner_error") or ""
                )
            ordered_docs, planner_annotations = merge_planner_document_order(
                docs,
                planner_plan,
                deterministic_docs,
                limit_docs=limit_docs,
            )
        except Exception as exc:
            result.document_download_planner_used = False
            result.document_download_planner_confidence = "low"
            result.document_download_planner_error = str(exc)
            ordered_docs = deterministic_docs
            planner_annotations = {}

    selected_docs = ordered_docs[:limit_docs]
    result.documents_selected = len(selected_docs)
    result.documents_selected_items = [
        document_selection_ref_with_annotations(
            doc,
            tender=priority_tender,
            planner_annotations=planner_annotations,
            all_docs=docs,
        )
        for doc in selected_docs
    ]
    if use_llm_planner:
        print(
            "Document download planner: "
            f"used={str(result.document_download_planner_used).lower()} "
            f"confidence={result.document_download_planner_confidence or 'unknown'}"
        )
        if result.document_download_planner_error:
            print(
                "Document download planner fallback: "
                f"error={result.document_download_planner_error}"
            )
        planned_preview = "; ".join(
            f"{item.get('document_id') or '-'}:{item.get('title') or '-'}"
            for item in result.documents_selected_items[:5]
        )
        if planned_preview:
            print(f"Document download plan selected: {planned_preview}")
    print(f"Documents selected for download: {len(selected_docs)}")

    for idx, doc in enumerate(selected_docs):
        while True:
            try:
                saved = download_document(
                    client,
                    tender,
                    doc,
                    document_rate_limit_seconds=document_rate_limit_seconds,
                    force_redownload=force_redownload,
                    stop_on_marketplace_auth_required=True,
                )
                if saved:
                    result.total_saved += 1
                    result.documents_downloaded = result.total_saved
                break
            except MarketplaceAuthRequiredError:
                add_failed_document(
                    result,
                    doc,
                    reason="marketplace_auth_required",
                    tender=priority_tender,
                    planner_annotations=planner_annotations,
                    all_docs=docs,
                )
                print(
                    "Stopping targeted download: documents require marketplace authorization "
                    f"external_id={tender['external_id']} document_id={doc.get('id')} "
                    f"html_hint={EXTERNAL_MARKETPLACE_AUTH_REQUIRED_HINT}"
                )
                return result
            except Exception as exc:
                if not is_rate_limit_error(exc):
                    reason = f"exception:{exc.__class__.__name__}"
                    add_failed_document(
                        result,
                        doc,
                        reason=reason,
                        tender=priority_tender,
                        planner_annotations=planner_annotations,
                        all_docs=docs,
                    )
                    message = (
                        "Document download failed: "
                        f"external_id={tender['external_id']} "
                        f"document_id={doc.get('id')} "
                        f"reason={reason} error={exc}"
                    )
                    print(f"Failed document {doc.get('id')}: {exc}")
                    record_processing_event(
                        str(tender["id"]),
                        "document_download_failed",
                        message,
                    )
                    break

                backoff_seconds = targeted_rate_limit_backoff_seconds(
                    exc,
                    document_rate_limit_seconds=document_rate_limit_seconds,
                )
                if (
                    result.rate_limit_retries < TARGETED_RATE_LIMIT_MAX_RETRIES
                    and backoff_seconds is not None
                ):
                    result.rate_limit_retries += 1
                    result.rate_limit_backoffs_seconds.append(backoff_seconds)
                    print(
                        "Targeted download hit 429; retrying once: "
                        f"document_id={doc.get('id')} "
                        f"retry={result.rate_limit_retries}/"
                        f"{TARGETED_RATE_LIMIT_MAX_RETRIES} "
                        f"backoff_seconds={backoff_seconds:.1f}"
                    )
                    logger.warning(
                        "Targeted document download hit 429; retrying: "
                        "external_id=%s document_id=%s retry=%s/%s "
                        "backoff_seconds=%.1f",
                        tender["external_id"],
                        doc.get("id"),
                        result.rate_limit_retries,
                        TARGETED_RATE_LIMIT_MAX_RETRIES,
                        backoff_seconds,
                    )
                    time.sleep(backoff_seconds)
                    continue

                if backoff_seconds is None:
                    print(
                        "Targeted download hit 429; not retrying because "
                        "Retry-After exceeds targeted cap: "
                        f"document_id={doc.get('id')} "
                        "max_backoff_seconds="
                        f"{TARGETED_RATE_LIMIT_BACKOFF_MAX_SECONDS:.1f}"
                    )
                    logger.warning(
                        "Targeted document download hit 429; Retry-After exceeds "
                        "targeted cap: external_id=%s document_id=%s "
                        "max_backoff_seconds=%.1f",
                        tender["external_id"],
                        doc.get("id"),
                        TARGETED_RATE_LIMIT_BACKOFF_MAX_SECONDS,
                    )

                result.stopped_due_to_rate_limit = True
                add_rate_limited_documents(
                    result,
                    selected_docs[idx:],
                    tender=priority_tender,
                    planner_annotations=planner_annotations,
                    all_docs=docs,
                )
                skipped_refs = result.documents_skipped_due_to_rate_limit_items
                message = (
                    "Stopping targeted download after 429 to protect API quota: "
                    f"external_id={tender['external_id']} "
                    f"document_id={doc.get('id')} "
                    f"rate_limit_retries={result.rate_limit_retries} "
                    f"backoffs={result.rate_limit_backoffs_seconds} "
                    "documents_skipped_due_to_rate_limit="
                    f"{result.documents_skipped_due_to_rate_limit} "
                    f"skipped_documents={skipped_refs}"
                )
                print(message)
                logger.warning(message)
                record_processing_event(
                    str(tender["id"]),
                    "document_download_rate_limited",
                    message,
                )
                return result

    if result.rate_limit_retries or result.documents_skipped_due_to_rate_limit:
        print(
            "Targeted rate-limit summary: "
            f"retries={result.rate_limit_retries}, "
            f"backoffs={result.rate_limit_backoffs_seconds}, "
            "documents_skipped_due_to_rate_limit="
            f"{result.documents_skipped_due_to_rate_limit}, "
            "skipped_documents="
            f"{result.documents_skipped_due_to_rate_limit_items}"
        )
    if result.documents_failed:
        print(
            "Targeted failed documents: "
            f"documents_failed={result.documents_failed}, "
            f"failed_documents={result.documents_failed_items}"
        )

    return result


def run_targeted_download(
    client: Zakupki360Client,
    *,
    external_id: str | None = None,
    tender_id: str | None = None,
    tender_context: dict | None = None,
    limit_docs: int,
    document_rate_limit_seconds: float,
    force_redownload: bool = False,
    use_llm_planner: bool = False,
) -> int:
    result = run_targeted_download_report(
        client,
        external_id=external_id,
        tender_id=tender_id,
        tender_context=tender_context,
        limit_docs=limit_docs,
        document_rate_limit_seconds=document_rate_limit_seconds,
        force_redownload=force_redownload,
        use_llm_planner=use_llm_planner,
    )
    return result.total_saved


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s",
    )

    parser = argparse.ArgumentParser(description="Download documents for tenders")
    parser.add_argument("--limit-tenders", type=int, default=3)
    parser.add_argument("--limit-docs", type=int, default=5)
    parser.add_argument("--min-price", type=float, default=3_000_000)
    parser.add_argument("--min-days-left", type=int, default=2)
    parser.add_argument("--include-renewals", action="store_true")
    parser.add_argument("--include-service-noise", action="store_true")
    parser.add_argument("--external-id", help="Download documents for one tender external_id")
    parser.add_argument("--tender-id", help="Download documents for one tender UUID")
    parser.add_argument(
        "--force-redownload",
        action="store_true",
        help="Force replacing existing downloaded documents in targeted mode",
    )
    args = parser.parse_args()

    targeted = bool(args.external_id or args.tender_id)
    if args.external_id and args.tender_id:
        parser.error("Use either --external-id or --tender-id, not both")

    if args.force_redownload and not targeted:
        parser.error("--force-redownload can only be used with --external-id or --tender-id")

    client = Zakupki360Client()
    client.login()

    document_rate_limit_seconds = get_document_rate_limit_seconds()
    print(f"Using document API rate limit: {document_rate_limit_seconds} seconds")

    if targeted:
        total_saved = run_targeted_download(
            client,
            external_id=args.external_id,
            tender_id=args.tender_id,
            limit_docs=args.limit_docs,
            document_rate_limit_seconds=document_rate_limit_seconds,
            force_redownload=args.force_redownload,
        )
        print()
        print(f"Downloaded documents: {total_saved}")
        return

    tenders = get_candidate_tenders(
        limit_tenders=args.limit_tenders,
        min_price=args.min_price,
        min_days_left=args.min_days_left,
        include_renewals=args.include_renewals,
        include_service_noise=args.include_service_noise,
    )

    print(f"Candidate tenders: {len(tenders)}")

    total_saved = 0

    for tender in tenders:
        raw = tender.get("raw") or {}
        full = raw.get("full") or {}
        docs = full.get("documents") or []

        if not docs:
            print(f"No documents for tender {tender['external_id']}")
            continue

        print()
        print(f"Tender {tender['external_id']}: {tender['title']}")
        print(f"Documents in API: {len(docs)}")

        for doc in docs[: args.limit_docs]:
            try:
                if download_document(
                    client,
                    tender,
                    doc,
                    document_rate_limit_seconds=document_rate_limit_seconds,
                ):
                    total_saved += 1
            except Exception as exc:
                print(f"Failed document {doc.get('id')}: {exc}")

    print()
    print(f"Downloaded documents: {total_saved}")


if __name__ == "__main__":
    main()
