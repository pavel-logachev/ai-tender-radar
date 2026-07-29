from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.pipeline import audit_document_selection


OUTPUT_FIELDS = (
    "external_id",
    "title",
    "customer_name",
    "initial_price",
    "source_documents_count",
    "docs_before",
    "docs_with_text_before",
    "suggested_action_before",
    "batch_status",
    "preparation_status",
    "ready_for_llm",
    "document_readiness",
    "docs_after",
    "docs_with_text_after",
    "documents_found",
    "documents_selected",
    "saved_documents",
    "documents_failed",
    "rate_limit_retries",
    "rate_limited",
    "planner_used",
    "summary",
)


def _bool_text(value: Any) -> str:
    return "true" if bool(value) else "false"


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clean_cell(value: Any) -> str:
    return " ".join(str(value or "").replace("\t", " ").split())


def _status_value(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value or "")


def fetch_backfill_candidates(
    *,
    hours: float,
    limit: int,
    external_id: str | None = None,
    include_downloaded_no_text: bool = False,
) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    from app.config import settings

    filters = [
        "source_documents_count > 0",
        (
            "docs_with_text_count = 0"
            if include_downloaded_no_text
            else "docs_count = 0"
        ),
    ]
    params: list[Any] = [audit_document_selection.LEAD_LLM_REPORT_ANALYSIS_TYPE]
    if external_id:
        filters.append("external_id = %s")
        params.append(external_id)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        filters.append(
            "COALESCE(latest_report_created_at, published_at, created_at) >= %s"
        )
        params.append(cutoff)

    params.append(limit)
    where_sql = " AND ".join(filters)

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
                        COALESCE(docs_stats.docs_with_text_count, 0)
                            AS docs_with_text_count,
                        COALESCE(docs_payload.documents, '[]'::jsonb) AS documents,
                        CASE
                            WHEN jsonb_typeof(
                                t.raw::jsonb #> '{{full,documents}}'
                            ) = 'array'
                            THEN jsonb_array_length(
                                t.raw::jsonb #> '{{full,documents}}'
                            )
                            ELSE 0
                        END AS source_documents_count
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
                    WHERE t.source = 'zakupki360'
                )
                SELECT *
                FROM candidate_rows
                WHERE {where_sql}
                ORDER BY
                    COALESCE(latest_report_created_at, published_at, created_at) DESC,
                    tender_id DESC
                LIMIT %s;
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def _download_details(result: Any) -> dict[str, Any]:
    steps = getattr(result, "steps", None)
    if not isinstance(steps, dict):
        return {}

    step = steps.get("targeted_document_download")
    details = getattr(step, "details", None)
    return details if isinstance(details, dict) else {}


def classify_backfill_result(result: Any) -> str:
    details = _download_details(result)
    counts = getattr(result, "document_counts", None)
    downloaded = _int_value(getattr(counts, "downloaded", 0))
    with_text = _int_value(getattr(counts, "with_extracted_text", 0))

    if _status_value(getattr(result, "status", "")) == "tender_not_found":
        return "tender_not_found"
    if bool(details.get("persistent_requeue_waiting")):
        return "retry_waiting"
    if bool(details.get("stopped_due_to_rate_limit")) or bool(
        details.get("persistent_requeue_scheduled")
    ):
        return "rate_limited"
    if with_text > 0:
        return "downloaded_with_text"
    if downloaded > 0:
        return "downloaded_no_text"
    if bool(getattr(result, "marketplace_auth_blocked", False)):
        return "blocked_marketplace_auth"
    if _int_value(details.get("documents_failed")) > 0:
        return "not_downloadable_or_rejected"
    if getattr(result, "errors", None):
        return "prepared_with_errors"
    return "not_prepared"


def should_stop_after_result(result: Any) -> bool:
    details = _download_details(result)
    return bool(details.get("stopped_due_to_rate_limit")) or bool(
        details.get("persistent_requeue_scheduled")
    )


def dry_run_output_row(candidate: dict[str, Any]) -> dict[str, str]:
    audit_row = audit_document_selection.audit_output_row(candidate)
    return {
        "external_id": audit_row["external_id"],
        "title": audit_row["title"],
        "customer_name": audit_row["customer_name"],
        "initial_price": audit_row["initial_price"],
        "source_documents_count": audit_row["source_documents_count"],
        "docs_before": audit_row["docs_count"],
        "docs_with_text_before": audit_row["docs_with_text_count"],
        "suggested_action_before": audit_row["suggested_action"],
        "batch_status": "dry_run_would_prepare",
        "preparation_status": "",
        "ready_for_llm": "false",
        "document_readiness": "",
        "docs_after": audit_row["docs_count"],
        "docs_with_text_after": audit_row["docs_with_text_count"],
        "documents_found": audit_row["source_documents_count"],
        "documents_selected": "0",
        "saved_documents": "0",
        "documents_failed": "0",
        "rate_limit_retries": "0",
        "rate_limited": "false",
        "planner_used": "false",
        "summary": "dry-run: add --apply to download and extract documents",
    }


