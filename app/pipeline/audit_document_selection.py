from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.document_quality import (
    DOCUMENT_CONFIDENCE_HIGH,
    DOCUMENT_CONFIDENCE_LOW,
    DOCUMENT_CONFIDENCE_MEDIUM,
    DOCUMENT_SOURCE_CARD_ONLY,
    DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
    DOCUMENT_SOURCE_MISSING,
    DOCUMENT_SOURCE_PRICING_OR_BOM,
    DOCUMENT_SOURCE_UNKNOWN,
    classify_primary_document_quality,
)
from app.document_status import (
    DOCUMENTS_BLOCKED_CARD_ONLY,
    DOCUMENTS_MISSING_CARD_ONLY,
    LEAD_CARD_ONLY_BASIS,
    build_document_status,
)


OUTPUT_FIELDS = (
    "external_id",
    "title",
    "customer_name",
    "initial_price",
    "has_full_lead_report",
    "latest_report_created_at",
    "latest_report_card_only",
    "stale_card_only_refresh_candidate",
    "docs_count",
    "docs_with_text_count",
    "source_documents_count",
    "document_inventory_state",
    "document_preparation_status",
    "documents_found",
    "documents_selected",
    "documents_failed",
    "document_state",
    "selector_status",
    "primary_document_title",
    "primary_section_hint",
    "primary_document_source_kind",
    "primary_document_confidence",
    "primary_document_quality_reason",
    "readiness_warning",
    "suggested_action",
)

LEAD_LLM_REPORT_ANALYSIS_TYPE = "llm_customer_lead_report"
CARD_ONLY_DOCUMENT_STATUS_CODES = {
    DOCUMENTS_BLOCKED_CARD_ONLY,
    DOCUMENTS_MISSING_CARD_ONLY,
}


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _as_dict(value: Any) -> dict[str, Any]:
    value = _json_value(value)
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    value = _json_value(value)
    return value if isinstance(value, list) else []


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _truthy_json_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _created_at_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _documents_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for item in _as_list(row.get("documents")):
        if not isinstance(item, dict):
            continue
        docs.append(
            {
                "id": item.get("id"),
                "title": item.get("title") or item.get("filename"),
                "filename": item.get("filename") or item.get("title"),
                "mime_type": item.get("mime_type"),
                "extracted_text": item.get("extracted_text") or "",
            }
        )
    return docs


def _source_documents_count(row: dict[str, Any]) -> int:
    raw = _as_dict(row.get("raw"))
    full = _as_dict(raw.get("full"))
    return len([item for item in _as_list(full.get("documents")) if isinstance(item, dict)])


