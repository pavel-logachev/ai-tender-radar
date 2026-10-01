import asyncio
import inspect
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Any

import psycopg
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.config import settings
from app.digest import (
    DEFAULT_DEADLINE_MIN_DAYS,
    TenderDigestCard,
    TodayFilterAuditCard,
    build_operational_lead_all_cards_with_stats,
    build_operational_lead_in_work_cards_with_stats,
    build_operational_lead_queue_cards_with_stats,
    build_in_work_digest_cards_with_stats,
    build_digest_cards_today_with_stats,
    build_digest_cards_with_stats,
    build_today_filter_audit_cards_with_stats,
    build_tender_digest_card,
    build_recent_llm_digest_cards_with_stats,
    get_tender_digest_row,
    manual_document_reason_for_tender,
    operational_lead_all_summary_text,
    operational_lead_in_work_summary_text,
    operational_lead_queue_summary_text,
    tender_has_extracted_document_text,
    today_digest_summary_text,
    today_filter_audit_summary_text,
)
from app.excel_export import (
    EXCEL_EXPORT_ALL,
    EXCEL_EXPORT_BITRIX_IN_WORK,
    EXCEL_EXPORT_IN_WORK,
    EXCEL_EXPORT_TODAY,
    ExcelExportNoRows,
    build_excel_export_file,
)
from app.lead_notes import (
    LEAD_NOTE_TEXT_LIMIT,
    LeadNoteTooLong,
    normalize_lead_note_text,
    save_lead_note,
)
from app.llm.analysis_depth import ANALYSIS_DEPTH_DEEP, resolve_analysis_limits
from app.llm.tender_report import generate_presales_report
from app.pipeline.prepare_tender_for_analysis import (
    PreparationStatus,
    TenderPreparationResult,
    prepare_tender_for_analysis,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Не логируем URL запросов к Telegram API, потому что в них содержится bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)


MAX_TELEGRAM_MESSAGE_LEN = 3900
TELEGRAM_UI_FLOW_VERSION = "lead-existing-client-2026-06-25"
TELEGRAM_RECENT_DIGEST_HOURS = 24
TELEGRAM_BULK_CARD_DELAY_SECONDS = 1.1
TELEGRAM_RETRY_AFTER_MAX_SECONDS = 300.0
TELEGRAM_RETRY_AFTER_BUFFER_SECONDS = 0.5
TELEGRAM_NETWORK_RETRY_ATTEMPTS = 3
TELEGRAM_NETWORK_RETRY_BASE_SECONDS = 1.5
TELEGRAM_NETWORK_RETRY_MAX_SECONDS = 8.0
TELEGRAM_TRANSIENT_NETWORK_ERRORS = {"NetworkError", "TimedOut"}
LEAD_NOTE_PENDING_KEY = "pending_lead_note"
LEAD_NOTE_PENDING_TTL = timedelta(minutes=15)
LEAD_NOTE_PROMPT_TEXT = (
    "Напиши примечание к этому лиду одним сообщением. "
    "Оно попадет в Excel. /cancel - отменить."
)
LOG_PREVIEW_LEN = 140
LLM_REPORT_ANALYSIS_TYPE_PREFIX = "llm_presales_report"
ROUTERAI_TWO_PASS_META_VALUE = "routerai_two_pass"
ROUTERAI_PREFERRED_ANALYSIS_TYPES = {
    "llm_presales_report_routerai_two_pass",
    "llm_presales_report_routerai_fallback",
}
ROUTERAI_PREFERRED_MODELS = {
    "deepseek/deepseek-v4-pro",
}
SELECTION_SOURCE_ROUTERAI_EXISTING = "routerai_existing"
SELECTION_SOURCE_LEGACY_EXISTING = "legacy_existing"
SELECTION_SOURCE_GENERATE_NEW = "generate_new"
FULL_REPORT_SCHEMA_FIELDS = (
    "recommendation",
    "technical_spec_analysis",
    "tailoring_risk",
    "commercial_assessment",
    "vendor_fit",
    "risk_map",
    "margin_assessment",
    "questions",
)
ROUTERAI_TWO_PASS_SCHEMA_FIELDS = (
    "verdict",
    "what_is_required",
    "top_risks",
    "problems_in_tz",
)
PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION = """
(
    lower(coalesce(d.filename, '')) LIKE '%%техническое задание%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%описание объекта закупки%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%описание предмета закупки%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%техническая спецификация%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%спецификац%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%перечень оборудования%%'
    OR lower(coalesce(d.filename, '')) LIKE '%%ведомость оборудования%%'
    OR lower(coalesce(d.filename, '')) ~ '(^|[^[:alnum:]_а-яё])тз([^[:alnum:]_а-яё]|$)'
    OR lower(coalesce(d.filename, '')) ~ '(^|[^[:alnum:]_а-яё])ооз([^[:alnum:]_а-яё]|$)'
)
""".strip()
PAST_DATE_PATTERN = re.compile(
    r"\b(?:(?P<iso_y>\d{4})-(?P<iso_m>\d{1,2})-(?P<iso_d>\d{1,2})|"
    r"(?P<d>\d{1,2})[./-](?P<m>\d{1,2})[./-](?P<y>\d{2,4}))\b"
)
TELEGRAM_LLM_ANALYSIS_LOCK_KEY = 742_906_417_126_845_031
TELEGRAM_LLM_ANALYSIS_STALE_AFTER = timedelta(minutes=30)
TELEGRAM_LLM_ANALYSIS_STARTED = "telegram_llm_analysis_started"
TELEGRAM_LLM_ANALYSIS_SUCCEEDED = "telegram_llm_analysis_succeeded"
TELEGRAM_LLM_ANALYSIS_FAILED = "telegram_llm_analysis_failed"
TELEGRAM_LLM_ANALYSIS_EVENT_TYPES = (
    TELEGRAM_LLM_ANALYSIS_STARTED,
    TELEGRAM_LLM_ANALYSIS_SUCCEEDED,
    TELEGRAM_LLM_ANALYSIS_FAILED,
)
TELEGRAM_LLM_START_STARTED = "started"
TELEGRAM_LLM_START_ALREADY_READY = "already_ready"
TELEGRAM_LLM_START_SAME_RUNNING = "same_running"
TELEGRAM_LLM_START_BUSY_OTHER = "busy_other"
TELEGRAM_LLM_ALREADY_READY_MESSAGE = "Разбор по этой закупке уже готов."
TELEGRAM_LLM_SAME_RUNNING_MESSAGE = "Разбор уже выполняется. Повторно запускать не нужно."
TELEGRAM_LLM_BUSY_OTHER_MESSAGE = "Сейчас уже идет LLM-разбор другой закупки. Попробуйте позже."
TELEGRAM_LLM_GENERATION_DISABLED_MESSAGE = (
    "Новый LLM-разбор приостановлен, чтобы не расходовать платный лимит. "
    "Уже готовые отчеты, карточки и рабочие кнопки остаются доступны."
)
TELEGRAM_LLM_STARTED_MESSAGE_TEMPLATE = "Принял в работу: {external_id}. Разбор запущен."
TELEGRAM_LLM_SUCCESS_PREFIX = "🤖 Разбор готов"
TELEGRAM_DOCUMENT_NOT_READY_MESSAGE = (
    "Документы для LLM-разбора не готовы: требуется авторизация на внешней площадке / "
    "документы не найдены. Скачайте КД/ТЗ/спецификацию и загрузите документ вручную."
)
SALES_IN_WORK_STATUS = "sales_in_work"
SALES_NOT_RELEVANT_STATUS = "sales_not_relevant"
SALES_RETURNED_STATUS = "sales_returned"
SALES_EXISTING_CLIENT_STATUS = "sales_existing_client"
SALES_WORKFLOW_STATUS_LABELS = {
    SALES_IN_WORK_STATUS: "В работе",
    SALES_NOT_RELEVANT_STATUS: "Мимо",
    SALES_RETURNED_STATUS: "Возвращено в список",
    SALES_EXISTING_CLIENT_STATUS: "Существующий клиент",
}
SALES_WORKFLOW_REPLY_TEXTS = {
    SALES_IN_WORK_STATUS: "Отмечено: в работу. Закупка скрыта из рабочих списков.",
    SALES_NOT_RELEVANT_STATUS: (
        "Отмечено: мимо. Закупка скрыта из рабочих списков. "
        "Можно уточнить причину ниже."
    ),
    SALES_RETURNED_STATUS: "Закупка возвращена в рабочие списки.",
    SALES_EXISTING_CLIENT_STATUS: (
        "Отмечено: существующий клиент. Закупка скрыта из рабочих списков, "
        "а новые процедуры этого заказчика будут скрываться по точному имени клиента."
    ),
}


REJECT_REASON_STATUS_LABELS = {
    "bad_price": "Маленький чек",
    "bad_region": "Не наш регион",
    "not_profile": "Не наш профиль",
    "service_only": "Услуги/ПО без инфраструктуры",
    "low_value_one_off": "Разовая мелкая поставка",
    "bad_customer_fit": "Заказчик не подходит",
    "bad_timing": "Поздно / не сейчас",
    "duplicate_or_processed": "Дубль / уже обработали",
    "other_reject": "Другое",
}

REJECT_REASON_BUTTON_ROWS = (
    (("💸 Маленький чек", "bad_price"), ("🌍 Не наш регион", "bad_region")),
    (("🧩 Не наш профиль", "not_profile"), ("🛠️ Услуги/ПО", "service_only")),
    (("📦 Разовая мелочь", "low_value_one_off"), ("🏛️ Заказчик не подходит", "bad_customer_fit")),
    (("⏰ Поздно/не сейчас", "bad_timing"), ("📞 Дубль/уже обработали", "duplicate_or_processed")),
    (("❓ Другое", "other_reject"),),
)


FEEDBACK_LABELS = {
    **SALES_WORKFLOW_STATUS_LABELS,
    **REJECT_REASON_STATUS_LABELS,
    "interesting": "Интересно",
    "in_work": "В работу",
    "skip": "Мимо",
    "tailored": "Заточено",
}


FILTER_FEEDBACK_LABELS = {
    "filter_hidden_correct": "фильтр сработал верно",
    "filter_hidden_wrong": "фильтр ошибся",
    "filter_docs_correct": "фильтр сработал верно",
    "filter_docs_wrong": "фильтр ошибся",
}


FILTER_FEEDBACK_ANSWERS = {
    "filter_hidden_correct": "Записал: фильтр сработал верно",
    "filter_docs_correct": "Записал: фильтр сработал верно",
    "filter_hidden_wrong": "Записал: фильтр ошибся",
    "filter_docs_wrong": "Записал: фильтр ошибся",
}


MENU_BUTTONS = [
    ["Сегодня", "Необработанные лиды"],
    ["📌 В работе"],
    ["📊 Excel Сегодня", "📊 Excel Необработанные лиды"],
    ["📊 Excel В работе", "📥 Excel для Bitrix"],
    ["Аудит отбора"],
]

