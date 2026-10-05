"""Portable Telegram lead workflow registered in a caller-owned application.

This module never creates a Bot/Application, starts polling or calls a model.
The adapter supplies is_authorized(message), should_process(message, is_command=...),
async build_control_event(message, update) and async handle_message(event).
Session management and runtime registration stay with the caller.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from telegram import ReplyKeyboardMarkup

ROWS = (("Выгрузить новые лиды", "Выгрузить в работе"),)
# Public command list; control commands stay with the caller.
COMMANDS = (
    ("leads", "Выгрузить новые лиды"),
    ("work", "Выгрузить лиды в работе"),
    ("guide", "Как это работает"),
)
WELCOME = (
    "Нахожу поставки серверов и СХД и готовлю лиды: кому звонить и о чём говорить.\n"
    "Карточки приходят сами по будням в 9:00, 12:00 и 15:00 по Москве. Под каждой две кнопки: «В работу» и «Мимо». Ответом на карточку можно оставить заметку.\n"
    "«Выгрузить новые лиды»: таблица всех карточек, на которые вы ещё не нажимали кнопки.\n"
    "«Выгрузить в работе»: таблица лидов, которые вы взяли в работу.\n"
    "Если нужно, напишите вопрос по закупкам."
)
HELP = (
    "Как это работает\n\n"
    "Агент сам читает закупки, оценивает заказчика и ищет, кому звонить. Карточки приходят по будням в 9:00, 12:00 и 15:00 по Москве: "
    "заказчик, что покупают, имя и телефон, зацепки и вопросы для разговора.\n"
    "Под карточкой: «В работу»: взяли на себя, «Мимо»: не подходит. Нажатие можно исправить. Заметку после звонка напишите ответом (reply) на карточку: она сохранится и появится в колонке «Комментарий» в обеих таблицах (то, что вписано в Excel вручную, при новой выгрузке не сохраняется).\n\n"
    "«Выгрузить новые лиды»: всё, что пришло, но вы ещё не отметили кнопкой: удобно, если долго не заходили в чат.\n"
    "«Выгрузить в работе»: лиды, которые вы взяли в работу. Таблицы формируются в момент нажатия.\n\n"
    "Отключить карточки: /digest_off, включить снова: /digest_on.\n"
    "Контакты берутся только из опубликованных источников. Карточка помогает подготовиться к разговору; "
    "перед звонком проверяйте срок приёма предложений."
)
EXPORT_EMPTY = {
    "work": "Пока нет лидов в работе. Нажимайте «В работу» под карточками, и они появятся в таблице.",
    "new": "Новых лидов нет: на все присланные карточки вы уже нажимали кнопки.",
}


def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(ROWS, resize_keyboard=True, is_persistent=True,
                               one_time_keyboard=False, input_field_placeholder="Карточки приходят сами; кнопки ниже собирают Excel")


ACTION_BY_TEXT = {"Выгрузить новые лиды": "export_new", "Выгрузить в работе": "export_work"}
ACTION_BY_COMMAND = {"start": "menu", "menu": "menu", "help": "guide", "guide": "guide", "leads": "export_new", "new": "export_new",
                     "work": "export_work", "export": "export_work", "data": "data", "dialog": "dialog", "halt": "halt"}


def action_for(text: str) -> str | None:
    if text in ACTION_BY_TEXT:
        return ACTION_BY_TEXT[text]
    return ACTION_BY_COMMAND.get(text.split(maxsplit=1)[0].split("@", 1)[0].lstrip("/")) if text.startswith("/") else None


class MenuController:
    """Reuse the caller's authorization and command dispatch."""
    def __init__(self, adapter, bundle_root: str | Path = ".", subscriptions=None, freshness_reader=None):
        self.adapter = adapter
        self.bundle_root = Path(bundle_root)
        self.subscriptions = subscriptions
        self.freshness_reader = freshness_reader

    async def observe(self, update, context):
        message = update.effective_message
        if (self.subscriptions is not None and message is not None
                and message.chat.type == "private" and self.allowed_user(message)
                and message.chat.id == message.from_user.id):
            await asyncio.to_thread(self.subscriptions.touch, message.chat.id, message.from_user.id)

    async def subscription(self, update, context):
        message = update.effective_message
        if (message is None or message.chat.type != "private" or not self.allowed_user(message)
                or not self.adapter.is_authorized(message)):
            return
        enabled = (message.text or "").split()[0].split("@",1)[0] == "/digest_on"
        if self.subscriptions is None:
            await message.reply_text("Карточки пока недоступны; попробуйте позже.")
            return
        if enabled:
            await asyncio.to_thread(self.subscriptions.subscribe, message.chat.id, message.from_user.id)
        else:
            await asyncio.to_thread(self.subscriptions.unsubscribe, message.chat.id)
        await message.reply_text("Карточки лидов включены." if enabled else "Карточки лидов отключены. Включить снова: /digest_on.", reply_markup=keyboard())

    async def handle(self, update, context) -> None:
        message = update.effective_message
        if (message is None or not message.text or getattr(message.chat, "type", None) != "private"
                or not self.allowed_user(message)
                or not self.adapter.is_authorized(message)
                or not self.adapter.should_process(message, is_command=message.text.startswith("/"))):
            return
        action = action_for(message.text)
        if action is None:
            return
        if action in {"export_new", "export_work"}:
            await self.export(message, "new" if action == "export_new" else "work")
            return
        if action in {"menu", "guide", "data"}:
            reply = {"menu": WELCOME, "guide": HELP}.get(action)
            if action == "data":
                reply = await asyncio.to_thread(self.freshness)
            await message.reply_text(reply, reply_markup=keyboard(), parse_mode=None)
            return
        event = await self.adapter.build_control_event(message, update)
        event.text = "/new" if action == "dialog" else "/stop"
        await self.adapter.handle_message(event)

    async def note(self, update, context) -> None:
        """A reply to a delivered lead card is the manager's note (call result etc.); it fills the Excel "Комментарий"."""
        message = update.effective_message
        root = os.getenv("RADAR_DIGEST_STATE_ROOT")
        if (message is None or not message.text or getattr(message.chat, "type", None) != "private" or not root
                or message.reply_to_message is None or not self.allowed_user(message)
                or not self.adapter.is_authorized(message)):
            return
        from agent_radar.lead_agent.store import LeadStore
        store = LeadStore(Path(root) / "leads.sqlite3")
        saved = await asyncio.to_thread(store.add_comment, message.chat.id, message.from_user.id,
                                        message.reply_to_message.message_id, message.text)
        await message.reply_text("Заметка записана, она появится в колонке «Комментарий» в таблицах." if saved
                                 else "Не удалось записать заметку к этой карточке.")

    async def export(self, message, kind: str = "work") -> None:
        """Excel on demand: "work" = leads this manager took in work, "new" = delivered cards not answered yet."""
        import io
        from datetime import datetime, timedelta, timezone
        root = os.getenv("RADAR_DIGEST_STATE_ROOT")
        if not root:
            await message.reply_text("Выгрузка пока недоступна; попробуйте позже.", reply_markup=keyboard())
            return

        def build():
            from agent_radar.lead_agent import export as lead_export
            from agent_radar.lead_agent.store import LeadStore
            store = LeadStore(Path(root) / "leads.sqlite3")
            user = message.from_user.id
            items = store.in_work(user) if kind == "work" else store.new_leads(message.chat.id, user)
            return len(items), (lead_export.build_workbook(items, kind=kind) if items else None)
        count, payload = await asyncio.to_thread(build)
        if not count:
            await message.reply_text(EXPORT_EMPTY[kind], reply_markup=keyboard())
            return
        day = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
        name = "leady-v-rabote" if kind == "work" else "leady-novye"
        caption = f"Лидов в работе: {count}" if kind == "work" else f"Новых лидов: {count}"
        await message.reply_document(document=io.BytesIO(payload), filename=f"{name}-{day}.xlsx", caption=caption,
                                     reply_markup=keyboard())

    @staticmethod
    def allowed_user(message) -> bool:
        # Pairing prefilters can admit unknown private users. Require an explicit
        # numeric allowlist before exposing lead or source metadata.
        raw = os.getenv("TELEGRAM_ALLOWED_USERS", "")
        parts = [item.strip() for item in raw.split(",")]
        if not parts or any(not re.fullmatch(r"[1-9][0-9]*", item) for item in parts):
            return False
        user = getattr(message, "from_user", None)
        return user is not None and not getattr(user, "is_bot", False) and str(user.id) in parts

    def freshness(self) -> str:
        from datetime import datetime, timezone
        try:
            if self.freshness_reader is None:
                raise ValueError("a freshness reader must be supplied by the caller")
            result = self.freshness_reader(self.bundle_root)
            collected = result.get("source_collected_at")
            start = result.get("source_window_from")
            if not collected or not start:
                return ("Время получения данных пока не подтверждено.\n"
                        "Могу разобрать доступные карточки, но не утверждаю, что они актуальны.\n"
                        "Обновление через Bidzaar API каждые 30 минут; документы ИТ-закупок читаются при доступности.")
            stamp = datetime.fromisoformat(collected.replace("Z", "+00:00"))
            window = datetime.fromisoformat(start.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            stale = "\n⚠ Данные старше 45 минут: возможно, обновление задержалось. Не считайте их текущими.\n" if age > 2700 else "\n"
            return (f"Последнее успешное получение: {stamp:%d.%m.%Y %H:%M} UTC.\n"
                    f"Свежие изменения с {window:%d.%m.%Y %H:%M} UTC; серверы и СХД, накопление за 14 дней.\n"
                    f"{stale}Источник: Bidzaar API; проверка каждые 30 минут.\n"
                    "Это не полный список активных закупок. Документы поставок серверов/СХД читаются, если доступны; исторические сигналы и пробелы отмечаются.")
        except (ValueError, OSError, TypeError, KeyError):
            return ("Не удалось проверить актуальность данных.\n"
                    "Попробуйте позже или напишите вопрос; буду отвечать только по доступным карточкам, без обещания свежести.")


def wire_telegram_menu(application, adapter, *, freshness_reader=None) -> None:
    """Register before the caller's catch-alls; reuse its application and authorization."""
    from telegram.ext import MessageHandler, CommandHandler, filters
    from agent_radar.subscriptions import Subscriptions
    # Keep the subscription database name for compatibility with earlier installations.
    subscriptions = Subscriptions(Path(os.environ["RADAR_DIGEST_STATE_ROOT"])/"digests.sqlite3") if os.getenv("RADAR_DIGEST_STATE_ROOT") else None
    controller = MenuController(adapter, subscriptions=subscriptions, freshness_reader=freshness_reader)
    if os.getenv("RADAR_LEAD_FEEDBACK") == "1" and os.getenv("RADAR_DIGEST_STATE_ROOT"):
        # Opt-in: manager feedback buttons under lead cards, same poller and allowlist.
        from agent_radar.lead_agent.feedback import wire_feedback
        wire_feedback(application, os.environ["RADAR_DIGEST_STATE_ROOT"])
        from telegram.ext.filters import MessageFilter
        from agent_radar.lead_agent.store import LeadStore
        lead_store = LeadStore(Path(os.environ["RADAR_DIGEST_STATE_ROOT"]) / "leads.sqlite3")

        class LeadCardReply(MessageFilter):
            """Only replies to a delivered lead card; every other reply stays with the caller's freeform flow."""
            def filter(self, message):
                reply = message.reply_to_message
                return bool(reply and message.text and lead_store.has_card(message.chat.id, reply.message_id))
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & LeadCardReply(),
                                               controller.note))
    if subscriptions is not None:
        # Observe after the caller's group 0 dispatch without consuming commands or freeform input.
        # Existing opt-outs stay off. The caller must authorize updates before dispatch.
        application.add_handler(MessageHandler(filters.ChatType.PRIVATE, controller.observe), group=98)
        application.add_handler(CommandHandler(["digest_on", "digest_off"], controller.subscription, filters=filters.ChatType.PRIVATE))
    # Same group as core: the first matching handler consumes only menu actions.
    pattern = r"^(?:" + "|".join(re.escape(label) for row in ROWS for label in row) + r")\Z"
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & filters.Regex(pattern), controller.handle))
    application.add_handler(CommandHandler(list(ACTION_BY_COMMAND), controller.handle, filters=filters.ChatType.PRIVATE))