def _report_and_meta(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _as_dict(row.get("llm_customer_lead_report_result"))
    report = _as_dict(result.get("report")) or result
    meta = _as_dict(result.get("meta"))
    return report, meta


def _report_document_status(row: dict[str, Any]) -> dict[str, Any]:
    report, meta = _report_and_meta(row)
    return _as_dict(report.get("document_status")) or _as_dict(
        meta.get("document_status")
    )


def latest_report_is_card_only(row: dict[str, Any]) -> bool:
    result = _as_dict(row.get("llm_customer_lead_report_result"))
    if not result:
        return False
    report, meta = _report_and_meta(row)
    status = _report_document_status(row)
    analysis_basis = str(
        status.get("analysis_basis")
        or report.get("analysis_basis")
        or result.get("analysis_basis")
        or ""
    )
    if analysis_basis == LEAD_CARD_ONLY_BASIS:
        return True
    if str(status.get("code") or "") in CARD_ONLY_DOCUMENT_STATUS_CODES:
        return True
    if str(meta.get("lead_context_mode") or "") == "card_only":
        return True
    if _truthy_json_bool(meta.get("lead_documents_missing_allowed")):
        return True
    if _truthy_json_bool(meta.get("card_only")):
        return True
    return str(meta.get("context_source") or "") == "card_only"


def stale_card_only_refresh_candidate(row: dict[str, Any]) -> bool:
    return (
        latest_report_is_card_only(row)
        and int(row.get("docs_with_text_count") or 0) > 0
    )


def _document_preparation_from_row(row: dict[str, Any]) -> dict[str, Any]:
    report, meta = _report_and_meta(row)
    preparation = _as_dict(meta.get("document_preparation"))
    if preparation:
        return preparation
    status = _as_dict(report.get("document_status")) or _as_dict(
        meta.get("document_status")
    )
    return _as_dict(status.get("document_preparation"))


def _document_inventory_state(
    *,
    docs_count: int,
    docs_with_text_count: int,
    source_documents_count: int,
    documents_found: int,
) -> str:
    if docs_count <= 0:
        if source_documents_count > 0 or documents_found > 0:
            return "source_inventory_not_downloaded"
        return "source_no_document_inventory"
    if docs_with_text_count <= 0:
        return "downloaded_no_text"
    return "downloaded_with_text"


def _selection_from_report_or_documents(
    row: dict[str, Any],
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    from app.llm.document_selector import (
        build_document_candidates,
        deterministic_document_selection,
    )

    report, meta = _report_and_meta(row)
    status = _as_dict(report.get("document_status")) or _as_dict(
        meta.get("document_status")
    )
    selection = _as_dict(meta.get("document_selection"))
    if not selection and status:
        selection = {
            "primary_document_id": status.get("primary_document_id"),
            "primary_document_title": status.get("primary_document_title"),
            "primary_section_hint": status.get("primary_section_hint"),
            "primary_document_confidence": status.get("primary_document_confidence"),
            "primary_document_source_kind": status.get("primary_document_source_kind"),
            "primary_document_quality_reason": status.get(
                "primary_document_quality_reason"
            ),
            "primary_document_is_full_technical_spec": status.get(
                "primary_document_is_full_technical_spec"
            ),
            "selector_status": status.get("selector_status"),
        }

    if selection and selection.get("primary_document_source_kind"):
        return selection

    candidates = build_document_candidates(
        documents,
        tender={
            "title": row.get("title"),
            "customer_name": row.get("customer_name"),
            "initial_price": row.get("initial_price"),
        },
    )
    if selection:
        primary_id = str(selection.get("primary_document_id") or "").strip()
        primary = next(
            (candidate for candidate in candidates if str(candidate.get("id")) == primary_id),
            None,
        )
        quality = classify_primary_document_quality(
            selection,
            primary_candidate=primary,
            docs_count=len(documents),
            docs_with_text_count=sum(
                1 for doc in documents if str(doc.get("extracted_text") or "").strip()
            ),
        )
        return {**selection, **quality}

    return deterministic_document_selection(candidates)


def _document_status_for_row(
    row: dict[str, Any],
    documents: list[dict[str, Any]],
    selection: dict[str, Any],
) -> dict[str, Any]:
    report, meta = _report_and_meta(row)
    status = _as_dict(report.get("document_status")) or _as_dict(
        meta.get("document_status")
    )
    docs_count = int(row.get("docs_count") or len(documents) or 0)
    docs_with_text_count = int(
        row.get("docs_with_text_count")
        or sum(1 for doc in documents if str(doc.get("extracted_text") or "").strip())
        or 0
    )
    rebuilt = build_document_status(
        document_selection=selection,
        document_preparation={
            "docs_count": docs_count,
            "docs_with_text_count": docs_with_text_count,
            "document_state": status.get("document_state") or "",
        },
        documents_summary=[
            {
                "filename": doc.get("filename"),
                "text_len": len(str(doc.get("extracted_text") or "")),
            }
            for doc in documents
        ],
    )
    if status:
        quality_keys = {
            "primary_document_confidence",
            "primary_document_source_kind",
            "primary_document_quality_reason",
            "primary_document_is_full_technical_spec",
            "primary_document_suspicious",
        }
        merged = {
            **rebuilt,
            **{
                key: value
                for key, value in status.items()
                if value and key not in quality_keys
            },
        }
        if not merged.get("primary_document_source_kind"):
            merged.update(rebuilt)
        return merged
    return rebuilt


def suggested_action_for_status(status: dict[str, Any]) -> str:
    confidence = str(status.get("primary_document_confidence") or "")
    source_kind = str(status.get("primary_document_source_kind") or "")
    selector_status = str(status.get("selector_status") or "")
    suspicious = bool(status.get("primary_document_suspicious"))
    docs_count = int(status.get("docs_count") or 0)
    docs_with_text_count = int(status.get("docs_with_text_count") or 0)
    source_documents_count = int(status.get("source_documents_count") or 0)
    documents_found = int(status.get("documents_found") or 0)

    if docs_count <= 0:
        if source_documents_count > 0 or documents_found > 0:
            return "source_docs_not_downloaded"
        return "missing_docs"
    if docs_with_text_count <= 0:
        return "low_confidence_card_only"
    if suspicious:
        return "suspicious_primary_doc"
    if confidence == DOCUMENT_CONFIDENCE_HIGH:
        return "ok_high_confidence"
    if confidence == DOCUMENT_CONFIDENCE_MEDIUM:
        return "ok_medium_confidence_review"
    if source_kind in {DOCUMENT_SOURCE_MISSING, DOCUMENT_SOURCE_CARD_ONLY}:
        return "low_confidence_card_only"
    if (
        selector_status == "technical_spec_found"
        and confidence == DOCUMENT_CONFIDENCE_LOW
    ):
        return "selector_may_need_review"
    if source_kind in {
        DOCUMENT_SOURCE_PRICING_OR_BOM,
        DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
        DOCUMENT_SOURCE_UNKNOWN,
    }:
        return "selector_may_need_review"
    return "low_confidence_card_only"


def audit_output_row(row: dict[str, Any]) -> dict[str, str]:
    documents = _documents_from_row(row)
    selection = _selection_from_report_or_documents(row, documents)
    status = _document_status_for_row(row, documents, selection)
    source_documents_count = _source_documents_count(row)
    preparation = _document_preparation_from_row(row)
    documents_found = int(preparation.get("documents_found") or 0)
    documents_selected = int(preparation.get("documents_selected") or 0)
    documents_failed = int(preparation.get("documents_failed") or 0)
    docs_count = int(status.get("docs_count") or 0)
    docs_with_text_count = int(status.get("docs_with_text_count") or 0)
    inventory_state = _document_inventory_state(
        docs_count=docs_count,
        docs_with_text_count=docs_with_text_count,
        source_documents_count=source_documents_count,
        documents_found=documents_found,
    )
    status = {
        **status,
        "source_documents_count": source_documents_count,
        "documents_found": documents_found,
        "documents_selected": documents_selected,
        "documents_failed": documents_failed,
    }
    suggested_action = suggested_action_for_status(status)
    latest_card_only = latest_report_is_card_only(row)
    stale_refresh_candidate = stale_card_only_refresh_candidate(
        {**row, "docs_with_text_count": docs_with_text_count}
    )
    if stale_refresh_candidate:
        suggested_action = "stale_card_only_refresh_candidate"
    has_full_report = bool(row.get("llm_customer_lead_report_result"))
    return {
        "external_id": str(row.get("external_id") or ""),
        "title": str(row.get("title") or ""),
        "customer_name": str(row.get("customer_name") or ""),
        "initial_price": str(row.get("initial_price") or ""),
        "has_full_lead_report": _bool_text(has_full_report),
        "latest_report_created_at": _created_at_text(row.get("latest_report_created_at")),
        "latest_report_card_only": _bool_text(latest_card_only),
        "stale_card_only_refresh_candidate": _bool_text(stale_refresh_candidate),
        "docs_count": str(docs_count),
        "docs_with_text_count": str(docs_with_text_count),
        "source_documents_count": str(source_documents_count),
        "document_inventory_state": inventory_state,
        "document_preparation_status": str(preparation.get("preparation_status") or ""),
        "documents_found": str(documents_found),
        "documents_selected": str(documents_selected),
        "documents_failed": str(documents_failed),
        "document_state": str(status.get("code") or ""),
        "selector_status": str(status.get("selector_status") or ""),
        "primary_document_title": str(status.get("primary_document_title") or ""),
        "primary_section_hint": str(status.get("primary_section_hint") or ""),
        "primary_document_source_kind": str(
            status.get("primary_document_source_kind") or ""
        ),
        "primary_document_confidence": str(
            status.get("primary_document_confidence") or DOCUMENT_CONFIDENCE_LOW
        ),
        "primary_document_quality_reason": str(
            status.get("primary_document_quality_reason") or ""
        ),
        "readiness_warning": str(status.get("warning") or ""),
        "suggested_action": suggested_action,
    }


def fetch_recent_rows(
    *,
    hours: float,
    limit: int,
    external_id: str | None = None,
    all_history: bool = False,
    only_stale_card_only: bool = False,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    from app.config import settings

    filters: list[str] = []
    params: list[Any] = [LEAD_LLM_REPORT_ANALYSIS_TYPE]
    if external_id:
        filters.append("external_id = %s")
        params.append(external_id)
    elif not all_history:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        filters.append(
            "COALESCE(latest_report_created_at, published_at, created_at) >= %s"
        )
        params.append(cutoff)
    if only_stale_card_only:
        filters.extend(
            [
                "llm_customer_lead_report_result IS NOT NULL",
                "docs_with_text_count > 0",
                """
                (
                    llm_customer_lead_report_result::jsonb #>> '{report,document_status,analysis_basis}' = 'lead_card_only'
                    OR llm_customer_lead_report_result::jsonb #>> '{meta,document_status,analysis_basis}' = 'lead_card_only'
                    OR llm_customer_lead_report_result::jsonb #>> '{report,analysis_basis}' = 'lead_card_only'
                    OR llm_customer_lead_report_result::jsonb #>> '{analysis_basis}' = 'lead_card_only'
                    OR llm_customer_lead_report_result::jsonb #>> '{report,document_status,code}' IN ('documents_missing_card_only', 'documents_blocked_card_only')
                    OR llm_customer_lead_report_result::jsonb #>> '{meta,document_status,code}' IN ('documents_missing_card_only', 'documents_blocked_card_only')
                    OR llm_customer_lead_report_result::jsonb #>> '{meta,lead_context_mode}' = 'card_only'
                    OR llm_customer_lead_report_result::jsonb #>> '{meta,lead_documents_missing_allowed}' = 'true'
                    OR llm_customer_lead_report_result::jsonb #>> '{meta,card_only}' = 'true'
                    OR llm_customer_lead_report_result::jsonb #>> '{meta,context_source}' = 'card_only'
                )
                """,
            ]
        )
    params.append(limit)
    where_sql = " AND ".join(filters) if filters else "TRUE"

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                WITH candidate_rows AS (
                    SELECT
                        t.id AS tender_id,
                        t.external_id,
                        t.title,
                        t.customer_name,
                        t.initial_price,
                        t.published_at,
                        t.created_at,
                        t.raw,
                        report.result AS llm_customer_lead_report_result,
                        report.created_at AS latest_report_created_at,
                        COALESCE(docs_stats.docs_count, 0) AS docs_count,
                        COALESCE(docs_stats.docs_with_text_count, 0) AS docs_with_text_count,
                        COALESCE(docs_payload.documents, '[]'::jsonb) AS documents
                    FROM tenders t
                    LEFT JOIN LATERAL (
                        SELECT result, created_at
                        FROM analysis_results
                        WHERE tender_id = t.id
                          AND analysis_type = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) report ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT
                            COUNT(*) AS docs_count,
                            COUNT(*) FILTER (
                                WHERE extracted_text IS NOT NULL
                                  AND length(extracted_text) > 0
                            ) AS docs_with_text_count
                        FROM documents
                        WHERE tender_id = t.id
                    ) docs_stats ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'id', d.id::text,
                                'title', d.filename,
                                'filename', d.filename,
                                'mime_type', d.mime_type,
                                'extracted_text', COALESCE(d.extracted_text, '')
                            )
                            ORDER BY d.id
                        ) AS documents
                        FROM documents d
                        WHERE d.tender_id = t.id
                    ) docs_payload ON TRUE
                )
                SELECT *
                FROM candidate_rows
                WHERE {where_sql}
                ORDER BY COALESCE(latest_report_created_at, published_at, created_at) DESC,
                         tender_id DESC
                LIMIT %s;
                """,
                params,
            )
            return list(cur.fetchall())


def write_rows(rows: list[dict[str, str]], *, output_path: str | None = None) -> None:
    output = open(output_path, "w", encoding="utf-8", newline="") if output_path else sys.stdout
    try:
        writer = csv.DictWriter(
            output,
            fieldnames=OUTPUT_FIELDS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    finally:
        if output_path:
            output.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit document selector confidence")
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--all-history", action="store_true")
    parser.add_argument("--only-warnings", action="store_true")
    parser.add_argument("--only-stale-card-only", action="store_true")
    parser.add_argument("--external-id")
    parser.add_argument("--output")
    args = parser.parse_args()

    rows = fetch_recent_rows(
        hours=max(0.1, float(args.hours)),
        limit=max(1, int(args.limit)),
        external_id=args.external_id,
        all_history=bool(args.all_history or args.only_stale_card_only),
        only_stale_card_only=bool(args.only_stale_card_only),
    )
    output_rows = [audit_output_row(dict(row)) for row in rows]
    if args.only_stale_card_only:
        output_rows = [
            row
            for row in output_rows
            if row.get("stale_card_only_refresh_candidate") == "true"
        ]
    if args.only_warnings:
        output_rows = [
            row
            for row in output_rows
            if row.get("suggested_action") != "ok_high_confidence"
        ]
    output_path = str(Path(args.output)) if args.output else None
    write_rows(output_rows, output_path=output_path)


if __name__ == "__main__":
    main()