TELEGRAM_COMMAND_MENU = (
    ("start", "Открыть меню"),
    ("menu", "Открыть меню"),
    ("digest_today", "Сегодня"),
    ("digest", "Необработанные лиды"),
    ("in_work", "Лиды в работе"),
    ("excel_today", "Excel: сегодня"),
    ("excel_all", "Excel: необработанные лиды"),
    ("excel_in_work", "Excel: в работе"),
    ("excel_bitrix", "Excel: Bitrix"),
    ("recent", "Свежие LLM-отчеты"),
    ("chatid", "Показать chat_id"),
    ("cancel", "Отменить ввод примечания"),
)

EXCEL_MENU_CHOICES = {
    "📊 Excel Сегодня": EXCEL_EXPORT_TODAY,
    "📊 Excel Необработанные лиды": EXCEL_EXPORT_ALL,
    "📊 Excel В работе": EXCEL_EXPORT_IN_WORK,
    "📥 Excel для Bitrix": EXCEL_EXPORT_BITRIX_IN_WORK,
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True, one_time_keyboard=False)


def bot_command_menu() -> list[BotCommand]:
    return [
        BotCommand(command=command, description=description)
        for command, description in TELEGRAM_COMMAND_MENU
    ]


async def setup_bot_menu(app: Any) -> None:
    try:
        commands = bot_command_menu()
        await app.bot.set_my_commands(commands)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.warning(
            "TG_FLOW version=%s event=bot_command_menu_failed",
            TELEGRAM_UI_FLOW_VERSION,
            exc_info=True,
        )
        return

    logger.info(
        "TG_FLOW version=%s event=bot_command_menu_set commands=%s",
        TELEGRAM_UI_FLOW_VERSION,
        ",".join(command.command for command in commands),
    )


def text_preview(text: str | None, limit: int = LOG_PREVIEW_LEN) -> str:
    if not text:
        return ""

    return " ".join(str(text).split())[:limit]


def callback_query_chat_id(query: Any) -> Any | None:
    message = getattr(query, "message", None)
    chat_id = getattr(message, "chat_id", None)
    if chat_id is not None:
        return chat_id
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", None)


def telegram_user_display_name(user: Any) -> str | None:
    if user is None:
        return None
    return (
        getattr(user, "username", None)
        or " ".join(
            item
            for item in [
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
            ]
            if item
        )
        or str(getattr(user, "id", ""))
        or None
    )


def context_user_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, dict):
        return user_data
    user_data = {}
    try:
        setattr(context, "user_data", user_data)
    except Exception:
        return {}
    return user_data


def set_pending_lead_note(context: ContextTypes.DEFAULT_TYPE, tender_id: str) -> None:
    context_user_data(context)[LEAD_NOTE_PENDING_KEY] = {
        "tender_id": tender_id,
        "created_at": datetime.now(timezone.utc),
    }


def get_pending_lead_note(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    pending = context_user_data(context).get(LEAD_NOTE_PENDING_KEY)
    return pending if isinstance(pending, dict) else None


def clear_pending_lead_note(context: ContextTypes.DEFAULT_TYPE) -> None:
    context_user_data(context).pop(LEAD_NOTE_PENDING_KEY, None)


def pending_lead_note_expired(pending: dict) -> bool:
    created_at = pending.get("created_at")
    if not isinstance(created_at, datetime):
        return True
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > LEAD_NOTE_PENDING_TTL


def telegram_retry_after_seconds(exc: Exception) -> float | None:
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, timedelta):
        seconds = retry_after.total_seconds()
    elif isinstance(retry_after, (int, float)):
        seconds = float(retry_after)
    else:
        return None

    if seconds <= 0 or seconds > TELEGRAM_RETRY_AFTER_MAX_SECONDS:
        return None
    return seconds


def telegram_network_retry_delay(exc: Exception, attempt: int) -> float | None:
    if exc.__class__.__name__ not in TELEGRAM_TRANSIENT_NETWORK_ERRORS:
        return None
    return min(
        TELEGRAM_NETWORK_RETRY_BASE_SECONDS * (2**attempt),
        TELEGRAM_NETWORK_RETRY_MAX_SECONDS,
    )


def rewind_telegram_uploads(kwargs: dict[str, Any]) -> None:
    for key in ("document", "photo", "video", "audio", "voice", "animation"):
        upload = kwargs.get(key)
        seek = getattr(upload, "seek", None)
        if callable(seek):
            seek(0)


async def send_telegram_with_retry(
    send_callable,
    kwargs: dict[str, Any],
    *,
    sender: str,
    chat_id: Any | None = None,
    tender_id: str | None = None,
    status: str = "",
    retry_attempts: int = 1,
    network_retry_attempts: int = TELEGRAM_NETWORK_RETRY_ATTEMPTS,
) -> None:
    rate_limit_attempt = 0
    network_attempt = 0
    while True:
        try:
            rewind_telegram_uploads(kwargs)
            await send_callable(**kwargs)
            return
        except Exception as exc:
            retry_after = telegram_retry_after_seconds(exc)
            if retry_after is not None and rate_limit_attempt < retry_attempts:
                rate_limit_attempt += 1
                delay = retry_after + TELEGRAM_RETRY_AFTER_BUFFER_SECONDS
                logger.warning(
                    "TG_FLOW version=%s sender=%s event=retry_after chat_id=%s tender_id=%s status=%s "
                    "retry_after=%s delay_seconds=%s attempt=%s/%s",
                    TELEGRAM_UI_FLOW_VERSION,
                    sender,
                    chat_id,
                    tender_id,
                    status,
                    retry_after,
                    delay,
                    rate_limit_attempt,
                    retry_attempts,
                )
                await asyncio.sleep(delay)
                continue

            network_delay = telegram_network_retry_delay(exc, network_attempt)
            if network_delay is None or network_attempt >= network_retry_attempts:
                raise

            network_attempt += 1
            logger.warning(
                "TG_FLOW version=%s sender=%s event=network_retry chat_id=%s tender_id=%s status=%s "
                "error_type=%s delay_seconds=%s attempt=%s/%s",
                TELEGRAM_UI_FLOW_VERSION,
                sender,
                chat_id,
                tender_id,
                status,
                exc.__class__.__name__,
                network_delay,
                network_attempt,
                network_retry_attempts,
            )
            await asyncio.sleep(network_delay)


async def sleep_between_bulk_cards(
    *,
    sender: str,
    index: int,
    total: int,
    delay_seconds: float,
    tender_id: str | None = None,
) -> None:
    if delay_seconds <= 0 or index >= total:
        return
    logger.info(
        "TG_FLOW version=%s sender=%s event=bulk_pace card=%s/%s tender_id=%s delay_seconds=%s",
        TELEGRAM_UI_FLOW_VERSION,
        sender,
        index,
        total,
        tender_id,
        delay_seconds,
    )
    await asyncio.sleep(delay_seconds)


async def send_feedback_callback_message(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    tender_id: str = "",
    status: str = "",
) -> None:
    chat_id = callback_query_chat_id(query)
    bot = getattr(context, "bot", None)
    send_message = getattr(bot, "send_message", None)

    if chat_id is not None and send_message is not None:
        kwargs = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        await send_telegram_with_retry(
            send_message,
            kwargs,
            sender="feedback_callback_message",
            chat_id=chat_id,
            tender_id=tender_id,
            status=status,
        )
        logger.info(
            "TG_FLOW version=%s sender=feedback_callback_message chat_id=%s tender_id=%s "
            "status=%s reply_markup=%s text_len=%s preview=%r",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
            tender_id,
            status,
            reply_markup is not None,
            len(text or ""),
            text_preview(text),
        )
        return

    message = getattr(query, "message", None)
    if message is None:
        raise RuntimeError("Cannot send feedback callback message without chat")
    kwargs = {}
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    await message.reply_text(text, **kwargs)


async def show_reject_reason_keyboard(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    tender_id: str,
    reply_text: str,
) -> None:
    keyboard = reject_reason_keyboard(tender_id)
    message = getattr(query, "message", None)
    edit_reply_markup = getattr(message, "edit_reply_markup", None)
    if edit_reply_markup is not None:
        try:
            await edit_reply_markup(reply_markup=keyboard)
            logger.info(
                "TG_FLOW version=%s sender=feedback_callback_message event=reject_reason_keyboard_updated "
                "chat_id=%s tender_id=%s status=%s reply_markup=True",
                TELEGRAM_UI_FLOW_VERSION,
                callback_query_chat_id(query),
                tender_id,
                SALES_NOT_RELEVANT_STATUS,
            )
            return
        except Exception:
            logger.warning(
                "Failed to update feedback callback keyboard; sending fallback message",
                exc_info=True,
            )

    await send_feedback_callback_message(
        query,
        context,
        reply_text,
        reply_markup=keyboard,
        tender_id=tender_id,
        status=SALES_NOT_RELEVANT_STATUS,
    )


async def send_tender_cards(
    update: Update,
    cards: list[TenderDigestCard],
    *,
    keyboard_factory=None,
    keyboard_from_card: bool = False,
    pace_seconds: float = TELEGRAM_BULK_CARD_DELAY_SECONDS,
) -> None:
    logger.info(
        "TG_FLOW version=%s sender=send_tender_cards cards=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
    )

    if keyboard_factory is None:
        keyboard_factory = business_keyboard

    for index, card in enumerate(cards, start=1):
        keyboard = keyboard_factory(card) if keyboard_from_card else keyboard_factory(card.tender_id)
        logger.info(
            "TG_FLOW version=%s sender=send_tender_cards card=%s/%s tender_id=%s external_id=%s keyboard=%s text_len=%s preview=%r",
            TELEGRAM_UI_FLOW_VERSION,
            index,
            len(cards),
            card.tender_id,
            card.external_id,
            bool(keyboard),
            len(card.message_text or ""),
            text_preview(card.message_text),
        )
        await send_card(
            update,
            card.message_text,
            keyboard,
            tender_id=card.tender_id,
            message_delay_seconds=pace_seconds,
        )
        await sleep_between_bulk_cards(
            sender="send_tender_cards",
            index=index,
            total=len(cards),
            delay_seconds=pace_seconds,
            tender_id=card.tender_id,
        )


def build_request() -> HTTPXRequest:
    kwargs = {
        "connect_timeout": 30,
        "read_timeout": 30,
        "write_timeout": 30,
        "pool_timeout": 30,
    }

    proxy_url = settings.telegram_proxy_url

    if proxy_url:
        params = inspect.signature(HTTPXRequest.__init__).parameters

        if "proxy" in params:
            kwargs["proxy"] = proxy_url
        elif "proxy_url" in params:
            kwargs["proxy_url"] = proxy_url
        else:
            raise RuntimeError("HTTPXRequest does not support proxy/proxy_url")

        logger.info("Telegram proxy enabled: %s", proxy_url.split("://")[0])

    return HTTPXRequest(**kwargs)


