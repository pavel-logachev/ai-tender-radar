"""Operator health check: tells the admin chat when the radar stops producing leads, and why.

Reads operator-supplied state and calls no model. Telegram access is configured by the operator.
A problem is reported when it appears, repeated once a day while it lasts, and closed with a recovery note.
With --slot it also sends one quiet status line when a delivery slot produced no cards for the admin, so that
"no new leads" can be told apart from "the radar is broken". Managers never receive these messages.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_radar.lead_agent.store import version

SOURCE_STALE_HOURS = 2      # the source timer fires every 30 minutes
RESEARCH_STALE_HOURS = 20   # research runs three times a day; the longest planned gap is 18 hours
REMIND_HOURS = 24
MSK = timezone(timedelta(hours=3))
_SECRET_LIKE = re.compile(r"https?://\S+|[A-Za-z0-9_\-]{20,}")


def _time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(path: Path, limit: int = 20_000_000):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _msk(moment: datetime) -> str:
    return moment.astimezone(MSK).strftime("%d.%m %H:%M")


def _hours(moment: datetime, now: datetime) -> int:
    return int((now - moment).total_seconds() // 3600)


def safe_error(text: str | None) -> str:
    """Last error line of a unit, with URLs and long tokens removed: a log line is not trusted to be secret-free."""
    return _SECRET_LIKE.sub("…", " ".join(str(text or "").split()))[:200]


def problems(bundle_root: Path, state_root: Path, now: datetime, *, source_error: str | None = None) -> dict[str, str]:
    """Current problems by stable key; the text is what the admin reads."""
    found: dict[str, str] = {}
    refresh = _json(bundle_root / "source-refresh.json", 10_000) or {}
    collected = _time(refresh.get("collected_at"))
    if collected is None:
        found["source"] = "Источник Bidzaar: нет данных об обновлении. Новые закупки не поступают."
    elif now - collected > timedelta(hours=SOURCE_STALE_HOURS):
        detail = safe_error(source_error)
        found["source"] = (f"Источник Bidzaar не обновляется {_hours(collected, now)} ч (последнее обновление "
                           f"{_msk(collected)} МСК). Новые закупки не поступают, карточек не будет."
                           + (f"\nОшибка: {detail}" if detail else ""))
    research = _json(state_root / "leads-research.json", 10_000) or {}
    ran = _time(research.get("at"))
    if ran is not None and now - ran > timedelta(hours=RESEARCH_STALE_HOURS):
        found["research"] = f"Исследование лидов не запускалось {_hours(ran, now)} ч (последний запуск {_msk(ran)} МСК)."
    elif ran is not None and research.get("error"):
        found["research"] = (f"Исследование лидов упало с ошибкой {safe_error(research['error'])} "
                             f"(запуск {_msk(ran)} МСК). Новые закупки не разобраны.")
    elif ran is not None and research.get("selected") and not research.get("saved") and research.get("failed"):
        found["research"] = (f"Агент не смог разобрать ни одной закупки из {research['selected']} "
                             f"(запуск {_msk(ran)} МСК). Вероятно, недоступна модель: ключ, лимит или прокси OpenRouter.")
    database = state_root / "leads.sqlite3"
    if database.is_file() and not database.is_symlink():
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)) as connection:
            stuck = connection.execute(
                "SELECT COUNT(*) FROM deliveries WHERE state='uncertain' OR (state='claimed' AND updated_at<?)",
                ((now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()[0]
        if stuck:
            found["delivery"] = (f"Карточек с неизвестным результатом отправки: {stuck}. Повторно они не отправляются; "
                                 "проверьте чат и журнал доставки.")
    return found


def quarantined(bundle_root: Path) -> dict[str, str]:
    """Purchases that passed the hardware gate but could not be normalized (kept by the source across passes)."""
    rows = (_json(bundle_root / "source-quarantine.json", 1_000_000) or {}).get("items") or []
    return {str(row.get("id")): safe_error(row.get("reason")) for row in rows if isinstance(row, dict) and row.get("id")}


def plan(previous: dict, current: dict[str, str], skipped: dict[str, str], now: datetime) -> tuple[list[str], dict]:
    """Messages to send now and the state to keep. Pure: no clock, files or network."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    messages, state = [], {"problems": {}, "quarantined": []}
    known = previous.get("problems") or {}
    for key, text in current.items():
        last = _time((known.get(key) or {}).get("alerted"))
        if last is None or now - last >= timedelta(hours=REMIND_HOURS):
            messages.append(("⚠️ Радар: " if last is None else "⚠️ Радар, проблема не устранена: ") + text)
            last = now
        state["problems"][key] = {"since": (known.get(key) or {}).get("since") or stamp,
                                  "alerted": last.strftime("%Y-%m-%dT%H:%M:%SZ")}
    recovered = [key for key in known if key not in current]
    if recovered:
        names = {"source": "источник Bidzaar обновляется", "research": "исследование лидов работает",
                 "delivery": "доставка карточек без зависших отправок"}
        messages.append("✅ Радар восстановлен: " + "; ".join(names.get(key, key) for key in recovered) + ".")
    seen = [key for key in (previous.get("quarantined") or []) if isinstance(key, str)]
    fresh = [key for key in skipped if key not in seen]
    if fresh:
        lines = [f"• № {key.split(':', 1)[-1]}: {skipped[key] or 'неизвестная ошибка'}" for key in fresh[:10]]
        messages.append("⚠️ Радар: закупка похожа на профильную, но источник не смог её разобрать. "
                        "Остальные закупки обработаны, эту стоит посмотреть вручную на Bidzaar.\n" + "\n".join(lines))
    state["quarantined"] = (seen + fresh)[-200:]
    return messages, state