def applied_output_row(
    candidate: dict[str, Any],
    result: Any,
    *,
    error: Exception | None = None,
) -> dict[str, str]:
    audit_row = audit_document_selection.audit_output_row(candidate)
    details = _download_details(result)
    counts = getattr(result, "document_counts", None)

    if error is not None:
        return {
            "external_id": audit_row["external_id"],
            "title": audit_row["title"],
            "customer_name": audit_row["customer_name"],
            "initial_price": audit_row["initial_price"],
            "source_documents_count": audit_row["source_documents_count"],
            "docs_before": audit_row["docs_count"],
            "docs_with_text_before": audit_row["docs_with_text_count"],
            "suggested_action_before": audit_row["suggested_action"],
            "batch_status": "error",
            "preparation_status": "",
            "ready_for_llm": "false",
            "document_readiness": "",
            "docs_after": audit_row["docs_count"],
            "docs_with_text_after": audit_row["docs_with_text_count"],
            "documents_found": audit_row["source_documents_count"],
            "documents_selected": "0",
            "saved_documents": "0",
            "documents_failed": "0",
            "rate_limit_retries": "0",
            "rate_limited": "false",
            "planner_used": "false",
            "summary": _clean_cell(error),
        }

    docs_after = _int_value(getattr(counts, "downloaded", 0))
    docs_with_text_after = _int_value(getattr(counts, "with_extracted_text", 0))
    rate_limited = bool(details.get("stopped_due_to_rate_limit")) or bool(
        details.get("persistent_requeue_scheduled")
        or details.get("persistent_requeue_waiting")
    )

    return {
        "external_id": audit_row["external_id"],
        "title": audit_row["title"],
        "customer_name": audit_row["customer_name"],
        "initial_price": audit_row["initial_price"],
        "source_documents_count": audit_row["source_documents_count"],
        "docs_before": audit_row["docs_count"],
        "docs_with_text_before": audit_row["docs_with_text_count"],
        "suggested_action_before": audit_row["suggested_action"],
        "batch_status": classify_backfill_result(result),
        "preparation_status": _status_value(getattr(result, "status", "")),
        "ready_for_llm": _bool_text(getattr(result, "ready_for_llm", False)),
        "document_readiness": _clean_cell(
            getattr(result, "document_readiness", "")
        ),
        "docs_after": str(docs_after),
        "docs_with_text_after": str(docs_with_text_after),
        "documents_found": str(
            _int_value(details.get("documents_found"))
            or _int_value(audit_row["source_documents_count"])
        ),
        "documents_selected": str(_int_value(details.get("documents_selected"))),
        "saved_documents": str(_int_value(details.get("saved_documents"))),
        "documents_failed": str(_int_value(details.get("documents_failed"))),
        "rate_limit_retries": str(_int_value(details.get("rate_limit_retries"))),
        "rate_limited": _bool_text(rate_limited),
        "planner_used": _bool_text(details.get("document_download_planner_used")),
        "summary": _clean_cell(getattr(result, "summary", "")),
    }


def run_backfill(
    candidates: list[dict[str, Any]],
    *,
    apply: bool,
    limit_docs: int,
    force_redownload: bool = False,
    overwrite_text: bool = False,
    continue_after_rate_limit: bool = False,
) -> list[dict[str, str]]:
    if not apply:
        return [dry_run_output_row(candidate) for candidate in candidates]

    from app.pipeline.prepare_tender_for_analysis import prepare_tender_for_analysis

    output_rows: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            result = prepare_tender_for_analysis(
                external_id=str(candidate.get("external_id") or ""),
                limit_docs=limit_docs,
                force_redownload=force_redownload,
                overwrite_text=overwrite_text,
                use_llm_planner=False,
            )
        except Exception as exc:
            output_rows.append(applied_output_row(candidate, None, error=exc))
            continue

        output_rows.append(applied_output_row(candidate, result))
        if should_stop_after_result(result) and not continue_after_rate_limit:
            break

    return output_rows


def write_rows(rows: list[dict[str, str]], *, output_path: str | None = None) -> None:
    output = (
        open(output_path, "w", encoding="utf-8", newline="")
        if output_path
        else sys.stdout
    )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill source/card documents without rebuilding LLM reports"
    )
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--external-id")
    parser.add_argument("--include-downloaded-no-text", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit-docs", type=int, default=5)
    parser.add_argument("--force-redownload", action="store_true")
    parser.add_argument("--overwrite-text", action="store_true")
    parser.add_argument("--continue-after-rate-limit", action="store_true")
    parser.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = fetch_backfill_candidates(
        hours=max(0.1, float(args.hours)),
        limit=max(1, int(args.limit)),
        external_id=args.external_id,
        include_downloaded_no_text=args.include_downloaded_no_text,
    )
    output_rows = run_backfill(
        rows,
        apply=bool(args.apply),
        limit_docs=max(1, int(args.limit_docs)),
        force_redownload=bool(args.force_redownload),
        overwrite_text=bool(args.overwrite_text),
        continue_after_rate_limit=bool(args.continue_after_rate_limit),
    )
    output_path = str(Path(args.output)) if args.output else None
    write_rows(output_rows, output_path=output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
