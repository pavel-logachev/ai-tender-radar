"""Inline feedback buttons under lead cards in a caller-owned Telegram application.

No second bot or poller: ``wire_feedback`` registers one CallbackQueryHandler in the caller's existing
``application``. Only explicitly allowed private users may press buttons, and only for leads that were
actually delivered to their chat.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from agent_radar.lead_agent.store import ACTIONS, LeadStore

PATTERN = r"^lf:[1-9][0-9]{0,9}:(?:work|skip)$"
_LAYOUT = (("work", "skip"),)


def parse(data: str) -> tuple[int, str] | None:
    match = re.fullmatch(r"lf:([1-9][0-9]{0,9}):(work|skip)", data or "")
    return (int(match.group(1)), match.group(2)) if match else None


def button_rows(lead_id: int, done: set[str] | list[str] = ()) -> list[list[tuple[str, str]]]:
    """(label, callback_data) rows; the last verdict gets a check mark and either button stays pressable to correct it."""
    done = set(done)
    return [[(("✓ " if code in done else "") + ACTIONS[code], f"lf:{lead_id}:{code}") for code in row] for row in _LAYOUT]


def keyboard(lead_id: int, done=()):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=data) for text, data in row]
                                 for row in button_rows(lead_id, done)])


def _allowed(user) -> bool:
    from agent_radar.telegram_menu import MenuController
    return MenuController.allowed_user(type("Holder", (), {"from_user": user})())


async def on_callback(update, context, *, store: LeadStore) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse(query.data)
    message, user = query.message, query.from_user
    if (parsed is None or message is None or user is None or message.chat.type != "private"
            or message.chat.id != user.id or not _allowed(user)):
        await query.answer()
        return
    lead_id, action = parsed
    recorded = await asyncio.to_thread(store.add_feedback, lead_id, message.chat.id, user.id, action)
    if not recorded:
        await query.answer("Не удалось записать: карточка не найдена", show_alert=False)
        return
    done = (await asyncio.to_thread(store.feedback, lead_id))[-1:]  # the latest verdict wins
    await query.answer("Записано")
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard(lead_id, done))
    except Exception:  # an unchanged markup or a deleted message must not lose the recorded answer
        pass


def wire_feedback(application, state_root: str | Path) -> None:
    from telegram.ext import CallbackQueryHandler
    store = LeadStore(Path(state_root) / "leads.sqlite3")

    async def handler(update, context):
        await on_callback(update, context, store=store)

    application.add_handler(CallbackQueryHandler(handler, pattern=PATTERN))
