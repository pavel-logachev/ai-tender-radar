"""Batch runner: one agent investigation per new/changed procurement, stored once, never repeated.

Operator invocation; performs paid model calls and web research, sends nothing to Telegram.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from agent_radar.lead_agent import agent as A
from agent_radar.lead_agent.card import deliverable, render_card
from agent_radar.lead_agent.grounding import ground_result
from agent_radar.lead_agent.store import LeadStore, version

MAX_ATTEMPTS = 2
DOCUMENT_WAIT_HOURS = 24  # a purchase closing sooner is researched without its queued files
_RANK = {"A": 0, "B": 1, "C": 2, None: 3}


def _rows(source_path: Path) -> list[dict]:
    if source_path.is_symlink() or not source_path.is_file() or source_path.stat().st_size > 20_000_000:
        raise ValueError("invalid analysis source file")
    data = json.loads(source_path.read_text(encoding="utf-8"))
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise ValueError("analysis source has no rows")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("card"), dict)]


def _deadline(row: dict) -> str:
    return str((row.get("opportunity") or {}).get("acceptance_end_date") or "9999")


def _documents_pending(row: dict) -> bool:
    return any(isinstance(gap, dict) and gap.get("reason") == "run_download_budget" for gap in row.get("gaps") or [])


def select(rows: list[dict], store: LeadStore, *, include_history: bool, now: datetime) -> list[dict]:
    """New procurement versions only; closed or expired procedures are history unless explicitly requested."""
    chosen = []
    for row in rows:
        if store.has(row["card"]["id"], version(row)):
            continue
        opportunity = row.get("opportunity") or {}
        active = opportunity.get("status") == "proposal" and _deadline(row) > now.strftime("%Y-%m-%dT%H:%M:%SZ")
        # Files the source has queued arrive within a few refreshes and make a new version: researching now
        # would pay twice and send two cards. Wait for them unless the deadline is close.
        if _documents_pending(row) and _deadline(row) > (now + timedelta(hours=DOCUMENT_WAIT_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ"):
            continue
        if active or include_history:
            chosen.append(row)
    chosen.sort(key=_deadline)  # most urgent first
    return chosen


def _investigate(row: dict, *, api_key: str, model: str, run: Callable[..., A.AgentRun]) -> tuple[A.AgentRun, dict | None]:
    outcome = run(row, api_key=api_key, model=model)
    result = A.extract_json(outcome.final_text)
    if result is not None:
        result = ground_result(result, outcome.tool_text)
    return outcome, result


def process(source_path: Path, store: LeadStore, *, api_key: str, model: str = A.DEFAULT_MODEL,
            fallback_model: str | None = A.FALLBACK_MODEL, limit: int = 10, max_run_cost: float = 1.0,
            include_history: bool = False, run: Callable[..., A.AgentRun] = A.run_agent,
            now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    counts = {"selected": 0, "saved": 0, "deliverable": 0, "failed": 0, "retried_with_fallback": 0, "cost_usd": 0.0}
    queue = select(_rows(source_path), store, include_history=include_history, now=now)
    counts["selected"] = len(queue)
    for row in queue[:max(0, limit)]:
        if counts["cost_usd"] >= max_run_cost:
            break
        card = row["card"]
        outcome, result = _investigate(row, api_key=api_key, model=model, run=run)
        counts["cost_usd"] += outcome.cost_usd
        used_model, tokens, run_cost = model, [outcome.tokens_in, outcome.tokens_out, outcome.tool_calls], outcome.cost_usd
        # A weaker grade, or a reject by the cheap model, gets one second opinion from the fallback model.
        if fallback_model and (result is None or result.get("grade") != "A"):
            second, second_result = _investigate(row, api_key=api_key, model=fallback_model, run=run)
            counts["cost_usd"] += second.cost_usd
            counts["retried_with_fallback"] += 1
            run_cost += second.cost_usd
            tokens = [tokens[0] + second.tokens_in, tokens[1] + second.tokens_out, tokens[2] + second.tool_calls]
            if second_result is not None and (result is None or _RANK[second_result.get("grade")] < _RANK[result.get("grade")]):
                result, used_model = second_result, fallback_model
        if result is None:
            if store.bump_attempt(card["id"], version(row)) < MAX_ATTEMPTS:
                counts["failed"] += 1
                continue
            result = {"verdict": "failed", "grade": None, "one_line": "Агент не вернул разбираемый результат"}
        if store.delivered_before(card["id"]):
            result["update"] = True  # the card says it replaces one the managers already have
        ok = deliverable(result)
        try:
            text = render_card(result, card.get("source_url")) if ok else None
        except ValueError:
            text, ok = None, False
        saved = store.save(tender_id=card["id"], fingerprint=version(row), model=used_model,
                           cost_usd=round(run_cost, 5), tokens_in=tokens[0], tokens_out=tokens[1], tool_calls=tokens[2],
                           result=result, card_html=text, deliverable=ok,
                           deadline=(row.get("opportunity") or {}).get("acceptance_end_date"))
        if saved:
            counts["saved"] += 1
            counts["deliverable"] += int(ok)
    counts["cost_usd"] = round(counts["cost_usd"], 4)
    return counts


def _heartbeat(state_root: Path, fields: dict) -> None:
    """Outcome of the latest run, read by the health check: a silent research stage must be noticed."""
    path = state_root / "leads-research.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **fields}),
                         encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="path to analysis-source.json")
    parser.add_argument("--state-root", required=True, help="private directory for leads.sqlite3")
    parser.add_argument("--model", default=A.DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=A.FALLBACK_MODEL)
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-run-cost", type=float, default=1.0)
    parser.add_argument("--include-history", action="store_true")
    args = parser.parse_args(argv)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("OPENROUTER_API_KEY is required", file=sys.stderr)
        return 2
    state_root = Path(args.state_root)
    store = LeadStore(state_root / "leads.sqlite3")
    try:
        counts = process(Path(args.source), store, api_key=api_key, model=args.model,
                         fallback_model=None if args.no_fallback else args.fallback_model, limit=args.limit,
                         max_run_cost=args.max_run_cost, include_history=args.include_history)
    except Exception as error:
        _heartbeat(state_root, {"error": type(error).__name__})
        raise
    _heartbeat(state_root, counts)
    print(json.dumps({**counts, **store.stats()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