def get_allowed_chat_ids() -> set[int]:
    raw = settings.telegram_allowed_chat_ids

    if not raw or not raw.strip():
        return set()

    try:
        return {int(item.strip()) for item in raw.split(",") if item.strip()}
    except ValueError as exc:
        raise RuntimeError(
            "TELEGRAM_ALLOWED_CHAT_IDS must contain only comma-separated integers"
        ) from exc


def is_allowed(chat_id: int) -> bool:
    allowed = get_allowed_chat_ids()
    return chat_id in allowed



def split_telegram_message(text: str, limit: int = MAX_TELEGRAM_MESSAGE_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts = []
    rest = text

    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)

        if cut == -1:
            cut = rest.rfind("\n", 0, limit)

        if cut == -1:
            cut = limit

        part = rest[:cut].strip()
        if part:
            parts.append(part)

        rest = rest[cut:].strip()

    if rest:
        parts.append(rest)

    return parts


async def send_card_message(
    message,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    tender_id: str | None = None,
    *,
    message_delay_seconds: float = 0.0,
) -> None:
    parts = split_telegram_message(text)

    for idx, part in enumerate(parts):
        attach_keyboard = keyboard if idx == len(parts) - 1 else None
        logger.info(
            "TG_FLOW version=%s sender=send_card tender_id=%s chunk=%s/%s reply_markup=%s text_len=%s preview=%r",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            idx + 1,
            len(parts),
            bool(attach_keyboard),
            len(part),
            text_preview(part),
        )
        await send_telegram_with_retry(
            message.reply_text,
            {
                "text": part,
                "reply_markup": attach_keyboard,
                "disable_web_page_preview": True,
            },
            sender="send_card",
            tender_id=tender_id,
        )
        await sleep_between_bulk_cards(
            sender="send_card",
            index=idx + 1,
            total=len(parts),
            delay_seconds=message_delay_seconds,
            tender_id=tender_id,
        )


async def send_card(
    update: Update,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    tender_id: str | None = None,
    *,
    message_delay_seconds: float = 0.0,
) -> None:
    await send_card_message(
        update.message,
        text,
        keyboard,
        tender_id=tender_id,
        message_delay_seconds=message_delay_seconds,
    )


def lead_queue_keyboard(tender_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ В работу",
                    callback_data=f"fb:{tender_id}:{SALES_IN_WORK_STATUS}",
                ),
                InlineKeyboardButton(
                    "🚫 Мимо",
                    callback_data=f"fb:{tender_id}:{SALES_NOT_RELEVANT_STATUS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏢 Существующий клиент",
                    callback_data=f"fb:{tender_id}:{SALES_EXISTING_CLIENT_STATUS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📝 Примечание",
                    callback_data=f"note:{tender_id}",
                ),
            ],
        ]
    )


def business_keyboard(tender_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ В работу",
                    callback_data=f"fb:{tender_id}:{SALES_IN_WORK_STATUS}",
                ),
                InlineKeyboardButton(
                    "🚫 Мимо",
                    callback_data=f"fb:{tender_id}:{SALES_NOT_RELEVANT_STATUS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏢 Существующий клиент",
                    callback_data=f"fb:{tender_id}:{SALES_EXISTING_CLIENT_STATUS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📝 Примечание",
                    callback_data=f"note:{tender_id}",
                ),
            ],
        ]
    )


def reject_reason_keyboard(tender_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text, callback_data=f"fb:{tender_id}:{status}")
                for text, status in row
            ]
            for row in REJECT_REASON_BUTTON_ROWS
        ]
    )


def in_work_keyboard(tender_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "↩️ Вернуть в список",
                    callback_data=f"fb:{tender_id}:{SALES_RETURNED_STATUS}",
                ),
                InlineKeyboardButton(
                    "🚫 Мимо",
                    callback_data=f"fb:{tender_id}:{SALES_NOT_RELEVANT_STATUS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🏢 Существующий клиент",
                    callback_data=f"fb:{tender_id}:{SALES_EXISTING_CLIENT_STATUS}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📝 Примечание",
                    callback_data=f"note:{tender_id}",
                ),
            ],
        ]
    )


def all_leads_keyboard(card: TenderDigestCard) -> InlineKeyboardMarkup:
    if "Статус: В работе" in (card.message_text or ""):
        return in_work_keyboard(card.tender_id)
    return lead_queue_keyboard(card.tender_id)


async def send_tender_card_to_message(message, card: TenderDigestCard) -> None:
    await send_card_message(
        message,
        card.message_text,
        business_keyboard(card.tender_id),
        tender_id=card.tender_id,
    )


def all_targets_summary_text(
    cards_count: int,
    deadline_warning_count: int,
    *,
    deadline_min_days: float = DEFAULT_DEADLINE_MIN_DAYS,
    limit: int | None = None,
) -> str:
    del deadline_min_days
    lines = [
        "AI Tender Radar - необработанные клиентские лиды",
        f"Необработанных лидов в очереди: {cards_count}",
        f"С близким/истекшим сроком: {deadline_warning_count}",
    ]

    if limit is None:
        lines.append("Показаны все доступные go-лиды, кроме отклоненных пользователем.")
    else:
        lines.append(f"Показаны первые {cards_count} лидов, лимит: {limit}.")

    return "\n".join(lines)


def filter_audit_keyboard(card: TodayFilterAuditCard) -> InlineKeyboardMarkup | None:
    del card
    return None


async def send_filter_audit_cards(
    update: Update,
    cards: list[TodayFilterAuditCard],
    *,
    pace_seconds: float = TELEGRAM_BULK_CARD_DELAY_SECONDS,
) -> None:
    logger.info(
        "TG_FLOW version=%s sender=send_filter_audit_cards cards=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
    )

    for index, card in enumerate(cards, start=1):
        keyboard = filter_audit_keyboard(card)
        logger.info(
            "TG_FLOW version=%s sender=send_filter_audit_cards card=%s/%s tender_id=%s external_id=%s status=%s keyboard=%s text_len=%s preview=%r",
            TELEGRAM_UI_FLOW_VERSION,
            index,
            len(cards),
            card.tender_id,
            card.external_id,
            card.status,
            bool(keyboard),
            len(card.message_text or ""),
            text_preview(card.message_text),
        )
        await send_card(
            update,
            card.message_text,
            keyboard,
            tender_id=card.tender_id,
            message_delay_seconds=pace_seconds,
        )
        await sleep_between_bulk_cards(
            sender="send_filter_audit_cards",
            index=index,
            total=len(cards),
            delay_seconds=pace_seconds,
            tender_id=card.tender_id,
        )


def feedback_column_names(cur) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'feedback';
        """,
        (),
    )
    return {str(row[0]) for row in cur.fetchall()}


def save_feedback(
    tender_id: str,
    status: str,
    user_name: str | None,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
) -> None:
    created_at = datetime.now(timezone.utc)
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            columns = feedback_column_names(cur)
            if not columns:
                columns = {"tender_id", "user_name", "status"}

            metadata = {}
            if chat_id is not None:
                metadata["chat_id"] = chat_id
            if user_id is not None:
                metadata["user_id"] = user_id

            field_values = {
                "tender_id": tender_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "user_name": user_name,
                "status": status,
                "created_at": created_at,
            }
            if metadata and "comment" in columns:
                field_values["comment"] = json.dumps(
                    {"telegram": metadata},
                    ensure_ascii=False,
                    sort_keys=True,
                )

            insert_columns = [
                column
                for column in (
                    "tender_id",
                    "chat_id",
                    "user_id",
                    "user_name",
                    "status",
                    "created_at",
                    "comment",
                )
                if column in columns and field_values.get(column) is not None
            ]
            placeholders = ", ".join(["%s"] * len(insert_columns))
            column_sql = ", ".join(insert_columns)
            cur.execute(
                f"INSERT INTO feedback ({column_sql}) VALUES ({placeholders});",
                tuple(field_values[column] for column in insert_columns),
            )
            conn.commit()


def get_tender_external_id(tender_id: str) -> tuple[str, str] | None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_id, title
                FROM tenders
                WHERE id = %s
                LIMIT 1;
                """,
                (tender_id,),
            )
            row = cur.fetchone()

    if not row:
        return None

    return row[0], row[1]


def llm_document_not_ready_message(tender_id: str) -> str | None:
    tender = get_tender_digest_row(tender_id)
    if not tender:
        return None

    tender = dict(tender)
    manual_reason = manual_document_reason_for_tender(tender)
    if manual_reason:
        return TELEGRAM_DOCUMENT_NOT_READY_MESSAGE

    if not tender_has_extracted_document_text(tender):
        return TELEGRAM_DOCUMENT_NOT_READY_MESSAGE

    return None


@dataclass(frozen=True)
class TelegramLLMRunningAnalysis:
    tender_id: str | None
    external_id: str | None
    started_at: datetime | None
    payload: dict[str, Any]


@dataclass
class TelegramLLMAnalysisStartResult:
    status: str
    lock: "TelegramLLMAdvisoryLock | None" = None
    existing_result: dict | None = None
    running: TelegramLLMRunningAnalysis | None = None


class TelegramLLMAdvisoryLock:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.released = False

    def release(self) -> None:
        if self.released:
            return

        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s);",
                    (TELEGRAM_LLM_ANALYSIS_LOCK_KEY,),
                )
                cur.fetchone()
            logger.info("Telegram LLM advisory lock released")
        finally:
            self.released = True
            self.conn.close()


_telegram_llm_running_state: TelegramLLMRunningAnalysis | None = None
_telegram_llm_running_state_lock = threading.Lock()


def telegram_event_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    return None


def telegram_event_timestamp(value: Any) -> float:
    value_dt = telegram_event_datetime(value)
    if not value_dt:
        return 0.0

    if value_dt.tzinfo is None:
        value_dt = value_dt.replace(tzinfo=timezone.utc)

    return value_dt.timestamp()


def telegram_llm_started_event_is_stale(
    started_at: Any,
    *,
    now: datetime | None = None,
) -> bool:
    started_ts = telegram_event_timestamp(started_at)
    if not started_ts:
        return True

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    return started_ts < (now_dt - TELEGRAM_LLM_ANALYSIS_STALE_AFTER).timestamp()


