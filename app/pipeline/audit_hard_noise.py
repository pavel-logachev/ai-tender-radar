from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta, timezone
from typing import Any


OUTPUT_FIELDS = (
    "external_id",
    "title",
    "customer_name",
    "initial_price",
    "hard_noise_reason",
    "matched_target_signals",
    "latest_feedback",
    "latest_triage_decision",
    "has_full_report",
    "suggested_action",
)
LEAD_LLM_REPORT_ANALYSIS_TYPE = "llm_customer_lead_report"
LEAD_TRIAGE_ANALYSIS_TYPE = "llm_customer_lead_triage"
NEGATIVE_SALES_FEEDBACK_SKIP_STATUSES = {
    "sales_not_relevant",
    "skip",
    "not_interesting",
    "not_our_profile",
    "tailored",
    "bad_region",
    "bad_price",
    "not_profile",
    "service_only",
    "low_value_one_off",
    "bad_customer_fit",
    "bad_timing",
    "duplicate_or_processed",
    "other_reject",
}


def _shortlist_module():
    from app.pipeline import run_llm_for_shortlist

    return run_llm_for_shortlist


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "да"}


def audit_row_latest_triage(row: dict[str, Any]) -> dict[str, Any]:
    return _shortlist_module().unwrap_lead_triage_result(
        row.get("lead_triage_result")
    ) or {}


def audit_row_has_full_report(row: dict[str, Any]) -> bool:
    return _shortlist_module().result_has_report(
        row.get("llm_customer_lead_report_result")
    )


def suggested_action_for_audit_row(
    row: dict[str, Any],
    *,
    hard_noise_reason: str,
    matched_target_signals: tuple[str, ...],
) -> str:
    latest_feedback = str(row.get("latest_feedback") or "").strip()
    if latest_feedback in NEGATIVE_SALES_FEEDBACK_SKIP_STATUSES:
        return "skipped_by_feedback"

    if audit_row_has_full_report(row):
        return "skipped_by_existing_report"

    triage = audit_row_latest_triage(row)
    decision = _shortlist_module().normalize_lead_triage_decision(
        triage.get("lead_decision")
    )
    if decision == "go" and _bool_value(triage.get("requires_full_lead_report")):
        return "existing_go_waiting"

    if matched_target_signals:
        return "override_to_triage"

    return "strict_skip"


def audit_output_row(row: dict[str, Any]) -> dict[str, str] | None:
    shortlist = _shortlist_module()
    hard_noise_reason = shortlist.lead_explicit_hard_noise_reason_for_row(row)
    if not hard_noise_reason:
        return None

    target_signals = shortlist.lead_target_signal_matches_for_row(
        row,
        hard_noise_reason=hard_noise_reason,
    )
    triage = audit_row_latest_triage(row)
    latest_triage_decision = shortlist.normalize_lead_triage_decision(
        triage.get("lead_decision")
    )
    has_full_report = audit_row_has_full_report(row)
    return {
        "external_id": str(row.get("external_id") or ""),
        "title": str(row.get("title") or ""),
        "customer_name": str(row.get("customer_name") or ""),
        "initial_price": str(row.get("initial_price") or ""),
        "hard_noise_reason": hard_noise_reason,
        "matched_target_signals": ";".join(target_signals),
        "latest_feedback": str(row.get("latest_feedback") or ""),
        "latest_triage_decision": latest_triage_decision,
        "has_full_report": _bool_text(has_full_report),
        "suggested_action": suggested_action_for_audit_row(
            row,
            hard_noise_reason=hard_noise_reason,
            matched_target_signals=target_signals,
        ),
    }


def fetch_recent_rows(*, limit: int, days: int) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    from app.config import settings

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
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
                    rb.result AS result,
                    rb.recommendation,
                    rb.confidence,
                    rb.score,
                    triage.result AS lead_triage_result,
                    triage.created_at AS lead_triage_created_at,
                    full_report.result AS llm_customer_lead_report_result,
                    feedback.status AS latest_feedback
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT result, recommendation, confidence, score, created_at
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type IN ('rule_based_score', 'rule_based_scoring')
                    ORDER BY created_at DESC
                    LIMIT 1
                ) rb ON TRUE
                LEFT JOIN LATERAL (
                    SELECT result, created_at
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) triage ON TRUE
                LEFT JOIN LATERAL (
                    SELECT result, created_at
                    FROM analysis_results
                    WHERE tender_id = t.id
                      AND analysis_type = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                ) full_report ON TRUE
                LEFT JOIN LATERAL (
                    SELECT status, created_at
                    FROM feedback
                    WHERE tender_id = t.id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) feedback ON TRUE
                WHERE COALESCE(t.published_at, t.created_at) >= %s
                ORDER BY COALESCE(t.published_at, t.created_at) DESC, t.id DESC
                LIMIT %s;
                """,
                (
                    LEAD_TRIAGE_ANALYSIS_TYPE,
                    LEAD_LLM_REPORT_ANALYSIS_TYPE,
                    cutoff,
                    limit,
                ),
            )
            return list(cur.fetchall())


def write_rows(rows: list[dict[str, str]], *, output_format: str) -> None:
    delimiter = "\t" if output_format == "tsv" else ","
    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=OUTPUT_FIELDS,
        delimiter=delimiter,
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit lead hard-noise decisions")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--format", choices=("tsv", "csv"), default="tsv")
    args = parser.parse_args()

    rows = fetch_recent_rows(limit=max(1, args.limit), days=max(1, args.days))
    output_rows = [
        output_row
        for row in rows
        if (output_row := audit_output_row(dict(row))) is not None
    ]
    write_rows(output_rows, output_format=args.format)


if __name__ == "__main__":
    main()
