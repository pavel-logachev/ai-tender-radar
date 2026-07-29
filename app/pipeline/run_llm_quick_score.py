from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.business_profile import (
    category_priority,
    get_price,
    is_excluded_vertical,
    is_full_deal_for_category,
    is_low_priority_deal,
    load_business_profile,
    match_target_category,
)
from app.business_rules import business_assessment, effective_recommendation
from app.config import settings
from app.llm.quick_score import (
    ANALYSIS_TYPE,
    QuickScoreParseError,
    QuickScoreRunResult,
    run_llm_quick_score,
    save_llm_quick_score_error,
    save_llm_quick_score,
)


FAILURE_DETAIL_MAX_CHARS = 500


@dataclass(frozen=True)
class QuickScoreCandidateFailure:
    external_id: str
    reason: str
    detail: str = ""


def truncate_detail(text: str, max_chars: int = FAILURE_DETAIL_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text

    return f"{text[: max_chars - 3]}..."


def compact_exception_detail(exc: Exception) -> str:
    return truncate_detail(" ".join(str(exc).split()))


def log_candidate_failure(failure: QuickScoreCandidateFailure) -> None:
    parts = [
        f"LLM quick-score candidate failed: external_id={failure.external_id}",
        f"reason={failure.reason}",
    ]

    if failure.detail:
        parts.append(f"detail={failure.detail}")

    print(" ".join(parts), flush=True)


def print_debug_skip(row: dict[str, Any], reason: str, enabled: bool) -> None:
    if not enabled:
        return

    external_id = row.get("external_id") or "unknown"
    title = row.get("title") or ""
    print(f"Skip LLM quick-score candidate {external_id}: {reason} | {title}")


def has_quick_score(row: dict[str, Any]) -> bool:
    result = row.get("llm_quick_score_result")
    return isinstance(result, dict) and bool(result.get("report"))


def candidate_priority(row: dict[str, Any], profile: dict[str, Any]) -> tuple[int, int, int, str]:
    _, category_cfg = match_target_category(row, profile)
    priority = category_priority(category_cfg)
    score = int(row.get("score") or 0)

    deadline = row.get("deadline_at")
    deadline_text = deadline.isoformat() if hasattr(deadline, "isoformat") else str(deadline or "")

    return (priority, -get_price(row), -score, deadline_text)


def get_candidate_pool(pool_limit: int) -> list[dict[str, Any]]:
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
                    t.currency,
                    t.region,
                    t.law,
                    t.procedure_type,
                    t.published_at,
                    t.deadline_at,
                    t.url,
                    t.raw,
                    rb.score,
                    rb.recommendation,
                    rb.confidence,
                    rb.result,
                    dr.result AS document_risk_result,
                    lqs.result AS llm_quick_score_result,
                    lpr.result AS llm_report_result,
                    COALESCE(docs_stats.docs_count, 0) AS docs_count,
                    COALESCE(docs_stats.docs_with_text, 0) AS docs_with_text
                FROM tenders t
                JOIN analysis_results rb
                  ON rb.tender_id = t.id
                 AND rb.analysis_type = 'rule_based_score'
                LEFT JOIN analysis_results dr
                  ON dr.tender_id = t.id
                 AND dr.analysis_type = 'document_risk_analysis'
                LEFT JOIN analysis_results lqs
                  ON lqs.tender_id = t.id
                 AND lqs.analysis_type = %s
                LEFT JOIN analysis_results lpr
                  ON lpr.tender_id = t.id
                 AND lpr.analysis_type = 'llm_presales_report'
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
                WHERE rb.recommendation IN ('go', 'maybe')
                ORDER BY rb.score DESC, t.deadline_at ASC NULLS LAST
                LIMIT %s;
                """,
                (ANALYSIS_TYPE, pool_limit),
            )
            return [dict(row) for row in cur.fetchall()]


def select_candidates(
    *,
    profile: dict[str, Any],
    limit: int,
    pool_limit: int,
    force: bool,
    min_price: int,
    include_low_priority: bool,
    include_non_full_deals: bool,
    include_domestic_restricted: bool,
    debug_skips: bool,
) -> list[dict[str, Any]]:
    rows = get_candidate_pool(pool_limit)
    candidates: list[dict[str, Any]] = []

    for row in rows:
        if effective_recommendation(row) == "no_go":
            print_debug_skip(row, "effective_recommendation=no_go", debug_skips)
            continue

        assessment = business_assessment(row)
        if assessment.get("action") in {"skip_incumbent", "no_go"}:
            print_debug_skip(row, f"business_action={assessment.get('action')}", debug_skips)
            continue

        if assessment.get("market_access") == "domestic_restricted" and not include_domestic_restricted:
            print_debug_skip(row, "market_access=domestic_restricted", debug_skips)
            continue

        if is_excluded_vertical(row, profile):
            print_debug_skip(row, "excluded_vertical", debug_skips)
            continue

        if has_quick_score(row) and not force:
            print_debug_skip(row, "already_has_llm_quick_score", debug_skips)
            continue

        if get_price(row) < min_price:
            print_debug_skip(row, f"price_below_min_price={min_price}", debug_skips)
            continue

        category_name, category_cfg = match_target_category(row, profile)
        if not category_name or not category_cfg:
            print_debug_skip(row, "no_target_category", debug_skips)
            continue

        if is_low_priority_deal(row, profile) and not include_low_priority:
            print_debug_skip(row, "low_priority_deal", debug_skips)
            continue

        if not include_non_full_deals and not is_full_deal_for_category(row, profile, category_cfg):
            print_debug_skip(row, "not_full_deal_for_category", debug_skips)
            continue

        row = dict(row)
        row["_llm_category"] = category_name
        row["_llm_category_label"] = category_cfg.get("label") or category_name
        candidates.append(row)

    return sorted(candidates, key=lambda row: candidate_priority(row, profile))[:limit]


def build_business_signals(row: dict[str, Any]) -> dict[str, Any]:
    signals = business_assessment(row)
    return {
        "target_category": row.get("_llm_category"),
        "target_category_label": row.get("_llm_category_label"),
        "commercial_interest": signals.get("commercial_interest"),
        "market_access": signals.get("market_access"),
        "tailoring_effect": signals.get("tailoring_effect"),
        "action": signals.get("action"),
    }


def run_quick_score_for_candidate(
    row: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
    max_output_tokens: int,
    json_mode: bool,
) -> QuickScoreRunResult:
    try:
        result = run_llm_quick_score(
            row,
            business_signals=build_business_signals(row),
            provider=provider,
            model=model,
            max_output_tokens=max_output_tokens,
            json_mode=json_mode,
        )
    except QuickScoreParseError as exc:
        try:
            save_llm_quick_score_error(
                tender_id=str(row["tender_id"]),
                error=exc,
                metadata={
                    "json_mode": json_mode,
                    "pipeline": "run_llm_quick_score",
                },
            )
        except Exception as save_exc:
            print(
                "Could not save llm_quick_score parse failure: "
                f"external_id={row.get('external_id')} detail={compact_exception_detail(save_exc)}",
                flush=True,
            )
        raise

    save_llm_quick_score(
        tender_id=str(row["tender_id"]),
        result=result,
        metadata={
            "json_mode": json_mode,
            "pipeline": "run_llm_quick_score",
        },
    )

    return result


def print_quick_score_summary(
    successful_external_ids: list[str],
    failures: list[QuickScoreCandidateFailure],
    decisions: dict[str, int],
) -> None:
    print()
    print("LLM quick-score summary:", flush=True)
    print(f"- succeeded: {len(successful_external_ids)}", flush=True)
    print(f"- failed: {len(failures)}", flush=True)
    print(
        "- top decisions: "
        f"go={decisions.get('go', 0)}, "
        f"maybe={decisions.get('maybe', 0)}, "
        f"no_go={decisions.get('no_go', 0)}",
        flush=True,
    )

    if successful_external_ids:
        print(f"- succeeded_external_ids: {', '.join(successful_external_ids)}", flush=True)

    if failures:
        failed_external_ids = ", ".join(f"{failure.external_id}({failure.reason})" for failure in failures)
        print(f"- failed_external_ids: {failed_external_ids}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run lightweight LLM quick-score for cleaned shortlist")
    parser.add_argument("--profile", default="config/business_profile.yaml")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--pool-limit", type=int, default=None)
    parser.add_argument("--force", action="store_true", help="Run quick-score even if it already exists")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug-skips", action="store_true", help="Print why tenders were skipped")
    parser.add_argument("--min-price", type=int, default=None)
    parser.add_argument("--include-low-priority", action="store_true")
    parser.add_argument("--include-non-full-deals", action="store_true")
    parser.add_argument("--include-domestic-restricted", action="store_true")
    parser.add_argument("--max-output-tokens", type=int, default=700)
    parser.add_argument("--provider", help="Override LLM provider for this quick-score run")
    parser.add_argument("--model", help="Override LLM model for this quick-score run")
    parser.add_argument("--no-json-mode", action="store_true", help="Disable response_format=json_object")
    args = parser.parse_args()

    profile = load_business_profile(Path(args.profile))
    quick_cfg = profile.get("llm_quick_score") or {}
    llm_selection_cfg = profile.get("llm_selection") or {}

    limit = int(args.limit or quick_cfg.get("default_limit", 20))
    pool_limit = int(args.pool_limit or quick_cfg.get("default_pool_limit", 100))
    min_price = int(
        args.min_price
        or quick_cfg.get("min_price_rub")
        or llm_selection_cfg.get("min_price_rub")
        or 5_000_000
    )
    json_mode = not args.no_json_mode

    candidates = select_candidates(
        profile=profile,
        limit=limit,
        pool_limit=pool_limit,
        force=args.force,
        min_price=min_price,
        include_low_priority=args.include_low_priority,
        include_non_full_deals=args.include_non_full_deals,
        include_domestic_restricted=args.include_domestic_restricted,
        debug_skips=args.debug_skips,
    )

    if not candidates:
        print("No LLM quick-score candidates selected.")
        return

    print("Selected LLM quick-score candidates:")
    for index, row in enumerate(candidates, start=1):
        assessment = business_assessment(row)
        print(
            f"{index}. {row.get('external_id')} | "
            f"{get_price(row):,} RUB | "
            f"{row.get('_llm_category_label')} | "
            f"{assessment.get('market_access')} | "
            f"{row.get('title')}"
        )

    successful_external_ids: list[str] = []
    failures: list[QuickScoreCandidateFailure] = []
    decisions = {"go": 0, "maybe": 0, "no_go": 0}

    if args.dry_run:
        print()
        print("Dry-run: no LLM calls were made and no analysis_results rows were changed.", flush=True)
        print_quick_score_summary(successful_external_ids, failures, decisions)
        return

    for row in candidates:
        external_id = str(row.get("external_id") or "unknown")
        title = row.get("title") or ""

        print()
        print(f"===== LLM quick-score candidate {external_id}: {title} =====", flush=True)

        try:
            result = run_quick_score_for_candidate(
                row,
                provider=args.provider,
                model=args.model,
                max_output_tokens=args.max_output_tokens,
                json_mode=json_mode,
            )
        except QuickScoreParseError as exc:
            failure = QuickScoreCandidateFailure(
                external_id=external_id,
                reason="parse_failed",
                detail=compact_exception_detail(exc),
            )
            failures.append(failure)
            log_candidate_failure(failure)
            continue
        except Exception as exc:
            failure = QuickScoreCandidateFailure(
                external_id=external_id,
                reason="quick_score_failed",
                detail=compact_exception_detail(exc),
            )
            failures.append(failure)
            log_candidate_failure(failure)
            continue

        decision = result.report.get("decision") or "maybe"
        decisions[decision] = decisions.get(decision, 0) + 1
        successful_external_ids.append(external_id)
        print(
            f"Saved llm_quick_score: decision={decision}, "
            f"confidence={result.report.get('confidence')}, "
            f"should_run_full_analysis={result.report.get('should_run_full_analysis')}",
            flush=True,
        )

    print_quick_score_summary(successful_external_ids, failures, decisions)


if __name__ == "__main__":
    main()