def parse_telegram_llm_event_payload(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message

    try:
        payload = json.loads(str(message or "{}"))
    except (TypeError, ValueError):
        return {}

    return payload if isinstance(payload, dict) else {}


def telegram_event_identity(event: dict[str, Any]) -> tuple[str | None, str | None]:
    payload = parse_telegram_llm_event_payload(event.get("message"))
    tender_id = event.get("tender_id") or payload.get("tender_id")
    external_id = event.get("external_id") or payload.get("external_id")
    return (
        str(tender_id) if tender_id else None,
        str(external_id) if external_id else None,
    )


def telegram_llm_events_match(
    left_tender_id: str | None,
    left_external_id: str | None,
    right_tender_id: str | None,
    right_external_id: str | None,
) -> bool:
    if left_tender_id and right_tender_id:
        return left_tender_id == right_tender_id

    if left_external_id and right_external_id:
        return left_external_id == right_external_id

    return False


def active_telegram_llm_analysis_from_events(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> TelegramLLMRunningAnalysis | None:
    ordered_events = sorted(
        events,
        key=lambda event: telegram_event_timestamp(event.get("created_at")),
        reverse=True,
    )

    terminal_events = {
        TELEGRAM_LLM_ANALYSIS_SUCCEEDED,
        TELEGRAM_LLM_ANALYSIS_FAILED,
    }

    for event in ordered_events:
        if event.get("event_type") != TELEGRAM_LLM_ANALYSIS_STARTED:
            continue

        started_at = telegram_event_datetime(event.get("created_at"))
        if telegram_llm_started_event_is_stale(started_at, now=now):
            continue

        tender_id, external_id = telegram_event_identity(event)
        started_ts = telegram_event_timestamp(started_at)
        has_terminal_event = False

        for candidate in ordered_events:
            if candidate.get("event_type") not in terminal_events:
                continue

            candidate_tender_id, candidate_external_id = telegram_event_identity(candidate)
            if not telegram_llm_events_match(
                tender_id,
                external_id,
                candidate_tender_id,
                candidate_external_id,
            ):
                continue

            if telegram_event_timestamp(candidate.get("created_at")) >= started_ts:
                has_terminal_event = True
                break

        if has_terminal_event:
            continue

        return TelegramLLMRunningAnalysis(
            tender_id=tender_id,
            external_id=external_id,
            started_at=started_at,
            payload=parse_telegram_llm_event_payload(event.get("message")),
        )

    return None


def load_recent_telegram_llm_analysis_events(
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    cutoff = now_dt - TELEGRAM_LLM_ANALYSIS_STALE_AFTER

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pe.tender_id::text,
                    t.external_id,
                    pe.event_type,
                    pe.message,
                    pe.created_at
                FROM processing_events pe
                LEFT JOIN tenders t ON t.id = pe.tender_id
                WHERE pe.event_type IN (%s, %s, %s)
                  AND pe.created_at >= %s
                ORDER BY pe.created_at DESC
                LIMIT 50;
                """,
                (*TELEGRAM_LLM_ANALYSIS_EVENT_TYPES, cutoff),
            )
            rows = cur.fetchall()

    return [
        {
            "tender_id": row[0],
            "external_id": row[1],
            "event_type": row[2],
            "message": row[3],
            "created_at": row[4],
        }
        for row in rows
    ]


def set_current_telegram_llm_analysis(running: TelegramLLMRunningAnalysis) -> None:
    global _telegram_llm_running_state

    with _telegram_llm_running_state_lock:
        _telegram_llm_running_state = running


def clear_current_telegram_llm_analysis(tender_id: str | None = None) -> None:
    global _telegram_llm_running_state

    with _telegram_llm_running_state_lock:
        if tender_id and _telegram_llm_running_state:
            current_tender_id = _telegram_llm_running_state.tender_id
            if current_tender_id and current_tender_id != str(tender_id):
                return

        _telegram_llm_running_state = None


def get_local_current_telegram_llm_analysis(
    *,
    now: datetime | None = None,
) -> TelegramLLMRunningAnalysis | None:
    with _telegram_llm_running_state_lock:
        running = _telegram_llm_running_state

    if not running:
        return None

    if telegram_llm_started_event_is_stale(running.started_at, now=now):
        clear_current_telegram_llm_analysis(running.tender_id)
        return None

    return running


def get_current_telegram_llm_analysis(
    *,
    now: datetime | None = None,
) -> TelegramLLMRunningAnalysis | None:
    local_running = get_local_current_telegram_llm_analysis(now=now)
    if local_running:
        return local_running

    try:
        events = load_recent_telegram_llm_analysis_events(now=now)
    except Exception as exc:
        logger.warning("Failed to read telegram LLM processing events: %s", exc)
        return None

    return active_telegram_llm_analysis_from_events(events, now=now)


def telegram_llm_start_result_for_running(
    tender_id: str,
    running: TelegramLLMRunningAnalysis | None,
    *,
    external_id: str | None = None,
) -> TelegramLLMAnalysisStartResult:
    if running and (
        running.tender_id == str(tender_id)
        or (external_id and running.external_id == external_id)
    ):
        return TelegramLLMAnalysisStartResult(
            status=TELEGRAM_LLM_START_SAME_RUNNING,
            running=running,
        )

    return TelegramLLMAnalysisStartResult(
        status=TELEGRAM_LLM_START_BUSY_OTHER,
        running=running,
    )


def try_acquire_telegram_llm_analysis_lock() -> TelegramLLMAdvisoryLock | None:
    conn = psycopg.connect(settings.database_url, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s);",
                (TELEGRAM_LLM_ANALYSIS_LOCK_KEY,),
            )
            row = cur.fetchone()

        if not row or not bool(row[0]):
            logger.info("Telegram LLM advisory lock busy")
            conn.close()
            return None

        logger.info("Telegram LLM advisory lock acquired")
        return TelegramLLMAdvisoryLock(conn)
    except Exception:
        conn.close()
        raise


def telegram_requested_by(query) -> dict[str, Any]:
    user = getattr(query, "from_user", None)
    user_name = None

    if user:
        user_name = getattr(user, "username", None)
        if not user_name:
            user_name = " ".join(
                item
                for item in [
                    getattr(user, "first_name", None),
                    getattr(user, "last_name", None),
                ]
                if item
            ).strip()
        if not user_name and getattr(user, "id", None):
            user_name = str(user.id)

    message = getattr(query, "message", None)
    return {
        "user_id": getattr(user, "id", None) if user else None,
        "user_name": user_name,
        "chat_id": getattr(message, "chat_id", None),
    }


def telegram_llm_analysis_event_type(status: str) -> str:
    return {
        TELEGRAM_LLM_START_STARTED: TELEGRAM_LLM_ANALYSIS_STARTED,
        "succeeded": TELEGRAM_LLM_ANALYSIS_SUCCEEDED,
        "failed": TELEGRAM_LLM_ANALYSIS_FAILED,
    }[status]


def record_telegram_llm_analysis_event(
    *,
    tender_id: str,
    external_id: str,
    requested_by: dict[str, Any],
    status: str,
    error_message: str | None = None,
    created_at: datetime | None = None,
) -> None:
    created_at_dt = created_at or datetime.now(timezone.utc)
    if created_at_dt.tzinfo is None:
        created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)

    payload: dict[str, Any] = {
        "external_id": external_id,
        "tender_id": str(tender_id),
        "requested_by": requested_by,
        "status": status,
        "created_at": created_at_dt.isoformat(),
    }

    if error_message:
        payload["error_message"] = str(error_message)[:500]

    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processing_events (tender_id, event_type, message)
                    VALUES (%s, %s, %s);
                    """,
                    (
                        tender_id,
                        telegram_llm_analysis_event_type(status),
                        json.dumps(payload, ensure_ascii=False, sort_keys=True)[:2000],
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("Failed to write telegram LLM processing event: %s", exc)


def begin_telegram_llm_analysis_run(
    *,
    tender_id: str,
    external_id: str,
    requested_by: dict[str, Any],
) -> TelegramLLMAnalysisStartResult:
    running = get_current_telegram_llm_analysis()
    if running:
        return telegram_llm_start_result_for_running(
            tender_id,
            running,
            external_id=external_id,
        )

    lock = try_acquire_telegram_llm_analysis_lock()
    if not lock:
        running = get_current_telegram_llm_analysis()
        return telegram_llm_start_result_for_running(
            tender_id,
            running,
            external_id=external_id,
        )

    started_at = datetime.now(timezone.utc)
    payload = {
        "external_id": external_id,
        "tender_id": str(tender_id),
        "requested_by": requested_by,
        "status": TELEGRAM_LLM_START_STARTED,
        "created_at": started_at.isoformat(),
    }
    set_current_telegram_llm_analysis(
        TelegramLLMRunningAnalysis(
            tender_id=str(tender_id),
            external_id=external_id,
            started_at=started_at,
            payload=payload,
        )
    )
    record_telegram_llm_analysis_event(
        tender_id=tender_id,
        external_id=external_id,
        requested_by=requested_by,
        status=TELEGRAM_LLM_START_STARTED,
        created_at=started_at,
    )

    return TelegramLLMAnalysisStartResult(
        status=TELEGRAM_LLM_START_STARTED,
        lock=lock,
    )


def finish_telegram_llm_analysis_run(
    *,
    lock: TelegramLLMAdvisoryLock | None,
    tender_id: str,
    external_id: str,
    requested_by: dict[str, Any],
    status: str,
    error_message: str | None = None,
) -> None:
    try:
        record_telegram_llm_analysis_event(
            tender_id=tender_id,
            external_id=external_id,
            requested_by=requested_by,
            status=status,
            error_message=error_message,
        )
    finally:
        clear_current_telegram_llm_analysis(tender_id)
        if lock:
            try:
                lock.release()
            except Exception as exc:
                logger.warning("Failed to release telegram LLM advisory lock: %s", exc)


def log_telegram_llm_background_task_result(task) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.warning("Telegram LLM background task was cancelled")
        return
    except Exception as exc:
        logger.warning("Could not inspect telegram LLM background task result: %s", exc)
        return

    if exc:
        logger.error(
            "Telegram LLM background task ended with unhandled exception",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def create_telegram_llm_background_task(context: ContextTypes.DEFAULT_TYPE, coro):
    application = getattr(context, "application", None)
    create_task = getattr(application, "create_task", None)

    if callable(create_task):
        task = create_task(coro)
    else:
        task = asyncio.create_task(coro)

    add_done_callback = getattr(task, "add_done_callback", None)
    if callable(add_done_callback):
        add_done_callback(log_telegram_llm_background_task_result)

    return task


async def run_telegram_llm_analysis_background(
    *,
    message,
    lock: TelegramLLMAdvisoryLock | None,
    tender_id: str,
    external_id: str,
    requested_by: dict[str, Any],
) -> None:
    logger.info(
        "TG_FLOW version=%s background=telegram_llm_analysis event=started tender_id=%s external_id=%s",
        TELEGRAM_UI_FLOW_VERSION,
        tender_id,
        external_id,
    )

    result = None
    run_status = "failed"
    error_message = None

    try:
        try:
            preparation = await asyncio.to_thread(
                prepare_tender_for_analysis,
                tender_id=tender_id,
            )
        except Exception as exc:
            logger.exception("Tender preparation failed")
            error_message = f"preparation failed: {exc}"
            await message.reply_text(
                "Разбор не запущен: не удалось подготовить документы. "
                "Попробуйте позже или проверьте карточку закупки вручную."
            )
            return

        logger.info(
            "TG_FLOW version=%s background=telegram_llm_analysis tender_id=%s external_id=%s prep_status=%s ready_for_llm=%s document_counts=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
            preparation.status.value,
            preparation.ready_for_llm,
            preparation.document_counts,
        )

        block_message = preparation_block_message(preparation)
        if block_message:
            error_message = block_message
            await message.reply_text(block_message)
            return

        await message.reply_text(
            "Документы готовы. Запускаю анализ, это может занять 1-2 минуты."
        )

        try:
            deep_limits = resolve_analysis_limits(ANALYSIS_DEPTH_DEEP)
            result = await asyncio.to_thread(
                generate_presales_report,
                external_id=external_id,
                analysis_depth=ANALYSIS_DEPTH_DEEP,
                max_spec_chars=deep_limits.max_spec_chars,
                max_other_chars=deep_limits.max_other_chars,
                max_output_tokens=deep_limits.max_output_tokens,
            )
        except Exception as exc:
            logger.exception("LLM analysis failed")
            error_message = str(exc)
            await message.reply_text(f"Ошибка разбора: {exc}")
            return

        run_status = "succeeded"
        logger.info(
            "TG_FLOW version=%s background=telegram_llm_analysis event=succeeded tender_id=%s external_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
        )

        try:
            await send_llm_success_card_reply(message, tender_id, result)
        except Exception as exc:
            logger.exception("Telegram LLM report delivery failed")
            error_message = f"report delivery failed: {exc}"
    except Exception as exc:
        logger.exception("Telegram LLM background task failed")
        error_message = str(exc)
        try:
            await message.reply_text(f"Ошибка разбора: {exc}")
        except Exception:
            logger.exception("Telegram LLM background error delivery failed")
    finally:
        if run_status == "failed":
            logger.info(
                "TG_FLOW version=%s background=telegram_llm_analysis event=failed tender_id=%s external_id=%s error=%r",
                TELEGRAM_UI_FLOW_VERSION,
                tender_id,
                external_id,
                error_message,
            )

        await asyncio.to_thread(
            finish_telegram_llm_analysis_run,
            lock=lock,
            tender_id=tender_id,
            external_id=external_id,
            requested_by=requested_by,
            status=run_status,
            error_message=error_message,
        )


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def non_empty_text(value) -> str:
    text = str(value or "").strip()
    return text


def clean_text_items(value, limit: int | None = None) -> list[str]:
    items = [non_empty_text(item) for item in as_list(value)]
    items = [item for item in items if item]
    return items[:limit] if limit else items


def report_meta(result: dict) -> dict:
    meta = as_dict(result.get("meta"))
    if meta:
        return meta
    return as_dict(result.get("llm_metadata"))


def is_successful_llm_report_result(result) -> bool:
    if not isinstance(result, dict):
        return False

    if str(result.get("status") or "").lower() in {"failed", "error"}:
        return False

    meta = report_meta(result)
    if str(meta.get("parse_status") or "").lower() in {"failed", "error"}:
        return False

    return bool(as_dict(result.get("report")))


def is_routerai_two_pass_report(result: dict) -> bool:
    report = as_dict(result.get("report"))
    meta = report_meta(result)

    if meta.get("pass_mode") == ROUTERAI_TWO_PASS_META_VALUE:
        return True

    has_routerai_native_fields = any(field in report for field in ROUTERAI_TWO_PASS_SCHEMA_FIELDS)
    has_full_schema_fields = any(field in report for field in FULL_REPORT_SCHEMA_FIELDS)
    return has_routerai_native_fields and not has_full_schema_fields


def is_routerai_preferred_report(result: dict) -> bool:
    if not is_successful_llm_report_result(result):
        return False

    analysis_type = str(result.get("analysis_type") or "").strip().lower()
    model = str(result.get("model") or "").strip().lower()
    provider = str(result.get("provider") or "").strip().lower()

    if analysis_type in ROUTERAI_PREFERRED_ANALYSIS_TYPES:
        return True

    if analysis_type.startswith(f"{LLM_REPORT_ANALYSIS_TYPE_PREFIX}_routerai_"):
        return True

    if (
        analysis_type == LLM_REPORT_ANALYSIS_TYPE_PREFIX
        and model in ROUTERAI_PREFERRED_MODELS
        and is_routerai_two_pass_report(result)
    ):
        return True

    return provider == "routerai" and is_routerai_two_pass_report(result)


def full_report_schema_score(result: dict) -> int:
    if is_routerai_two_pass_report(result):
        return 0

    report = as_dict(result.get("report"))
    return sum(1 for field in FULL_REPORT_SCHEMA_FIELDS if as_dict(report.get(field)) or report.get(field))


def is_legacy_full_report(result: dict) -> bool:
    return (
        is_successful_llm_report_result(result)
        and not is_routerai_preferred_report(result)
        and full_report_schema_score(result) >= 2
    )


def created_at_timestamp(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    return 0.0


def latest_primary_technical_document_created_at_for_tender(
    tender_id: str,
) -> datetime | None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT max(d.created_at)
                FROM documents d
                WHERE d.tender_id = %s
                  AND d.extracted_text IS NOT NULL
                  AND length(d.extracted_text) > 0
                  AND {PRIMARY_TECHNICAL_DOCUMENT_SQL_CONDITION};
                """,
                (tender_id,),
            )
            row = cur.fetchone()

    return row[0] if row else None


def existing_llm_report_is_stale_after_primary_technical_document(
    tender_id: str,
    result: dict,
) -> bool:
    report_created_at = result.get("_telegram_created_at")
    if not isinstance(report_created_at, datetime):
        return False

    primary_doc_created_at = latest_primary_technical_document_created_at_for_tender(
        tender_id
    )
    if primary_doc_created_at is None:
        return False

    if primary_doc_created_at.tzinfo is None and report_created_at.tzinfo is not None:
        primary_doc_created_at = primary_doc_created_at.replace(
            tzinfo=report_created_at.tzinfo
        )
    elif primary_doc_created_at.tzinfo is not None and report_created_at.tzinfo is None:
        report_created_at = report_created_at.replace(
            tzinfo=primary_doc_created_at.tzinfo
        )

    return primary_doc_created_at > report_created_at


def latest_successful_candidate(candidates: list[dict]) -> dict | None:
    best: dict | None = None
    best_key: tuple[float, int] | None = None

    for index, candidate in enumerate(candidates):
        key = (
            created_at_timestamp(candidate.get("_telegram_created_at")),
            index,
        )
        if best_key is None or key > best_key:
            best = candidate
            best_key = key

    return best


def selected_report_metadata(result: dict | None) -> dict[str, str | None]:
    if not result:
        return {
            "selection_source": SELECTION_SOURCE_GENERATE_NEW,
            "selected_analysis_type": None,
            "selected_model": None,
        }

    return {
        "selection_source": str(
            result.get("_telegram_selection_source") or SELECTION_SOURCE_GENERATE_NEW
        ),
        "selected_analysis_type": result.get("analysis_type"),
        "selected_model": result.get("model"),
    }


def with_selection_metadata(result: dict, selection_source: str) -> dict:
    selected = dict(result)
    selected["_telegram_selection_source"] = selection_source
    selected["_telegram_selected_analysis_type"] = selected.get("analysis_type")
    selected["_telegram_selected_model"] = selected.get("model")
    return selected


def select_best_existing_llm_report(candidates: list[dict]) -> tuple[dict | None, dict[str, str | None]]:
    routerai = latest_successful_candidate(
        [candidate for candidate in candidates if is_routerai_preferred_report(candidate)]
    )
    if routerai:
        selected = with_selection_metadata(routerai, SELECTION_SOURCE_ROUTERAI_EXISTING)
        return selected, selected_report_metadata(selected)

    legacy = latest_successful_candidate(
        [candidate for candidate in candidates if is_legacy_full_report(candidate)]
    )
    if legacy:
        selected = with_selection_metadata(legacy, SELECTION_SOURCE_LEGACY_EXISTING)
        return selected, selected_report_metadata(selected)

    return None, selected_report_metadata(None)


def best_existing_llm_report(candidates: list[dict]) -> dict | None:
    selected, _ = select_best_existing_llm_report(candidates)
    return selected


def get_best_existing_llm_report(tender_id: str, external_id: str) -> dict | None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT analysis_type, model, result, created_at
                FROM analysis_results
                WHERE tender_id = %s
                  AND analysis_type LIKE %s
                ORDER BY created_at DESC;
                """,
                (tender_id, f"{LLM_REPORT_ANALYSIS_TYPE_PREFIX}%"),
            )
            rows = cur.fetchall()

    candidates: list[dict] = []
    for analysis_type, model, result, created_at in rows:
        if not isinstance(result, dict):
            continue

        candidate = dict(result)
        candidate.setdefault("external_id", external_id)
        candidate.setdefault("model", model)
        candidate.setdefault("analysis_type", analysis_type)
        candidate["_telegram_created_at"] = created_at
        candidates.append(candidate)

    selected = best_existing_llm_report(candidates)
    if selected:
        try:
            report_is_stale = (
                existing_llm_report_is_stale_after_primary_technical_document(
                    tender_id,
                    selected,
                )
            )
        except Exception:
            logger.exception("Existing LLM primary technical staleness check failed")
            report_is_stale = False
        if report_is_stale:
            logger.info(
                "TG_FLOW version=%s existing_report_stale_after_primary_technical_document tender_id=%s external_id=%s",
                TELEGRAM_UI_FLOW_VERSION,
                tender_id,
                external_id,
            )
            return None

    return selected


def delivery_feasibility_text(value) -> str:
    delivery = as_dict(value)
    if delivery:
        delivery_term = delivery.get("delivery_term")
        assessment = delivery.get("assessment")
        if isinstance(delivery_term, list) or isinstance(assessment, list):
            lines: list[str] = []
            for item in clean_text_items(delivery_term, 3):
                lines.append(f"— Срок: {item}")
            for item in clean_text_items(assessment, 4):
                lines.append(f"— Оценка: {item}")
            if lines:
                return "\n".join(lines)
        return f"{delivery_term or 'не указан'} / {assessment or 'не оценён'}"
    if isinstance(value, list):
        return bullet(value)
    if value:
        return str(value)
    return "не указан / не оценён"


def potential_offer_items(value) -> list:
    offer = as_dict(value)
    if offer:
        return as_list(offer.get("what_to_offer"))
    return as_list(value)


def presales_check_items(report: dict) -> list:
    offer = as_dict(report.get("potential_offer"))
    return clean_text_items(
        report.get("what_presales_should_check")
        or offer.get("what_presales_should_check")
        or offer.get("what_to_verify")
    )


def critical_requirement_items(report: dict) -> list:
    return clean_text_items(
        report.get("critical_tz_requirements")
        or report.get("what_is_required")
    )


def bullet(items, limit=5) -> str:
    values = clean_text_items(items, limit)
    if not values:
        return "— не указано"
    return "\n".join(f"— {item}" for item in values)


def optional_bullet(items, limit=5) -> str | None:
    values = clean_text_items(items, limit)
    if not values:
        return None
    return "\n".join(f"— {item}" for item in values)


def optional_text(value) -> str | None:
    if isinstance(value, dict):
        parts: list[str] = []
        for item in value.values():
            parts.extend(clean_text_items(item))
        return "; ".join(parts) if parts else None

    if isinstance(value, list):
        return optional_bullet(value)

    return non_empty_text(value) or None


def parse_explicit_date(match: re.Match) -> date | None:
    try:
        if match.group("iso_y"):
            return date(
                int(match.group("iso_y")),
                int(match.group("iso_m")),
                int(match.group("iso_d")),
            )

        year = int(match.group("y"))
        if year < 100:
            year += 2000

        return date(year, int(match.group("m")), int(match.group("d")))
    except ValueError:
        return None


def remove_date_with_near_preposition(text: str, start: int, end: int) -> str:
    prefix = text[:start]
    prefix_match = re.search(r"(?i)\b(?:до|к|на|от)\s+$", prefix)
    if prefix_match:
        start = prefix_match.start()

    return f"{text[:start]}{text[end:]}"


def sanitize_next_action_text(text: str, *, today: date | None = None) -> str:
    today = today or date.today()
    cleaned = non_empty_text(text)
    if not cleaned:
        return ""

    past_matches = [
        match
        for match in PAST_DATE_PATTERN.finditer(cleaned)
        if (parsed := parse_explicit_date(match)) and parsed < today
    ]
    if not past_matches:
        return cleaned

    for match in reversed(past_matches):
        cleaned = remove_date_with_near_preposition(cleaned, match.start(), match.end())

    cleaned = re.sub(
        r"(?i)\bконтрольн\w*\s+точк\w*\s*[-–—:]?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = cleaned.strip(" -–—:;,.")

    if cleaned:
        return cleaned

    return "Актуализировать ближайшую контрольную точку и следующий шаг"


def sanitized_next_action_items(value, limit=5) -> list[str]:
    values = [sanitize_next_action_text(item) for item in clean_text_items(value)]
    values = [item for item in values if item]
    return values[:limit]


def sanitized_next_action_bullet(value, limit=5) -> str:
    values = sanitized_next_action_items(value, limit)
    if not values:
        return "— не указано"
    return "\n".join(f"— {item}" for item in values)


def optional_sanitized_next_action_bullet(value, limit=5) -> str | None:
    values = sanitized_next_action_items(value, limit)
    if not values:
        return None
    return "\n".join(f"— {item}" for item in values)


def telegram_section(title: str, body: str | None) -> str | None:
    if not body:
        return None
    return f"{title}:\n{body}"


def compact_routerai_two_pass_report_text(result: dict) -> str:
    report = as_dict(result.get("report"))
    sections: list[str] = [
        "🤖 Разбор готов",
        "",
        f"Закупка: {result.get('external_id')}",
        f"Модель: {result.get('model')}",
    ]

    summary = optional_text(report.get("summary"))
    if summary:
        sections.extend(["", f"Резюме:\n{summary}"])

    verdict = optional_text(report.get("verdict"))
    confidence = optional_text(report.get("confidence"))
    verdict_parts = []
    if verdict:
        verdict_parts.append(verdict)
    if confidence:
        verdict_parts.append(f"уверенность: {confidence}")
    if verdict_parts:
        sections.extend(["", f"Вердикт:\n{' / '.join(verdict_parts)}"])

    optional_sections = [
        telegram_section("Состав поставки", optional_bullet(report.get("supply_composition"), 6)),
        telegram_section("Критичные требования ТЗ", optional_bullet(critical_requirement_items(report), 6)),
        telegram_section("Почему / риски", optional_bullet(report.get("top_risks"), 5)),
        telegram_section("Срок поставки / исполнимость", optional_text(report.get("delivery_feasibility"))),
        telegram_section("Что можно предложить", optional_bullet(report.get("potential_offer"), 5)),
        telegram_section("Что проверить пресейлу", optional_bullet(presales_check_items(report), 6)),
        telegram_section("Что уточнить у заказчика", optional_bullet(report.get("questions_to_customer"), 6)),
        telegram_section(
            "Рекомендованные действия",
            optional_sanitized_next_action_bullet(report.get("recommended_actions") or report.get("next_action"), 5),
        ),
    ]
    for section in optional_sections:
        if section:
            sections.extend(["", section])

    return "\n".join(sections).strip()


def compact_full_report_text(result: dict) -> str:
    report = as_dict(result.get("report"))

    recommendation = as_dict(report.get("recommendation"))
    tailoring = as_dict(report.get("tailoring_risk"))
    delivery = report.get("delivery_feasibility")
    tech = as_dict(report.get("technical_spec_analysis"))
    offer = report.get("potential_offer")
    commercial = as_dict(report.get("commercial_assessment"))
    decision = recommendation.get("decision") or report.get("verdict") or "не указано"
    confidence = recommendation.get("confidence") or report.get("confidence") or "не указана"

    return f"""
🤖 Разбор готов

Закупка: {result.get("external_id")}
Модель: {result.get("model")}

Резюме:
{report.get("summary", "не указано")}

Вердикт:
{decision} / уверенность: {confidence}

Почему:
{bullet(recommendation.get("reasoning"), 4)}

Анализ ТЗ:
{bullet(tech.get("what_is_required"), 5)}

Модели/вендоры:
{bullet(tech.get("mentioned_models_or_vendors"), 5)}

Эквивалент допускается:
{tech.get("equivalent_allowed", "не указано")}

Срок поставки:
{delivery_feasibility_text(delivery)}

Риск заточки:
{tailoring.get("level", "не указан")}

Основания:
{bullet(tailoring.get("reasons"), 5)}

Что можно предложить:
{bullet(potential_offer_items(offer), 5)}

Что проверить пресейлу:
{bullet(presales_check_items(report), 6)}

Что уточнить у заказчика:
{bullet(report.get("questions_to_customer"), 6)}

Fit для интегратора:
{commercial.get("fit_for_integrator", "не указан")}

Рекомендованные действия:
{sanitized_next_action_bullet(report.get("recommended_actions") or report.get("next_action"), 5)}
""".strip()


def compact_llm_report_text(result: dict) -> str:
    if is_routerai_two_pass_report(result):
        return compact_routerai_two_pass_report_text(result)

    return compact_full_report_text(result)


async def send_long_reply(message, text: str) -> None:
    parts = split_telegram_message(text)
    logger.info(
        "TG_FLOW version=%s sender=send_long_reply chunks=%s text_len=%s preview=%r",
        TELEGRAM_UI_FLOW_VERSION,
        len(parts),
        len(text or ""),
        text_preview(text),
    )

    for index, part in enumerate(parts, start=1):
        logger.info(
            "TG_FLOW version=%s sender=send_long_reply chunk=%s/%s reply_markup=False text_len=%s preview=%r",
            TELEGRAM_UI_FLOW_VERSION,
            index,
            len(parts),
            len(part),
            text_preview(part),
        )
        await message.reply_text(part, disable_web_page_preview=True)


async def send_llm_success_card_reply(
    message,
    tender_id: str,
    result: dict | None = None,
) -> None:
    try:
        card = await asyncio.to_thread(
            build_tender_digest_card,
            tender_id,
            prefix=TELEGRAM_LLM_SUCCESS_PREFIX,
            llm_result_override=result,
        )
    except Exception:
        logger.exception("Telegram full tender card build failed after LLM report")
        card = None

    if card:
        logger.info(
            "TG_FLOW version=%s sender=send_llm_success_card_reply tender_id=%s external_id=%s full_card=True text_len=%s",
            TELEGRAM_UI_FLOW_VERSION,
            card.tender_id,
            card.external_id,
            len(card.message_text or ""),
        )
        await send_tender_card_to_message(message, card)
        return

    logger.warning(
        "TG_FLOW version=%s sender=send_llm_success_card_reply tender_id=%s full_card=False fallback=compact_report",
        TELEGRAM_UI_FLOW_VERSION,
        tender_id,
    )
    fallback_text = TELEGRAM_LLM_SUCCESS_PREFIX
    if result:
        fallback_text = f"{fallback_text}\n\n{compact_llm_report_text(result)}"
    await send_long_reply(message, fallback_text)


def preparation_block_message(result: TenderPreparationResult) -> str | None:
    counts = result.document_counts

    if result.status == PreparationStatus.READY_FOR_LLM:
        return None

    if result.status == PreparationStatus.BLOCKED_BY_MARKETPLACE_AUTH:
        return (
            "Разбор не запущен: часть документов доступна только после авторизации "
            "на внешней площадке. Нужна ручная загрузка документов или доступ к ЭТП."
        )

    if result.status == PreparationStatus.NO_VALID_DOCUMENTS:
        return (
            "Разбор не запущен: не нашлось валидных документов для анализа. "
            f"Найдено в карточке: {counts.found}, скачано: {counts.downloaded}."
        )

    if result.status == PreparationStatus.DOCUMENTS_DOWNLOADED_BUT_NO_TEXT:
        return (
            "Разбор не запущен: документы скачаны, но текст из них извлечь не удалось. "
            "Нужна ручная проверка файлов или сканов."
        )

    if result.status == PreparationStatus.TENDER_NOT_FOUND:
        return "Разбор не запущен: закупка не найдена в базе."

    if result.status == PreparationStatus.PARTIAL_PREPARATION:
        return (
            "Разбор не запущен: подготовка документов завершилась частично, "
            "данных пока недостаточно для надежного автоматического разбора."
        )

    return "Разбор не запущен: закупку не удалось подготовить к анализу."


async def llm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    logger.info(
        "TG_FLOW version=%s handler=llm_callback chat_id=%s data=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        query.data,
    )

    if not is_allowed(chat_id):
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")

    if len(parts) != 2 or parts[0] != "llm":
        await query.answer("Некорректная команда", show_alert=True)
        return

    tender_id = parts[1]
    tender_ref = get_tender_external_id(tender_id)

    if not tender_ref:
        await query.answer("Закупка не найдена", show_alert=True)
        return

    external_id, _title = tender_ref

    await query.answer("Ищу готовый разбор")
    try:
        existing_result = await asyncio.to_thread(
            get_best_existing_llm_report,
            tender_id,
            external_id,
        )
    except Exception:
        logger.exception("Existing LLM report lookup failed")
        await query.message.reply_text(
            "Разбор не запущен: не удалось проверить, есть ли готовый отчет. "
            "Попробуйте позже."
        )
        return

    if existing_result:
        selection = selected_report_metadata(existing_result)
        logger.info(
            "TG_FLOW version=%s handler=llm_callback tender_id=%s external_id=%s existing_report=True selection_source=%s selected_analysis_type=%s selected_model=%s schema=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
            selection["selection_source"],
            selection["selected_analysis_type"],
            selection["selected_model"],
            "routerai_two_pass" if is_routerai_two_pass_report(existing_result) else "full",
        )
        logger.info(
            "TG_FLOW version=%s handler=llm_callback event=already_ready tender_id=%s external_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
        )
        await query.message.reply_text(TELEGRAM_LLM_ALREADY_READY_MESSAGE)
        await send_llm_success_card_reply(query.message, tender_id, existing_result)
        return

    if not bool(getattr(settings, "telegram_llm_generation_enabled", True)):
        logger.info(
            "TG_FLOW version=%s handler=llm_callback event=generation_disabled tender_id=%s external_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
        )
        await query.message.reply_text(TELEGRAM_LLM_GENERATION_DISABLED_MESSAGE)
        return

    logger.info(
        "TG_FLOW version=%s handler=llm_callback tender_id=%s external_id=%s existing_report=False selection_source=%s",
        TELEGRAM_UI_FLOW_VERSION,
        tender_id,
        external_id,
        SELECTION_SOURCE_GENERATE_NEW,
    )

    try:
        not_ready_message = await asyncio.to_thread(
            llm_document_not_ready_message,
            tender_id,
        )
    except Exception:
        logger.exception("Telegram LLM document readiness lookup failed")
        await query.message.reply_text(
            "Разбор не запущен: не удалось проверить готовность документов. "
            "Попробуйте позже."
        )
        return

    if not_ready_message:
        logger.info(
            "TG_FLOW version=%s handler=llm_callback event=document_not_ready tender_id=%s external_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
        )
        await query.message.reply_text(not_ready_message)
        return

    requested_by = telegram_requested_by(query)
    try:
        start_result = await asyncio.to_thread(
            begin_telegram_llm_analysis_run,
            tender_id=tender_id,
            external_id=external_id,
            requested_by=requested_by,
        )
    except Exception as exc:
        logger.exception("Telegram LLM analysis start failed")
        await query.message.reply_text(f"Ошибка разбора: {exc}")
        return

    if start_result.status == TELEGRAM_LLM_START_SAME_RUNNING:
        logger.info(
            "TG_FLOW version=%s handler=llm_callback event=already_running tender_id=%s external_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
        )
        await query.message.reply_text(TELEGRAM_LLM_SAME_RUNNING_MESSAGE)
        return

    if start_result.status == TELEGRAM_LLM_START_BUSY_OTHER:
        running_external_id = start_result.running.external_id if start_result.running else None
        logger.info(
            "TG_FLOW version=%s handler=llm_callback event=busy tender_id=%s external_id=%s running_external_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            tender_id,
            external_id,
            running_external_id,
        )
        await query.message.reply_text(TELEGRAM_LLM_BUSY_OTHER_MESSAGE)
        return

    if start_result.status == TELEGRAM_LLM_START_ALREADY_READY and start_result.existing_result:
        await query.message.reply_text(TELEGRAM_LLM_ALREADY_READY_MESSAGE)
        await send_llm_success_card_reply(
            query.message,
            tender_id,
            start_result.existing_result,
        )
        return

    background_coro = run_telegram_llm_analysis_background(
        message=query.message,
        lock=start_result.lock,
        tender_id=tender_id,
        external_id=external_id,
        requested_by=requested_by,
    )
    try:
        create_telegram_llm_background_task(context, background_coro)
    except Exception as exc:
        background_coro.close()
        logger.exception("Telegram LLM background task scheduling failed")
        await asyncio.to_thread(
            finish_telegram_llm_analysis_run,
            lock=start_result.lock,
            tender_id=tender_id,
            external_id=external_id,
            requested_by=requested_by,
            status="failed",
            error_message=f"background task scheduling failed: {exc}",
        )
        await query.message.reply_text(f"Ошибка разбора: {exc}")
        return

    logger.info(
        "TG_FLOW version=%s handler=llm_callback event=accepted tender_id=%s external_id=%s",
        TELEGRAM_UI_FLOW_VERSION,
        tender_id,
        external_id,
    )
    await query.message.reply_text(
        TELEGRAM_LLM_STARTED_MESSAGE_TEMPLATE.format(external_id=external_id)
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info(
        "TG_FLOW version=%s handler=start chat_id=%s reply_markup=True",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
    )

    await update.message.reply_text(
        (
            f"AI Tender Radar запущен.\n\n"
            f"Ваш chat_id: {chat_id}\n\n"
            "Кнопки внизу:\n"
            "Сегодня - новые клиентские лиды, созданные сегодня\n"
            "Необработанные лиды - go-лиды, которые еще не взяли в работу и не отклонили\n"
            "📌 В работе - лиды, которые уже взяли в работу\n"
            "📊 Excel Сегодня / Необработанные лиды / В работе - выгрузка очереди в .xlsx\n"
            "📥 Excel для Bitrix - лиды из 'В работе' в формате импорта CRM\n"
            "Аудит отбора - почему сигналы попали или не попали в очередь\n\n"
            "Команды: /excel_today, /excel_all, /excel_in_work, /excel_bitrix"
        ),
        reply_markup=main_menu_keyboard(),
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info(
        "TG_FLOW version=%s handler=chatid chat_id=%s reply_markup=False",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
    )

    await update.message.reply_text(f"chat_id: {chat_id}")


async def send_excel_export(update: Update, mode: str) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s handler=send_excel_export chat_id=%s mode=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        mode,
    )

    if not is_allowed(chat_id):
        logger.info(
            "TG_FLOW version=%s handler=send_excel_export chat_id=%s mode=%s denied=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
            mode,
        )
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    try:
        export_file = await asyncio.to_thread(build_excel_export_file, mode)
    except ExcelExportNoRows:
        logger.info(
            "TG_FLOW version=%s handler=send_excel_export mode=%s empty=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            mode,
        )
        await update.message.reply_text("Нет закупок для выгрузки.", reply_markup=main_menu_keyboard())
        return
    except Exception:
        logger.exception("Telegram Excel export generation failed: mode=%s", mode)
        await update.message.reply_text(
            "Не удалось сформировать Excel-выгрузку. Ошибка записана в лог.",
            reply_markup=main_menu_keyboard(),
        )
        return

    document = BytesIO(export_file.content)
    document.name = export_file.filename
    logger.info(
        "TG_FLOW version=%s handler=send_excel_export mode=%s rows=%s filename=%s",
        TELEGRAM_UI_FLOW_VERSION,
        mode,
        export_file.rows_count,
        export_file.filename,
    )
    await send_telegram_with_retry(
        update.message.reply_document,
        {
            "document": document,
            "filename": export_file.filename,
            "reply_markup": main_menu_keyboard(),
        },
        sender="send_excel_export",
        chat_id=chat_id,
        status=mode,
    )


async def excel_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_excel_export(update, EXCEL_EXPORT_TODAY)


async def excel_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_excel_export(update, EXCEL_EXPORT_ALL)


async def excel_in_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_excel_export(update, EXCEL_EXPORT_IN_WORK)


async def excel_bitrix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_excel_export(update, EXCEL_EXPORT_BITRIX_IN_WORK)


async def lead_note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = callback_query_chat_id(query)
    logger.info(
        "TG_FLOW version=%s handler=lead_note_callback chat_id=%s data=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        query.data,
    )

    if chat_id is None:
        await query.answer("Не удалось определить чат", show_alert=True)
        return

    if not is_allowed(chat_id):
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":", 1)
    if len(parts) != 2 or parts[0] != "note" or not parts[1]:
        await query.answer("Некорректная команда", show_alert=True)
        return

    tender_id = parts[1]
    set_pending_lead_note(context, tender_id)
    await query.answer("Жду примечание")
    await query.message.reply_text(LEAD_NOTE_PROMPT_TEXT)


async def cancel_lead_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = get_pending_lead_note(context)
    if pending:
        clear_pending_lead_note(context)
        await update.message.reply_text("Ок, примечание не менял.", reply_markup=main_menu_keyboard())
        return

    await update.message.reply_text("Активного примечания нет.", reply_markup=main_menu_keyboard())


async def lead_note_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = get_pending_lead_note(context)
    if not pending:
        return

    chat_id = update.effective_chat.id if update.effective_chat else None
    if not is_allowed(chat_id):
        clear_pending_lead_note(context)
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    if pending_lead_note_expired(pending):
        clear_pending_lead_note(context)
        await update.message.reply_text(
            "Время для примечания истекло. Нажми 📝 Примечание под карточкой еще раз.",
            reply_markup=main_menu_keyboard(),
        )
        return

    try:
        note_text = normalize_lead_note_text(update.message.text)
    except LeadNoteTooLong:
        await update.message.reply_text(
            f"Примечание слишком длинное. Лимит: {LEAD_NOTE_TEXT_LIMIT} символов.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if not note_text:
        await update.message.reply_text(
            "Пустое примечание не сохраняю. Напиши текст или отправь /cancel.",
            reply_markup=main_menu_keyboard(),
        )
        return

    user = update.effective_user
    tender_id = str(pending.get("tender_id") or "")
    user_name = telegram_user_display_name(user)

    try:
        await asyncio.to_thread(
            save_lead_note,
            tender_id=tender_id,
            note_text=note_text,
            user_name=user_name,
            chat_id=chat_id,
            user_id=getattr(user, "id", None),
        )
    except Exception:
        logger.exception("Failed to save lead note: tender_id=%s chat_id=%s", tender_id, chat_id)
        await update.message.reply_text(
            "Не удалось сохранить примечание. Ошибка записана в лог.",
            reply_markup=main_menu_keyboard(),
        )
        return

    clear_pending_lead_note(context)
    logger.info(
        "TG_FLOW version=%s handler=lead_note_message saved=True tender_id=%s chat_id=%s note_len=%s",
        TELEGRAM_UI_FLOW_VERSION,
        tender_id,
        chat_id,
        len(note_text),
    )
    await update.message.reply_text(
        "Примечание сохранено. Оно появится в следующей Excel-выгрузке.",
        reply_markup=main_menu_keyboard(),
    )


async def send_all_targets_digest(update: Update, limit: int | None = None) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s flow=send_all_leads_digest chat_id=%s limit=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        limit,
    )

    if not is_allowed(chat_id):
        logger.info(
            "TG_FLOW version=%s flow=send_all_leads_digest chat_id=%s denied=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    cards, stats = build_operational_lead_all_cards_with_stats(limit=limit)
    summary = operational_lead_all_summary_text(stats)
    logger.info(
        "TG_FLOW version=%s flow=send_all_leads_digest built_cards=%s new=%s hidden_in_work=%s hidden_negative=%s hidden_by_lead_triage_reject=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
        stats.new_count,
        stats.hidden_in_work,
        stats.hidden_negative_feedback,
        getattr(stats, "hidden_by_lead_triage_reject", 0),
    )

    if not cards:
        logger.info(
            "TG_FLOW version=%s flow=send_all_leads_digest empty=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
        )
        await update.message.reply_text(
            summary,
            reply_markup=main_menu_keyboard(),
        )
        return

    logger.info(
        "TG_FLOW version=%s flow=send_all_leads_digest summary_reply reply_markup=True cards=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
    )
    await update.message.reply_text(
        summary,
        reply_markup=main_menu_keyboard(),
    )

    await send_tender_cards(
        update,
        cards,
        keyboard_factory=all_leads_keyboard,
        keyboard_from_card=True,
    )


async def digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s handler=digest chat_id=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
    )

    await send_all_targets_digest(update, limit=None)


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s handler=recent chat_id=%s hours=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        TELEGRAM_RECENT_DIGEST_HOURS,
    )

    if not is_allowed(chat_id):
        logger.info(
            "TG_FLOW version=%s handler=recent chat_id=%s denied=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    cards, hidden_no_go, deadline_warning_count = build_recent_llm_digest_cards_with_stats(
        TELEGRAM_RECENT_DIGEST_HOURS,
        limit=15,
        include_no_go=False,
    )
    logger.info(
        "TG_FLOW version=%s handler=recent built_cards=%s hidden_no_go=%s deadline_warning_count=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
        hidden_no_go,
        deadline_warning_count,
    )

    if not cards:
        await update.message.reply_text(
            (
                f"AI Tender Radar: свежих рабочих LLM-отчетов за последние {TELEGRAM_RECENT_DIGEST_HOURS} ч нет. "
                f"Скрыто no_go: {hidden_no_go}. "
                f"С близким/истекшим сроком: {deadline_warning_count}."
            ),
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_text(
        (
            f"AI Tender Radar - свежие LLM-отчеты за последние {TELEGRAM_RECENT_DIGEST_HOURS} ч\n"
            f"Найдено свежих рабочих LLM-отчетов: {len(cards)}\n"
            f"Скрыто no_go: {hidden_no_go}\n"
            f"С близким/истекшим сроком: {deadline_warning_count}"
        ),
        reply_markup=main_menu_keyboard(),
    )

    await send_tender_cards(update, cards)


async def in_work(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s handler=in_work chat_id=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
    )

    if not is_allowed(chat_id):
        logger.info(
            "TG_FLOW version=%s handler=in_work chat_id=%s denied=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    cards, stats = build_operational_lead_in_work_cards_with_stats(limit=100)
    logger.info(
        "TG_FLOW version=%s handler=in_work built_cards=%s total_found=%s hidden_expired=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
        stats.total_found,
        stats.hidden_expired,
    )

    summary = operational_lead_in_work_summary_text(stats)
    if not cards:
        await update.message.reply_text(
            summary,
            reply_markup=main_menu_keyboard(),
        )
        return

    await update.message.reply_text(summary, reply_markup=main_menu_keyboard())
    await send_tender_cards(update, cards, keyboard_factory=in_work_keyboard)


async def digest_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s handler=digest_today chat_id=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
    )

    if not is_allowed(chat_id):
        logger.info(
            "TG_FLOW version=%s handler=digest_today chat_id=%s denied=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    cards, stats = build_operational_lead_queue_cards_with_stats(limit=100)
    logger.info(
        "TG_FLOW version=%s handler=digest_today built_cards=%s total_found=%s hidden_in_work=%s hidden_negative_feedback=%s hidden_by_lead_triage_reject=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
        stats.total_found,
        stats.hidden_in_work,
        stats.hidden_negative_feedback,
        getattr(stats, "hidden_by_lead_triage_reject", 0),
    )

    if not cards:
        logger.info(
            "TG_FLOW version=%s handler=digest_today empty=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
        )
        await update.message.reply_text(
            operational_lead_queue_summary_text(stats),
            reply_markup=main_menu_keyboard(),
        )
        return

    logger.info(
        "TG_FLOW version=%s handler=digest_today summary_reply reply_markup=True cards=%s",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
    )
    await update.message.reply_text(
        operational_lead_queue_summary_text(stats),
        reply_markup=main_menu_keyboard(),
    )

    await send_tender_cards(update, cards, keyboard_factory=lead_queue_keyboard)


async def digest_today_filter_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info(
        "TG_FLOW version=%s handler=digest_today_filter_audit chat_id=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
    )

    if not is_allowed(chat_id):
        logger.info(
            "TG_FLOW version=%s handler=digest_today_filter_audit chat_id=%s denied=True reply_markup=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await update.message.reply_text("Нет доступа к этому боту.", reply_markup=main_menu_keyboard())
        return

    cards, stats = build_today_filter_audit_cards_with_stats(limit=50)
    summary = today_filter_audit_summary_text(stats)
    logger.info(
        "TG_FLOW version=%s handler=digest_today_filter_audit cards=%s hidden_no_go=%s hidden_deadline=%s manual_documents=%s hidden_other=%s reply_markup=True",
        TELEGRAM_UI_FLOW_VERSION,
        len(cards),
        stats.hidden_no_go,
        stats.hidden_deadline,
        stats.manual_documents_needed,
        stats.hidden_other,
    )

    await update.message.reply_text(
        summary,
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )

    if cards:
        await send_filter_audit_cards(update, cards)



async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    choice = (update.message.text or "").strip()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if get_pending_lead_note(context):
        clear_pending_lead_note(context)
        logger.info(
            "TG_FLOW version=%s handler=menu_router chat_id=%s pending_lead_note_cancelled=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
    logger.info(
        "TG_FLOW version=%s handler=menu_router chat_id=%s choice=%r",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        choice,
    )

    if choice == "Сегодня":
        logger.info(
            "TG_FLOW version=%s handler=menu_router route=digest_today chat_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await digest_today(update, context)
        return

    if choice == "Необработанные лиды":
        logger.info(
            "TG_FLOW version=%s handler=menu_router route=send_all_leads_digest chat_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await send_all_targets_digest(update, limit=None)
        return

    if choice == "📌 В работе":
        logger.info(
            "TG_FLOW version=%s handler=menu_router route=in_work chat_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await in_work(update, context)
        return

    if choice == "Аудит отбора":
        logger.info(
            "TG_FLOW version=%s handler=menu_router route=digest_today_filter_audit chat_id=%s",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )
        await digest_today_filter_audit(update, context)
        return

    if choice in EXCEL_MENU_CHOICES:
        mode = EXCEL_MENU_CHOICES[choice]
        logger.info(
            "TG_FLOW version=%s handler=menu_router route=send_excel_export chat_id=%s mode=%s",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
            mode,
        )
        await send_excel_export(update, mode)
        return

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = callback_query_chat_id(query)
    logger.info(
        "TG_FLOW version=%s handler=feedback_callback chat_id=%s data=%s",
        TELEGRAM_UI_FLOW_VERSION,
        chat_id,
        query.data,
    )

    if chat_id is None:
        await query.answer("Не удалось определить чат", show_alert=True)
        return

    if not is_allowed(chat_id):
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")

    if len(parts) != 3 or parts[0] != "fb":
        await query.answer("Некорректная команда", show_alert=True)
        return

    _, tender_id, status = parts

    all_feedback_labels = {**FEEDBACK_LABELS, **FILTER_FEEDBACK_LABELS}
    if status not in all_feedback_labels:
        await query.answer("Неизвестный статус", show_alert=True)
        return

    if get_pending_lead_note(context):
        clear_pending_lead_note(context)
        logger.info(
            "TG_FLOW version=%s handler=feedback_callback chat_id=%s pending_lead_note_cancelled=True",
            TELEGRAM_UI_FLOW_VERSION,
            chat_id,
        )

    user = query.from_user
    user_name = telegram_user_display_name(user)

    try:
        save_feedback(
            tender_id=tender_id,
            status=status,
            user_name=user_name,
            chat_id=chat_id,
            user_id=getattr(user, "id", None),
        )
    except Exception as exc:
        logger.exception("Failed to save feedback")
        await query.answer(f"Ошибка сохранения: {exc}", show_alert=True)
        return

    label = all_feedback_labels[status]
    logger.info("Feedback saved: tender_id=%s status=%s user=%s", tender_id, status, user_name)

    if status in FILTER_FEEDBACK_ANSWERS:
        await query.answer(FILTER_FEEDBACK_ANSWERS[status])
        return

    if status == SALES_NOT_RELEVANT_STATUS:
        reply_text = SALES_WORKFLOW_REPLY_TEXTS[status]
        await query.answer(reply_text)
        await show_reject_reason_keyboard(query, context, tender_id, reply_text)
        return

    if status in SALES_WORKFLOW_REPLY_TEXTS:
        reply_text = SALES_WORKFLOW_REPLY_TEXTS[status]
        await query.answer(reply_text)
        await query.message.reply_text(reply_text)
        return

    if status in REJECT_REASON_STATUS_LABELS:
        reply_text = f"Причина сохранена: {label}. Лид остается скрытым из рабочих списков."
        await query.answer(f"Причина: {label}")
        await send_feedback_callback_message(
            query,
            context,
            reply_text,
            tender_id=tender_id,
            status=status,
        )
        return

    await query.answer(f"Сохранено: {label}")

    # Добавляем короткую отметку в чат, чтобы было видно, что реакция принята.
    await query.message.reply_text(f"Обратная связь сохранена: {label}")


def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    allowed_chat_ids = get_allowed_chat_ids()
    if not allowed_chat_ids:
        raise RuntimeError("TELEGRAM_ALLOWED_CHAT_IDS is not set")

    logger.info(
        "TG_FLOW version=%s event=bot_start module=%s allowed_chat_count=%s",
        TELEGRAM_UI_FLOW_VERSION,
        __name__,
        len(allowed_chat_ids),
    )

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .request(build_request())
        .get_updates_request(build_request())
        .post_init(setup_bot_menu)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("digest", digest))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("in_work", in_work))
    app.add_handler(CommandHandler("digest_today", digest_today))
    app.add_handler(CommandHandler("excel_today", excel_today))
    app.add_handler(CommandHandler("excel_all", excel_all))
    app.add_handler(CommandHandler("excel_in_work", excel_in_work))
    app.add_handler(CommandHandler("excel_bitrix", excel_bitrix))
    app.add_handler(CommandHandler("cancel", cancel_lead_note))
    app.add_handler(MessageHandler(filters.Regex(r"^(Сегодня|Необработанные лиды|📌 В работе|📊 Excel Сегодня|📊 Excel Необработанные лиды|📊 Excel В работе|📥 Excel для Bitrix|Аудит отбора)$"), menu_router))
    app.add_handler(CallbackQueryHandler(llm_callback, pattern=r"^llm:"))
    app.add_handler(CallbackQueryHandler(lead_note_callback, pattern=r"^note:"))
    app.add_handler(CallbackQueryHandler(feedback_callback, pattern=r"^fb:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lead_note_message))

    logger.info(
        "TG_FLOW version=%s event=run_polling handlers=start,menu,chatid,digest,recent,in_work,digest_today,excel,menu_router,llm,lead_note,feedback",
        TELEGRAM_UI_FLOW_VERSION,
    )
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
