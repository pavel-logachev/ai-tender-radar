from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.pipeline.search_profile import (
    DEFAULT_PROFILE_PATH,
    QueryRunSummary,
    aggregate_by_pack,
    aggregate_total,
    defaults,
    list_group_names,
    list_pack_names,
    load_search_profile,
    query_specs_for_packs,
    selected_pack_names,
)


DEFAULT_QUERY_DELAY_SECONDS = 20.0


@dataclass(frozen=True)
class QueryFailure:
    pack: str
    query: str
    reason: str


def as_int(value: object, default: int) -> int:
    if value is None:
        return default
    return int(value)


def as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def summary_from_collection(pack: str, stats: Any) -> QueryRunSummary:
    return QueryRunSummary(
        pack=pack,
        query=stats.query,
        collected=stats.collected,
        external_ids=set(stats.external_ids),
        new_external_ids=set(stats.new_external_ids),
        saved_or_updated=stats.saved_or_updated,
        skipped_existing=stats.skipped_existing,
        full_load_attempts=stats.full_load_attempts,
        failed_full_loads=stats.failed_full_loads,
    )


def failure_reason(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if response is not None and exc.__class__.__name__ == "HTTPStatusError":
        status_code = response.status_code
        retry_after = response.headers.get("retry-after")
        suffix = f", retry-after={retry_after}" if retry_after else ""
        return f"HTTP {status_code}{suffix}"

    reason = f"{type(exc).__name__}: {exc}"
    if len(reason) > 300:
        return reason[:297] + "..."
    return reason


def create_z360_client() -> Any:
    from app.collector.zakupki360 import Zakupki360Client

    return Zakupki360Client()


def close_z360_client(client: Any | None) -> None:
    if client is not None and hasattr(client, "close"):
        client.close()


def collect_with_client(
    *,
    client: Any,
    query: str,
    days_back: int,
    limit_full: int,
    min_price: float | None,
    reload_existing: bool,
    max_short_results: int | None,
) -> Any:
    from app.collector.zakupki360 import collect

    return collect(
        query=query,
        days_back=days_back,
        limit_full=limit_full,
        min_price=min_price,
        reload_existing=reload_existing,
        max_short_results=max_short_results,
        client=client,
    )


def token_login_retry_count(client: Any | None) -> int:
    if client is None:
        return 0
    return int(getattr(client, "login_retry_count", 0) or 0)


def sleep_between_queries(delay_seconds: float) -> None:
    if delay_seconds <= 0:
        return
    print(f"\nWaiting {delay_seconds:g}s before next query...")
    time.sleep(delay_seconds)


def fetch_rule_based_metrics(external_ids: Collection[str]) -> tuple[set[str], set[str]]:
    import psycopg
    from psycopg.rows import dict_row

    from app.config import settings

    ids = [str(external_id) for external_id in external_ids if external_id]
    if not ids:
        return set(), set()

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    t.external_id,
                    t.status,
                    rb.recommendation
                FROM tenders t
                LEFT JOIN LATERAL (
                    SELECT a.recommendation
                    FROM analysis_results a
                    WHERE a.tender_id = t.id
                      AND a.analysis_type = 'rule_based_score'
                    ORDER BY a.created_at DESC
                    LIMIT 1
                ) rb ON TRUE
                WHERE t.source = 'zakupki360'
                  AND t.external_id = ANY(%s);
                """,
                (ids,),
            )
            rows = cur.fetchall()

    scored: set[str] = set()
    passed: set[str] = set()

    for row in rows:
        external_id = str(row["external_id"])
        if row.get("status") == "scored":
            scored.add(external_id)
        if row.get("recommendation") in {"go", "maybe"}:
            passed.add(external_id)

    return scored, passed


def enrich_with_rule_based_metrics(summaries: list[QueryRunSummary]) -> None:
    for summary in summaries:
        scored, passed = fetch_rule_based_metrics(summary.external_ids)
        summary.scored_external_ids = scored
        summary.rule_based_passed_external_ids = passed


def print_pack_list(profile: dict) -> None:
    print("Доступные packs:")
    for pack_name in list_pack_names(profile):
        pack = (profile.get("packs") or {}).get(pack_name) or {}
        queries = pack.get("queries") or []
        description = pack.get("description") or ""
        suffix = f" - {description}" if description else ""
        print(f"- {pack_name}: запросов={len(queries)}{suffix}")

    groups = list_group_names(profile)
    if groups:
        print("\nДоступные groups:")
        for group_name in groups:
            group_cfg = (profile.get("groups") or {}).get(group_name) or {}
            packs = group_cfg.get("packs") if isinstance(group_cfg, dict) else group_cfg
            pack_text = ", ".join(str(pack) for pack in packs or [])
            print(f"- {group_name}: {pack_text}")


def print_dry_run(
    pack_names: list[str],
    specs,
    *,
    days_back: int,
    min_price: float | None,
    limit_full: int,
    max_short_results: int | None,
    reload_existing: bool,
    query_delay_seconds: float,
) -> None:
    print("Dry-run search profile")
    print(f"Packs: {', '.join(pack_names)}")
    print(
        "Collector params: "
        f"days_back={days_back}, min_price={min_price}, limit_full={limit_full}, "
        f"max_short_results={max_short_results}, reload_existing={reload_existing}"
    )
    print(f"Query delay seconds: {query_delay_seconds:g} (skipped in dry-run)")
    print("\nЗапросы:")

    for spec in specs:
        print(f"- [{spec.pack}] {spec.query}")


def print_summary(
    summaries: list[QueryRunSummary],
    *,
    scoring_enabled: bool,
    failures: list[QueryFailure],
    token_login_retries: int,
) -> None:
    succeeded = len(summaries)
    failed = len(failures)
    if succeeded and failed:
        run_status = "partial"
    elif failed:
        run_status = "failed"
    else:
        run_status = "success"

    print("\nSearch profile summary")
    print(f"Run status: {run_status}")
    print(f"Queries: succeeded={succeeded}, failed={failed}, total={succeeded + failed}")
    print(f"Token/login retries: {token_login_retries}")
    print(f"Rule-based scoring: {'enabled' if scoring_enabled else 'skipped'}")

    if failures:
        print("\nFailed queries:")
        for failure in failures:
            print(f"- [{failure.pack}] {failure.query}: {failure.reason}")

    print(
        "pack | query | collected | unique | new | saved | skipped_existing | "
        "full_attempts | failed | scored | rule_based_passed"
    )

    for summary in summaries:
        print(
            f"{summary.pack} | {summary.query} | {summary.collected} | {summary.unique} | "
            f"{summary.new} | {summary.saved_or_updated} | {summary.skipped_existing} | "
            f"{summary.full_load_attempts} | {summary.failed_full_loads} | "
            f"{summary.scored} | {summary.rule_based_passed}"
        )

    print("\nПо pack:")
    for aggregate in aggregate_by_pack(summaries):
        print(
            f"{aggregate.label}: collected={aggregate.collected}, unique={aggregate.unique}, "
            f"new={aggregate.new}, saved={aggregate.saved_or_updated}, "
            f"skipped_existing={aggregate.skipped_existing}, scored={aggregate.scored}, "
            f"rule_based_passed={aggregate.rule_based_passed}"
        )

    total = aggregate_total(summaries)
    print("\nИтого:")
    print(
        f"collected={total.collected}, unique={total.unique}, new={total.new}, "
        f"saved={total.saved_or_updated}, skipped_existing={total.skipped_existing}, "
        f"full_attempts={total.full_load_attempts}, failed={total.failed_full_loads}, "
        f"scored={total.scored}, rule_based_passed={total.rule_based_passed}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Запустить query packs Закупки360 из search profile")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_PATH), help="Path to search profile YAML")
    parser.add_argument("--pack", action="append", default=[], help="Pack name to run; can be passed multiple times")
    parser.add_argument("--group", action="append", default=[], help="Group name to run; can be passed multiple times")
    parser.add_argument("--list-packs", action="store_true", help="List available packs and groups")
    parser.add_argument("--dry-run", action="store_true", help="Print selected queries without collector calls")
    parser.add_argument("--days-back", type=int, default=None, help="Override profile days_back")
    parser.add_argument("--limit-full", type=int, default=None, help="Override full order loads per query")
    parser.add_argument("--max-short-results", type=int, default=None, help="Override short search results per query")
    parser.add_argument("--min-price", type=float, default=None, help="Override InitialPriceFrom")
    parser.add_argument("--reload-existing", action="store_true", help="Reload full orders even if tender exists")
    parser.add_argument("--skip-score", action="store_true", help="Skip rule-based scorer after collection")
    parser.add_argument(
        "--query-delay-seconds",
        type=float,
        default=DEFAULT_QUERY_DELAY_SECONDS,
        help="Delay between search profile queries; skipped in dry-run",
    )
    args = parser.parse_args(argv)

    profile = load_search_profile(Path(args.profile))

    if args.list_packs:
        print_pack_list(profile)
        return 0

    profile_defaults = defaults(profile)
    pack_names = selected_pack_names(profile, packs=args.pack, groups=args.group)
    specs = query_specs_for_packs(profile, pack_names)

    days_back = as_int(args.days_back, as_int(profile_defaults.get("days_back"), 14))
    limit_full = as_int(
        args.limit_full,
        as_int(profile_defaults.get("limit_full_per_query"), 10),
    )
    max_short_results = (
        args.max_short_results
        if args.max_short_results is not None
        else profile_defaults.get("max_short_results_per_query")
    )
    if max_short_results is not None:
        max_short_results = int(max_short_results)
    min_price = (
        args.min_price
        if args.min_price is not None
        else as_float_or_none(profile_defaults.get("min_price"))
    )

    if args.dry_run:
        print_dry_run(
            pack_names,
            specs,
            days_back=days_back,
            min_price=min_price,
            limit_full=limit_full,
            max_short_results=max_short_results,
            reload_existing=args.reload_existing,
            query_delay_seconds=args.query_delay_seconds,
        )
        return 0

    summaries: list[QueryRunSummary] = []
    failures: list[QueryFailure] = []
    client: Any | None = None

    print("AI Tender Radar search profile")
    print(f"Profile: {args.profile}")
    print(f"Packs: {', '.join(pack_names)}")
    print(f"Query delay seconds: {args.query_delay_seconds:g}")

    try:
        for idx, spec in enumerate(specs, start=1):
            print(f"\n=== Pack: {spec.pack} | Query: {spec.query} ===")
            try:
                if client is None:
                    client = create_z360_client()

                stats = collect_with_client(
                    client=client,
                    query=spec.query,
                    days_back=days_back,
                    limit_full=limit_full,
                    min_price=min_price,
                    reload_existing=args.reload_existing,
                    max_short_results=max_short_results,
                )
                summaries.append(summary_from_collection(spec.pack, stats))
            except Exception as exc:
                reason = failure_reason(exc)
                failures.append(QueryFailure(pack=spec.pack, query=spec.query, reason=reason))
                print(f"Query failed: {reason}")

            if idx < len(specs):
                sleep_between_queries(args.query_delay_seconds)
    finally:
        close_z360_client(client)

    if not summaries:
        print("\nRule-based scoring skipped because no queries succeeded.")
        print_summary(
            summaries,
            scoring_enabled=False,
            failures=failures,
            token_login_retries=token_login_retry_count(client),
        )
        return 1

    scoring_enabled = bool(summaries) and not args.skip_score
    if args.skip_score:
        print("\nRule-based scoring пропущен из-за --skip-score.")
    else:
        print("\nЗапускаю rule-based scorer...")
        from app.scorer import main as run_scorer

        run_scorer()

    enrich_with_rule_based_metrics(summaries)
    print_summary(
        summaries,
        scoring_enabled=scoring_enabled,
        failures=failures,
        token_login_retries=token_login_retry_count(client),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
