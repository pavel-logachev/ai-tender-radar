"""Short Telegram lead card rendered from a grounded agent result."""
from __future__ import annotations

import html
import json

LIMIT = 3900  # Telegram text limit is 4096; keep headroom for the link footer
_MARKERS = ("<|", "[inst]", "[/inst]", "<think", "</think", "<tool_call", "<function_call", "<scratchpad")


def deliverable(result: dict) -> bool:
    """Only real leads with a verified phone go to managers: a caller needs a number."""
    return result.get("verdict") == "lead" and result.get("grade") in ("A", "B")


def _e(value) -> str:
    return html.escape(str(value), quote=False)


def _clip(value, size: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= size else text[:size - 1].rstrip() + "…"


def _build(result: dict, source_url: str | None, hooks: int, questions: int) -> str:
    signal, customer = result.get("signal") or {}, result.get("customer") or {}
    talk = result.get("talk_track") or {}
    lines = [f"<b>Лид {_e(result.get('grade') or '-')}</b> · {_e(_clip(customer.get('name') or '', 120))}",
             _e(_clip(result.get("one_line") or "", 400)), ""]
    if signal.get("what_they_buy"):
        lines.append(f"<b>Что покупают:</b> {_e(_clip(signal['what_they_buy'], 380))}")
    if signal.get("deadline"):
        lines.append(f"<b>Срок:</b> {_e(_clip(signal['deadline'], 120))}")
    economics = customer.get("economics_short") or customer.get("economics")
    if economics:
        lines.append(f"<b>Заказчик:</b> {_e(_clip(economics, 300))}")
    lines += ["", "<b>Кому звонить</b>"]
    for contact in (result.get("contacts") or [])[:3]:
        if not (contact.get("phone") or contact.get("email") or contact.get("name")):
            continue
        name = contact.get("name")
        head = _e(name) if name else _e(_clip(contact.get("role") or "контакт", 80))
        if name and contact.get("role"):
            head += f" — {_e(_clip(contact['role'], 100))}"
        line = "• " + head
        if contact.get("phone"):
            line += f"\n  ☎ {_e(_clip(contact['phone'], 90))}"
        if contact.get("email"):
            line += f"  ✉ {_e(contact['email'])}"
        lines.append(line)
    if talk.get("opening"):
        lines += ["", f"<b>Начать так:</b> {_e(_clip(talk['opening'], 300))}"]
    if talk.get("hooks") and hooks:
        lines += ["", "<b>Зацепки</b>"] + [f"• {_e(_clip(h, 220))}" for h in talk["hooks"][:hooks]]
    if talk.get("questions") and questions:
        lines += ["", "<b>Спросить</b>"] + [f"• {_e(_clip(q, 200))}" for q in talk["questions"][:questions]]
    if result.get("important"):
        lines += ["", f"⚠️ <b>Важно:</b> {_e(_clip(result['important'], 300))}"]
    footer = ""
    if source_url and source_url.startswith("https://"):
        footer = f'\n\n<a href="{html.escape(source_url, quote=True)}">Открыть закупку</a>'
    return "\n".join(lines) + footer


def render_card(result: dict, source_url: str | None = None) -> str:
    """Drop the least important sections (never cut inside a tag) until the card fits."""
    raw = json.dumps(result, ensure_ascii=False).lower()  # check before HTML escaping hides the markers
    if any(marker in raw for marker in _MARKERS):
        raise ValueError("card contains internal model markers")
    for hooks, questions in ((4, 3), (3, 2), (2, 1), (1, 1), (0, 0)):
        text = _build(result, source_url, hooks, questions)
        if len(text) <= LIMIT:
            break
    else:
        raise ValueError("card exceeds Telegram limit")
    return text