def slot_line(bundle_root: Path, state_root: Path, admin_chat: int, now: datetime) -> str | None:
    """One status line for a delivery slot that sent the admin nothing; None when cards were just delivered."""
    database = state_root / "leads.sqlite3"
    done: set[tuple[str, str]] = set()
    if database.is_file() and not database.is_symlink():
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10)) as connection:
            recent = connection.execute("SELECT COUNT(*) FROM deliveries WHERE chat_id=? AND updated_at>=?",
                                        (admin_chat, (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchone()[0]
            if recent:
                return None
            done = set(connection.execute("SELECT tender_id, fingerprint FROM leads").fetchall())
    source = _json(bundle_root / "analysis-source.json") or {}
    collected = _time(source.get("collected_at"))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    active = waiting = 0
    for row in source.get("rows") or []:
        opportunity = row.get("opportunity") or {}
        if opportunity.get("status") != "proposal" or str(opportunity.get("acceptance_end_date") or "9999") <= stamp:
            continue
        active += 1
        card = row.get("card")
        waiting += not (isinstance(card, dict) and (card.get("id"), version(row)) in done)
    updated = f"источник обновлён {_msk(collected)} МСК" if collected else "источник ещё не обновлялся"
    tail = f"ждут исследования: {waiting}" if waiting else "все разобраны"
    return f"Радар · {now.astimezone(MSK):%H:%M}: новых лидов нет. Активных профильных закупок: {active}, {tail}; {updated}."


async def _send(texts: list[tuple[str, bool]], admin_chat: int) -> None:
    from telegram import Bot
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(proxy=os.getenv("TELEGRAM_PROXY") or None, connect_timeout=30, read_timeout=30,
                           write_timeout=30, pool_timeout=30)
    async with Bot(os.environ["TELEGRAM_BOT_TOKEN"], request=request) as bot:
        for text, quiet in texts:
            await bot.send_message(chat_id=admin_chat, text=text[:3900], disable_web_page_preview=True,
                                   disable_notification=quiet)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--bundle-root", required=True)
    parser.add_argument("--source-error", default="", help="last error line of the source unit, if it failed")
    parser.add_argument("--slot", action="store_true", help="also report an empty delivery slot to the admin")
    args = parser.parse_args(argv)
    state_root, bundle_root = Path(args.state_root), Path(args.bundle_root)
    now = datetime.now(timezone.utc)
    admin = os.environ.get("RADAR_ADMIN_CHAT", "").strip()
    current = problems(bundle_root, state_root, now, source_error=args.source_error)
    state_path = state_root / "health.json"
    messages, state = plan(_json(state_path, 100_000) or {}, current, quarantined(bundle_root), now)
    texts = [(text, False) for text in messages]
    if args.slot and admin.isdigit() and not current:
        line = slot_line(bundle_root, state_root, int(admin), now)
        if line:
            texts.append((line, True))
    if texts and admin.isdigit():
        asyncio.run(_send(texts, int(admin)))
    # Saved only after a successful send: a failed alert is retried on the next check.
    temporary = state_path.with_name(state_path.name + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, state_path)
    print(json.dumps({"problems": sorted(current), "sent": len(texts) if admin.isdigit() else 0,
                      "admin_configured": admin.isdigit()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
