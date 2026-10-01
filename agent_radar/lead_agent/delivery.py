"""One-shot delivery of stored lead cards to subscribed private chats.

Claim-before-send with at-most-once semantics: an uncertain outcome is never retried automatically.
A flood-control rejection (HTTP 429) is definitive non-delivery, so that claim is released and the run stops.
Creates no poller and calls no model.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Callable

from agent_radar.lead_agent import feedback
from agent_radar.lead_agent.store import LeadStore


async def deliver(store: LeadStore, bot, recipients: list[int], *, per_chat_limit: int = 5, pause: float = 0.4,
                  keyboard: Callable = feedback.keyboard) -> dict:
    counts = {"sent": 0, "uncertain": 0, "rate_limited": 0}
    for chat_id in recipients:
        for lead in store.undelivered(chat_id, limit=per_chat_limit):
            if not store.claim(lead["id"], chat_id):
                continue
            try:
                receipt = await bot.send_message(chat_id=chat_id, text=lead["card_html"], parse_mode="HTML",
                                                 disable_web_page_preview=True, reply_markup=keyboard(lead["id"]))
                store.record_sent(lead["id"], chat_id, receipt.message_id)
                counts["sent"] += 1
            except Exception as error:
                if getattr(error, "retry_after", None) is not None:
                    store.release(lead["id"], chat_id)
                    counts["rate_limited"] += 1
                    break
                store.record_uncertain(lead["id"], chat_id)
                counts["uncertain"] += 1
            await asyncio.sleep(pause)
    return counts


async def _main(state_root: Path, per_chat_limit: int) -> dict:
    from telegram import Bot
    from telegram.request import HTTPXRequest
    from agent_radar.digest_store import DigestStore
    allowed = {int(item) for item in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if item.strip().isdigit()}
    recipients = DigestStore(state_root / "digests.sqlite3").recipients(allowed)
    store = LeadStore(state_root / "leads.sqlite3")
    # Same proxy variable and timeouts as the native digest sender inside the host container.
    request = HTTPXRequest(proxy=os.getenv("TELEGRAM_PROXY") or None, connect_timeout=30, read_timeout=30,
                           write_timeout=30, pool_timeout=30)
    async with Bot(os.environ["TELEGRAM_BOT_TOKEN"], request=request) as bot:
        return await deliver(store, bot, recipients, per_chat_limit=per_chat_limit)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--per-chat-limit", type=int, default=5)
    args = parser.parse_args(argv)
    result = asyncio.run(_main(Path(args.state_root), args.per_chat_limit))
    print(json.dumps(result, sort_keys=True))
    return 1 if result["uncertain"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
