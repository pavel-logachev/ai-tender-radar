from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch


class Dummy:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __getattr__(self, name: str):
        return self

    def __call__(self, *args, **kwargs):
        return self


STUBBED_MODULE_NAMES = (
    "psycopg",
    "telegram",
    "telegram.ext",
    "telegram.request",
    "app.config",
    "app.digest",
    "app.excel_export",
    "app.lead_notes",
    "app.llm.tender_report",
    "app.pipeline.prepare_tender_for_analysis",
    "app.telegram_bot",
)
MISSING_MODULE = object()
ORIGINAL_MODULES = {
    name: sys.modules.get(name, MISSING_MODULE)
    for name in STUBBED_MODULE_NAMES
}
FORBIDDEN_ROLE_WORD = "".join(
    chr(code) for code in (0x0437, 0x0432, 0x043E, 0x043D, 0x0430, 0x0440, 0x044C)
)


def restore_telegram_formatter_stubs() -> None:
    for name, module in ORIGINAL_MODULES.items():
        if module is MISSING_MODULE:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def install_telegram_formatter_stubs() -> None:
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda *args, **kwargs: Dummy()
    sys.modules["psycopg"] = psycopg

    telegram = types.ModuleType("telegram")
    telegram.BotCommand = Dummy
    telegram.InlineKeyboardButton = Dummy
    telegram.InlineKeyboardMarkup = Dummy
    telegram.MenuButtonCommands = Dummy
    telegram.ReplyKeyboardMarkup = Dummy
    telegram.Update = Dummy
    sys.modules["telegram"] = telegram

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.ApplicationBuilder = Dummy
    telegram_ext.CallbackQueryHandler = Dummy
    telegram_ext.CommandHandler = Dummy
    telegram_ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    telegram_ext.MessageHandler = Dummy
    telegram_ext.filters = Dummy()
    sys.modules["telegram.ext"] = telegram_ext

    telegram_request = types.ModuleType("telegram.request")
    telegram_request.HTTPXRequest = Dummy
    sys.modules["telegram.request"] = telegram_request

    config = types.ModuleType("app.config")
    config.settings = types.SimpleNamespace(
        telegram_proxy_url=None,
        telegram_allowed_chat_ids="",
        telegram_bot_token="token",
        agent_radar_telegram_bundle=None,
        agent_radar_telegram_review_queue=None,
        database_url="",
    )
    sys.modules["app.config"] = config

    digest = types.ModuleType("app.digest")
    digest.DEFAULT_DEADLINE_MIN_DAYS = 3.0
    digest.TenderDigestCard = Dummy
    digest.TodayFilterAuditCard = Dummy
    digest.TodayDigestStats = Dummy
    digest.OperationalLeadQueueStats = Dummy
    digest.OperationalLeadInWorkStats = Dummy
    digest.OperationalLeadAllStats = Dummy
    digest.build_operational_lead_queue_cards_with_stats = lambda *args, **kwargs: (
        [],
        types.SimpleNamespace(
            total_found=0,
            shown=0,
            hidden_in_work=0,
            hidden_negative_feedback=0,
        ),
    )
    digest.build_operational_lead_in_work_cards_with_stats = lambda *args, **kwargs: (
        [],
        types.SimpleNamespace(total_found=0, shown=0, hidden_expired=0),
    )
    digest.build_operational_lead_all_cards_with_stats = lambda *args, **kwargs: (
        [],
        types.SimpleNamespace(
            total_found=0,
            shown=0,
            new_count=0,
            in_work_count=0,
            hidden_in_work=0,
            hidden_negative_feedback=0,
        ),
    )
    digest.build_digest_cards_today_with_stats = lambda *args, **kwargs: (
        [],
        [],
        types.SimpleNamespace(
            total_found=0,
            shown=0,
            manual_documents_needed=0,
            hidden_no_go=0,
            hidden_deadline=0,
            hidden_preparation=0,
        ),
    )
    digest.build_digest_cards_with_stats = lambda *args, **kwargs: ([], 0)
    digest.build_in_work_digest_cards_with_stats = lambda *args, **kwargs: ([], 0)
    digest.build_tender_digest_card = lambda *args, **kwargs: None
    digest.build_recent_llm_digest_cards_with_stats = lambda *args, **kwargs: ([], 0, 0)
    digest.get_tender_digest_row = lambda *args, **kwargs: None
    digest.manual_document_not_ready_message = lambda reason=None: (
        "Документы для LLM-разбора не готовы: документы отсутствуют или не готовы. "
        "Скачайте КД/ТЗ/спецификацию и загрузите документ вручную."
    )
    digest.manual_document_reason_for_tender = lambda tender: None
    digest.tender_has_extracted_document_text = lambda tender: True
    digest.build_today_filter_audit_cards_with_stats = lambda *args, **kwargs: (
        [],
        types.SimpleNamespace(
            total_found=0,
            would_show=0,
            manual_documents_needed=0,
            hidden_no_go=0,
            hidden_deadline=0,
            hidden_other=0,
            listed=0,
            limit=50,
        ),
    )
    digest.build_today_filter_audit_messages = lambda *args, **kwargs: [
        "AI Tender Radar - Аудит отбора"
    ]
    digest.today_filter_audit_summary_text = lambda stats, **kwargs: (
        "AI Tender Radar - Аудит отбора\n"
        "Отобраны как лиды: 0\n"
        "Отложены: 0\n"
        "Отклонены triage: 0"
    )
    digest.today_digest_summary_text = lambda stats, **kwargs: (
        "AI Tender Radar - найдено сегодня\n"
        f"Найдено сегодня всего: {stats.total_found}\n"
        f"Показано рабочих кандидатов: {stats.shown}\n"
        f"Нужны документы для LLM-разбора: {stats.manual_documents_needed}\n"
        f"Скрыто no_go: {stats.hidden_no_go}\n"
        f"С близким/истекшим сроком: {getattr(stats, 'deadline_warning', getattr(stats, 'hidden_deadline', 0))}\n"
        f"Скрыто по документам/доступу: {stats.hidden_preparation}"
    )
    digest.operational_lead_queue_summary_text = lambda stats: (
        "AI Tender Radar - новые клиентские лиды сегодня\n"
        f"Новых лидов сегодня: {stats.shown}\n"
        f"Скрыто как \"в работе\": {stats.hidden_in_work}\n"
        f"Скрыто негативным feedback: {stats.hidden_negative_feedback}"
    )
    digest.operational_lead_in_work_summary_text = lambda stats: (
        "AI Tender Radar - лиды в работе\n"
        f"Лидов в работе: {stats.shown}"
        + (f"\nСкрыто просроченных: {stats.hidden_expired}" if stats.hidden_expired else "")
    )
    digest.operational_lead_all_summary_text = lambda stats: (
        "AI Tender Radar - необработанные клиентские лиды\n"
        f"Необработанных лидов в очереди: {stats.new_count}\n"
        f"Скрыто как \"в работе\": {stats.hidden_in_work}\n"
        f"Исключено негативным feedback: {stats.hidden_negative_feedback}"
    )
    sys.modules["app.digest"] = digest

    excel_export = types.ModuleType("app.excel_export")
    excel_export.EXCEL_EXPORT_TODAY = "today"
    excel_export.EXCEL_EXPORT_ALL = "all"
    excel_export.EXCEL_EXPORT_IN_WORK = "in_work"
    excel_export.EXCEL_EXPORT_BITRIX_IN_WORK = "bitrix_in_work"

    class ExcelExportNoRows(ValueError):
        pass

    excel_export.ExcelExportNoRows = ExcelExportNoRows
    excel_export.build_excel_export_file = lambda mode: types.SimpleNamespace(
        filename=f"{mode}.xlsx",
        content=b"excel",
        rows_count=1,
    )
    sys.modules["app.excel_export"] = excel_export

    tender_report = types.ModuleType("app.llm.tender_report")
    tender_report.generate_presales_report = lambda *args, **kwargs: {}
    sys.modules["app.llm.tender_report"] = tender_report

    prepare = types.ModuleType("app.pipeline.prepare_tender_for_analysis")
    prepare.PreparationStatus = Dummy
    prepare.TenderPreparationResult = Dummy
    prepare.prepare_tender_for_analysis = lambda *args, **kwargs: Dummy()
    sys.modules["app.pipeline.prepare_tender_for_analysis"] = prepare


install_telegram_formatter_stubs()
sys.modules.pop("app.telegram_bot", None)
sys.modules.pop("app.lead_notes", None)
telegram_bot = importlib.import_module("app.telegram_bot")
restore_telegram_formatter_stubs()


class TelegramFormatterTest(unittest.TestCase):
    def test_empty_allowlist_denies_access(self) -> None:
        with patch.object(telegram_bot.settings, "telegram_allowed_chat_ids", ""):
            self.assertEqual(telegram_bot.get_allowed_chat_ids(), set())
            self.assertFalse(telegram_bot.is_allowed(123))

    def test_allowlist_permits_only_configured_chats(self) -> None:
        with patch.object(
            telegram_bot.settings,
            "telegram_allowed_chat_ids",
            "123, -456",
        ):
            self.assertEqual(telegram_bot.get_allowed_chat_ids(), {123, -456})
            self.assertTrue(telegram_bot.is_allowed(123))
            self.assertTrue(telegram_bot.is_allowed(-456))
            self.assertFalse(telegram_bot.is_allowed(789))

    def test_invalid_allowlist_fails_closed(self) -> None:
        with patch.object(
            telegram_bot.settings,
            "telegram_allowed_chat_ids",
            "123,not-a-chat",
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "TELEGRAM_ALLOWED_CHAT_IDS must contain only comma-separated integers",
            ):
                telegram_bot.get_allowed_chat_ids()

    def test_bot_start_requires_nonempty_allowlist(self) -> None:
        with (
            patch.object(telegram_bot.settings, "telegram_bot_token", "token"),
            patch.object(telegram_bot.settings, "telegram_allowed_chat_ids", ""),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "TELEGRAM_ALLOWED_CHAT_IDS is not set",
            ):
                telegram_bot.main()

    def make_llm_update(self, tender_id: str = "tender-1"):
        replies = []
        answers = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        query = types.SimpleNamespace(
            message=Message(),
            data=f"llm:{tender_id}",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=answers,
        )

        async def answer(text, **kwargs):
            answers.append((text, kwargs))

        query.answer = answer
        return types.SimpleNamespace(callback_query=query), replies, answers

    def test_main_menu_contains_today_digest_button(self) -> None:
        self.assertIn(["Сегодня", "Необработанные лиды"], telegram_bot.MENU_BUTTONS)
        self.assertIn(["📌 В работе"], telegram_bot.MENU_BUTTONS)
        self.assertIn(["📊 Excel Сегодня", "📊 Excel Необработанные лиды"], telegram_bot.MENU_BUTTONS)
        self.assertIn(["📊 Excel В работе", "📥 Excel для Bitrix"], telegram_bot.MENU_BUTTONS)
        self.assertIn(["Аудит отбора"], telegram_bot.MENU_BUTTONS)
        flat_menu = [item for row in telegram_bot.MENU_BUTTONS for item in row]
        self.assertNotIn("Все лиды", flat_menu)
        self.assertNotIn("Открытые лиды", flat_menu)
        self.assertNotIn("Все целевые", flat_menu)
        self.assertNotIn("Проверка фильтра", flat_menu)
        self.assertFalse(any(FORBIDDEN_ROLE_WORD in item.lower() for item in flat_menu))

    def test_bot_command_menu_contains_open_menu_and_core_actions(self) -> None:
        commands = [command for command, _description in telegram_bot.TELEGRAM_COMMAND_MENU]

        self.assertEqual(
            commands,
            [
                "start",
                "menu",
                "digest_today",
                "digest",
                "in_work",
                "excel_today",
                "excel_all",
                "excel_in_work",
                "excel_bitrix",
                "recent",
                "chatid",
                "cancel",
            ],
        )
        self.assertEqual(len(commands), len(set(commands)))
        self.assertTrue(all(command.islower() for command in commands))
        self.assertTrue(all(1 <= len(command) <= 32 for command in commands))
        self.assertTrue(all(description for _command, description in telegram_bot.TELEGRAM_COMMAND_MENU))

    def test_setup_bot_menu_registers_commands_and_menu_button(self) -> None:
        class Command:
            def __init__(self, *, command, description):
                self.command = command
                self.description = description

        class MenuButton:
            pass

        class Bot:
            def __init__(self):
                self.commands = None
                self.menu_button = None

            async def set_my_commands(self, commands):
                self.commands = commands
                return True

            async def set_chat_menu_button(self, *, menu_button):
                self.menu_button = menu_button
                return True

        bot = Bot()

        with (
            patch.object(telegram_bot, "BotCommand", Command),
            patch.object(telegram_bot, "MenuButtonCommands", MenuButton),
        ):
            asyncio.run(telegram_bot.setup_bot_menu(types.SimpleNamespace(bot=bot)))

        self.assertEqual(
            [command.command for command in bot.commands],
            [command for command, _description in telegram_bot.TELEGRAM_COMMAND_MENU],
        )
        self.assertIsInstance(bot.menu_button, MenuButton)

    def test_menu_today_routes_to_digest_today_flow(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="Сегодня"),
        )

        with (
            patch.object(telegram_bot, "digest_today", new=AsyncMock()) as today_handler,
            patch.object(telegram_bot, "send_all_targets_digest", new=AsyncMock()) as all_targets_handler,
        ):
            asyncio.run(telegram_bot.menu_router(update, types.SimpleNamespace()))

        today_handler.assert_awaited_once()
        all_targets_handler.assert_not_awaited()

    def test_menu_choice_clears_pending_lead_note_before_routing(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="Сегодня"),
        )
        context = types.SimpleNamespace(
            user_data={
                telegram_bot.LEAD_NOTE_PENDING_KEY: {
                    "tender_id": "tender-1",
                    "created_at": datetime.now(timezone.utc),
                }
            }
        )

        with patch.object(telegram_bot, "digest_today", new=AsyncMock()) as today_handler:
            asyncio.run(telegram_bot.menu_router(update, context))

        today_handler.assert_awaited_once()
        self.assertNotIn(telegram_bot.LEAD_NOTE_PENDING_KEY, context.user_data)

    def test_menu_all_targets_routes_to_full_backlog_digest(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="Необработанные лиды"),
        )

        with (
            patch.object(telegram_bot, "digest_today", new=AsyncMock()) as today_handler,
            patch.object(telegram_bot, "send_all_targets_digest", new=AsyncMock()) as all_targets_handler,
            patch.object(telegram_bot, "digest_today_filter_audit", new=AsyncMock()) as audit_handler,
        ):
            asyncio.run(telegram_bot.menu_router(update, types.SimpleNamespace()))

        all_targets_handler.assert_awaited_once_with(update, limit=None)
        today_handler.assert_not_awaited()
        audit_handler.assert_not_awaited()

    def test_menu_in_work_routes_to_in_work_flow(self) -> None:
        context = types.SimpleNamespace()
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="📌 В работе"),
        )

        with (
            patch.object(telegram_bot, "digest_today", new=AsyncMock()) as today_handler,
            patch.object(telegram_bot, "send_all_targets_digest", new=AsyncMock()) as all_targets_handler,
            patch.object(telegram_bot, "in_work", new=AsyncMock()) as in_work_handler,
            patch.object(telegram_bot, "digest_today_filter_audit", new=AsyncMock()) as audit_handler,
        ):
            asyncio.run(telegram_bot.menu_router(update, context))

        in_work_handler.assert_awaited_once_with(update, context)
        today_handler.assert_not_awaited()
        all_targets_handler.assert_not_awaited()
        audit_handler.assert_not_awaited()

    def test_menu_filter_audit_routes_to_debug_flow(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="Аудит отбора"),
        )

        with (
            patch.object(telegram_bot, "digest_today", new=AsyncMock()) as today_handler,
            patch.object(telegram_bot, "send_all_targets_digest", new=AsyncMock()) as all_targets_handler,
            patch.object(telegram_bot, "digest_today_filter_audit", new=AsyncMock()) as audit_handler,
        ):
            asyncio.run(telegram_bot.menu_router(update, types.SimpleNamespace()))

        audit_handler.assert_awaited_once()
        today_handler.assert_not_awaited()
        all_targets_handler.assert_not_awaited()

    def test_menu_excel_today_routes_to_excel_export(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="📊 Excel Сегодня"),
        )

        with patch.object(telegram_bot, "send_excel_export", new=AsyncMock()) as excel_handler:
            asyncio.run(telegram_bot.menu_router(update, types.SimpleNamespace()))

        excel_handler.assert_awaited_once_with(update, telegram_bot.EXCEL_EXPORT_TODAY)

    def test_menu_excel_all_leads_routes_to_excel_export(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="📊 Excel Необработанные лиды"),
        )

        with patch.object(telegram_bot, "send_excel_export", new=AsyncMock()) as excel_handler:
            asyncio.run(telegram_bot.menu_router(update, types.SimpleNamespace()))

        excel_handler.assert_awaited_once_with(update, telegram_bot.EXCEL_EXPORT_ALL)

    def test_menu_excel_bitrix_routes_to_bitrix_export(self) -> None:
        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=types.SimpleNamespace(text="📥 Excel для Bitrix"),
        )

        with patch.object(telegram_bot, "send_excel_export", new=AsyncMock()) as excel_handler:
            asyncio.run(telegram_bot.menu_router(update, types.SimpleNamespace()))

        excel_handler.assert_awaited_once_with(update, telegram_bot.EXCEL_EXPORT_BITRIX_IN_WORK)

    def test_excel_commands_route_to_export_modes(self) -> None:
        update = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=123), message=types.SimpleNamespace())
        context = types.SimpleNamespace()

        with patch.object(telegram_bot, "send_excel_export", new=AsyncMock()) as excel_handler:
            asyncio.run(telegram_bot.excel_today(update, context))
            asyncio.run(telegram_bot.excel_all(update, context))
            asyncio.run(telegram_bot.excel_in_work(update, context))
            asyncio.run(telegram_bot.excel_bitrix(update, context))

        self.assertEqual(
            [call.args for call in excel_handler.await_args_list],
            [
                (update, telegram_bot.EXCEL_EXPORT_TODAY),
                (update, telegram_bot.EXCEL_EXPORT_ALL),
                (update, telegram_bot.EXCEL_EXPORT_IN_WORK),
                (update, telegram_bot.EXCEL_EXPORT_BITRIX_IN_WORK),
            ],
        )

    def test_excel_export_empty_rows_sends_clear_message(self) -> None:
        replies = []
        documents = []

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

            async def reply_document(self, **kwargs):
                documents.append(kwargs)

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_excel_export_file",
                side_effect=telegram_bot.ExcelExportNoRows(),
            ),
        ):
            asyncio.run(telegram_bot.send_excel_export(update, telegram_bot.EXCEL_EXPORT_TODAY))

        self.assertEqual([reply[0] for reply in replies], ["Нет закупок для выгрузки."])
        self.assertEqual(documents, [])

    def test_send_telegram_with_retry_retries_network_error(self) -> None:
        attempts = []

        class NetworkError(Exception):
            pass

        async def send_message(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise NetworkError("temporary proxy error")

        with patch.object(telegram_bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            asyncio.run(
                telegram_bot.send_telegram_with_retry(
                    send_message,
                    {"text": "ok"},
                    sender="unit_test",
                    network_retry_attempts=1,
                )
            )

        self.assertEqual(len(attempts), 2)
        sleep.assert_awaited_once_with(telegram_bot.TELEGRAM_NETWORK_RETRY_BASE_SECONDS)

    def test_excel_export_retries_network_error_and_rewinds_document(self) -> None:
        attempts = []
        document_positions = []

        class NetworkError(Exception):
            pass

        class Message:
            async def reply_text(self, text, **kwargs):
                raise AssertionError("reply_text should not be called")

            async def reply_document(self, **kwargs):
                document = kwargs["document"]
                document_positions.append(document.tell())
                document.read()
                attempts.append(kwargs)
                if len(attempts) == 1:
                    raise NetworkError("temporary proxy error")

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )
        export_file = types.SimpleNamespace(
            filename="all.xlsx",
            content=b"excel",
            rows_count=1,
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "build_excel_export_file", return_value=export_file),
            patch.object(telegram_bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            asyncio.run(telegram_bot.send_excel_export(update, telegram_bot.EXCEL_EXPORT_ALL))

        self.assertEqual(len(attempts), 2)
        self.assertEqual(document_positions, [0, 0])
        self.assertEqual([attempt["filename"] for attempt in attempts], ["all.xlsx", "all.xlsx"])
        sleep.assert_awaited_once_with(telegram_bot.TELEGRAM_NETWORK_RETRY_BASE_SECONDS)

    def test_hidden_filter_audit_keyboard_is_read_only(self) -> None:
        card = types.SimpleNamespace(tender_id="tender-hidden", status="СКРЫТО")

        keyboard = telegram_bot.filter_audit_keyboard(card)

        self.assertIsNone(keyboard)

    def test_docs_filter_audit_keyboard_is_read_only(self) -> None:
        card = types.SimpleNamespace(tender_id="tender-docs", status="НУЖНЫ ДОКУМЕНТЫ")

        keyboard = telegram_bot.filter_audit_keyboard(card)

        self.assertIsNone(keyboard)

    def test_shown_filter_audit_card_has_no_diagnostic_keyboard(self) -> None:
        card = types.SimpleNamespace(tender_id="tender-shown", status="ПОКАЗАНО")

        self.assertIsNone(telegram_bot.filter_audit_keyboard(card))

    def test_today_working_card_keyboard_has_only_sales_workflow_buttons(self) -> None:
        class Button:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.rows = rows

        with (
            patch.object(telegram_bot, "InlineKeyboardButton", Button),
            patch.object(telegram_bot, "InlineKeyboardMarkup", Markup),
        ):
            keyboard = telegram_bot.lead_queue_keyboard("tender-1")

        buttons = [button for row in keyboard.rows for button in row]
        self.assertEqual(
            [(button.text, button.callback_data) for button in buttons],
            [
                ("✅ В работу", "fb:tender-1:sales_in_work"),
                ("🚫 Мимо", "fb:tender-1:sales_not_relevant"),
                ("🏢 Существующий клиент", "fb:tender-1:sales_existing_client"),
                ("📝 Примечание", "note:tender-1"),
            ],
        )
        self.assertNotIn("🔎 Разобрать", [button.text for button in buttons])
        self.assertFalse(any(str(button.callback_data).startswith("llm:") for button in buttons))

    def test_business_keyboard_has_only_sales_workflow_buttons(self) -> None:
        class Button:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.rows = rows

        with (
            patch.object(telegram_bot, "InlineKeyboardButton", Button),
            patch.object(telegram_bot, "InlineKeyboardMarkup", Markup),
        ):
            keyboard = telegram_bot.business_keyboard("tender-2")

        buttons = [button for row in keyboard.rows for button in row]
        self.assertEqual(
            [(button.text, button.callback_data) for button in buttons],
            [
                ("✅ В работу", "fb:tender-2:sales_in_work"),
                ("🚫 Мимо", "fb:tender-2:sales_not_relevant"),
                ("🏢 Существующий клиент", "fb:tender-2:sales_existing_client"),
                ("📝 Примечание", "note:tender-2"),
            ],
        )
        self.assertNotIn("🔎 Разобрать", [button.text for button in buttons])

    def test_document_needed_lead_card_keyboard_has_only_sales_workflow_buttons(self) -> None:
        class Button:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.rows = rows

        with (
            patch.object(telegram_bot, "InlineKeyboardButton", Button),
            patch.object(telegram_bot, "InlineKeyboardMarkup", Markup),
        ):
            keyboard = telegram_bot.lead_queue_keyboard("manual-doc")

        buttons = [button for row in keyboard.rows for button in row]
        self.assertEqual(
            [button.text for button in buttons],
            ["✅ В работу", "🚫 Мимо", "🏢 Существующий клиент", "📝 Примечание"],
        )

    def test_lead_note_callback_sets_pending_note_and_prompts(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        query = types.SimpleNamespace(
            message=Message(),
            data="note:tender-1",
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)
        context = types.SimpleNamespace(user_data={})

        with patch.object(telegram_bot, "is_allowed", return_value=True):
            asyncio.run(telegram_bot.lead_note_callback(update, context))

        self.assertEqual(query.answers[0][0], "Жду примечание")
        self.assertEqual(replies, [(telegram_bot.LEAD_NOTE_PROMPT_TEXT, {})])
        self.assertEqual(
            context.user_data[telegram_bot.LEAD_NOTE_PENDING_KEY]["tender_id"],
            "tender-1",
        )

    def test_lead_note_message_saves_note_and_clears_pending_state(self) -> None:
        replies = []

        class Message:
            text = "Позвонить ИТ-директору после праздников"

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            effective_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            message=Message(),
        )
        context = types.SimpleNamespace(
            user_data={
                telegram_bot.LEAD_NOTE_PENDING_KEY: {
                    "tender_id": "tender-1",
                    "created_at": datetime.now(timezone.utc),
                }
            }
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_lead_note") as save_note,
        ):
            asyncio.run(telegram_bot.lead_note_message(update, context))

        save_note.assert_called_once_with(
            tender_id="tender-1",
            note_text="Позвонить ИТ-директору после праздников",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        self.assertNotIn(telegram_bot.LEAD_NOTE_PENDING_KEY, context.user_data)
        self.assertEqual(replies[0][0], "Примечание сохранено. Оно появится в следующей Excel-выгрузке.")

    def test_lead_note_message_rejects_too_long_note_without_clearing_pending_state(self) -> None:
        replies = []

        class Message:
            text = "x" * (telegram_bot.LEAD_NOTE_TEXT_LIMIT + 1)

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            effective_user=types.SimpleNamespace(username="tester", id=321),
            message=Message(),
        )
        context = types.SimpleNamespace(
            user_data={
                telegram_bot.LEAD_NOTE_PENDING_KEY: {
                    "tender_id": "tender-1",
                    "created_at": datetime.now(timezone.utc),
                }
            }
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_lead_note") as save_note,
        ):
            asyncio.run(telegram_bot.lead_note_message(update, context))

        save_note.assert_not_called()
        self.assertIn(telegram_bot.LEAD_NOTE_PENDING_KEY, context.user_data)
        self.assertEqual(
            replies[0][0],
            f"Примечание слишком длинное. Лимит: {telegram_bot.LEAD_NOTE_TEXT_LIMIT} символов.",
        )

    def test_lead_note_message_expires_pending_state_without_saving(self) -> None:
        replies = []

        class Message:
            text = "Позвонить позже"

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            effective_user=types.SimpleNamespace(username="tester", id=321),
            message=Message(),
        )
        context = types.SimpleNamespace(
            user_data={
                telegram_bot.LEAD_NOTE_PENDING_KEY: {
                    "tender_id": "tender-1",
                    "created_at": (
                        datetime.now(timezone.utc)
                        - telegram_bot.LEAD_NOTE_PENDING_TTL
                        - timedelta(seconds=1)
                    ),
                }
            }
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_lead_note") as save_note,
        ):
            asyncio.run(telegram_bot.lead_note_message(update, context))

        save_note.assert_not_called()
        self.assertNotIn(telegram_bot.LEAD_NOTE_PENDING_KEY, context.user_data)
        self.assertEqual(
            replies[0][0],
            "Время для примечания истекло. Нажми 📝 Примечание под карточкой еще раз.",
        )

    def test_lead_note_message_reports_save_failure_without_logging_note_text(self) -> None:
        replies = []

        class Message:
            text = "Очень приватное примечание"

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            effective_user=types.SimpleNamespace(username="tester", id=321),
            message=Message(),
        )
        context = types.SimpleNamespace(
            user_data={
                telegram_bot.LEAD_NOTE_PENDING_KEY: {
                    "tender_id": "tender-1",
                    "created_at": datetime.now(timezone.utc),
                }
            }
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_lead_note", side_effect=RuntimeError("db down")),
        ):
            with self.assertLogs(telegram_bot.logger, level="ERROR") as logs:
                asyncio.run(telegram_bot.lead_note_message(update, context))

        self.assertIn(telegram_bot.LEAD_NOTE_PENDING_KEY, context.user_data)
        self.assertEqual(replies[0][0], "Не удалось сохранить примечание. Ошибка записана в лог.")
        self.assertNotIn("Очень приватное примечание", "\n".join(logs.output))

    def test_reject_reason_keyboard_has_expected_buttons(self) -> None:
        class Button:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.rows = rows

        with (
            patch.object(telegram_bot, "InlineKeyboardButton", Button),
            patch.object(telegram_bot, "InlineKeyboardMarkup", Markup),
        ):
            keyboard = telegram_bot.reject_reason_keyboard("tender-1")

        buttons = [button for row in keyboard.rows for button in row]
        self.assertEqual(
            [(button.text, button.callback_data) for button in buttons],
            [
                ("💸 Маленький чек", "fb:tender-1:bad_price"),
                ("🌍 Не наш регион", "fb:tender-1:bad_region"),
                ("🧩 Не наш профиль", "fb:tender-1:not_profile"),
                ("🛠️ Услуги/ПО", "fb:tender-1:service_only"),
                ("📦 Разовая мелочь", "fb:tender-1:low_value_one_off"),
                ("🏛️ Заказчик не подходит", "fb:tender-1:bad_customer_fit"),
                ("⏰ Поздно/не сейчас", "fb:tender-1:bad_timing"),
                ("📞 Дубль/уже обработали", "fb:tender-1:duplicate_or_processed"),
                ("❓ Другое", "fb:tender-1:other_reject"),
            ],
        )

    def test_in_work_card_keyboard_has_only_return_button(self) -> None:
        class Button:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.rows = rows

        with (
            patch.object(telegram_bot, "InlineKeyboardButton", Button),
            patch.object(telegram_bot, "InlineKeyboardMarkup", Markup),
        ):
            keyboard = telegram_bot.in_work_keyboard("tender-1")

        buttons = [button for row in keyboard.rows for button in row]
        self.assertEqual(
            [(button.text, button.callback_data) for button in buttons],
            [
                ("↩️ Вернуть в список", "fb:tender-1:sales_returned"),
                ("🚫 Мимо", "fb:tender-1:sales_not_relevant"),
                ("🏢 Существующий клиент", "fb:tender-1:sales_existing_client"),
                ("📝 Примечание", "note:tender-1"),
            ],
        )

    def test_all_leads_keyboard_uses_status_without_llm_button(self) -> None:
        class Button:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data

        class Markup:
            def __init__(self, rows):
                self.rows = rows

        with (
            patch.object(telegram_bot, "InlineKeyboardButton", Button),
            patch.object(telegram_bot, "InlineKeyboardMarkup", Markup),
        ):
            new_keyboard = telegram_bot.all_leads_keyboard(
                types.SimpleNamespace(tender_id="new", message_text="Статус: Новый\ncard")
            )
            in_work_keyboard = telegram_bot.all_leads_keyboard(
                types.SimpleNamespace(tender_id="work", message_text="Статус: В работе\ncard")
            )

        new_buttons = [button for row in new_keyboard.rows for button in row]
        work_buttons = [button for row in in_work_keyboard.rows for button in row]
        self.assertEqual(
            [button.text for button in new_buttons],
            ["✅ В работу", "🚫 Мимо", "🏢 Существующий клиент", "📝 Примечание"],
        )
        self.assertEqual(
            [button.text for button in work_buttons],
            ["↩️ Вернуть в список", "🚫 Мимо", "🏢 Существующий клиент", "📝 Примечание"],
        )
        self.assertFalse(
            any(str(button.callback_data).startswith("llm:") for button in new_buttons + work_buttons)
        )

    def test_send_card_message_retries_after_telegram_rate_limit(self) -> None:
        sent = []

        class RetryAfter(Exception):
            retry_after = 2

        class Message:
            async def reply_text(self, **kwargs):
                sent.append(kwargs)
                if len(sent) == 1:
                    raise RetryAfter()

        with patch.object(telegram_bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            asyncio.run(
                telegram_bot.send_card_message(
                    Message(),
                    "card text",
                    None,
                    tender_id="tender-1",
                )
            )

        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["text"], "card text")
        self.assertEqual(sent[1]["text"], "card text")
        sleep.assert_awaited_once_with(
            RetryAfter.retry_after + telegram_bot.TELEGRAM_RETRY_AFTER_BUFFER_SECONDS
        )

    def test_send_card_message_honors_long_telegram_flood_control_pause(self) -> None:
        sent = []

        class RetryAfter(Exception):
            retry_after = 178

        class Message:
            async def reply_text(self, **kwargs):
                sent.append(kwargs)
                if len(sent) == 1:
                    raise RetryAfter()

        with patch.object(telegram_bot.asyncio, "sleep", new=AsyncMock()) as sleep:
            asyncio.run(
                telegram_bot.send_card_message(
                    Message(),
                    "card text",
                    None,
                    tender_id="tender-1",
                )
            )

        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["text"], "card text")
        self.assertEqual(sent[1]["text"], "card text")
        sleep.assert_awaited_once_with(
            RetryAfter.retry_after + telegram_bot.TELEGRAM_RETRY_AFTER_BUFFER_SECONDS
        )

    def test_send_tender_cards_paces_bulk_delivery(self) -> None:
        cards = [
            types.SimpleNamespace(tender_id="tender-1", external_id="external-1", message_text="card 1"),
            types.SimpleNamespace(tender_id="tender-2", external_id="external-2", message_text="card 2"),
        ]
        update = types.SimpleNamespace(message=types.SimpleNamespace())

        with (
            patch.object(telegram_bot, "send_card", new=AsyncMock()) as send_card,
            patch.object(telegram_bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            asyncio.run(
                telegram_bot.send_tender_cards(
                    update,
                    cards,
                    keyboard_factory=lambda tender_id: f"keyboard-{tender_id}",
                )
            )

        self.assertEqual(send_card.await_count, 2)
        self.assertEqual([call.args[1] for call in send_card.await_args_list], ["card 1", "card 2"])
        self.assertEqual(
            [call.kwargs["message_delay_seconds"] for call in send_card.await_args_list],
            [
                telegram_bot.TELEGRAM_BULK_CARD_DELAY_SECONDS,
                telegram_bot.TELEGRAM_BULK_CARD_DELAY_SECONDS,
            ],
        )
        sleep.assert_awaited_once_with(telegram_bot.TELEGRAM_BULK_CARD_DELAY_SECONDS)

    def test_digest_today_command_uses_operational_lead_queue(self) -> None:
        replies = []

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append(text)

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_operational_lead_queue_cards_with_stats",
                return_value=(
                    [],
                    types.SimpleNamespace(
                        total_found=5,
                        shown=0,
                        hidden_in_work=2,
                        hidden_negative_feedback=1,
                    ),
                ),
            ) as lead_builder,
            patch.object(telegram_bot, "build_digest_cards_today_with_stats") as old_today_builder,
            patch.object(telegram_bot, "send_all_targets_digest", new=AsyncMock()) as all_targets_handler,
            patch.object(telegram_bot, "build_recent_llm_digest_cards_with_stats") as recent_builder,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
        ):
            asyncio.run(telegram_bot.digest_today(update, types.SimpleNamespace()))

        lead_builder.assert_called_once_with(limit=100)
        old_today_builder.assert_not_called()
        all_targets_handler.assert_not_awaited()
        recent_builder.assert_not_called()
        generate_report.assert_not_called()
        prepare_tender.assert_not_called()
        self.assertIn("AI Tender Radar - новые клиентские лиды сегодня", replies[0])
        self.assertNotIn(FORBIDDEN_ROLE_WORD, replies[0].lower())
        self.assertIn("Новых лидов сегодня: 0", replies[0])
        self.assertIn('Скрыто как "в работе": 2', replies[0])
        self.assertIn("Скрыто негативным feedback: 1", replies[0])

    def test_digest_today_sends_operational_lead_cards(self) -> None:
        replies = []
        lead_card = types.SimpleNamespace(tender_id="95633694", message_text="lead card")

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append(text)

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_operational_lead_queue_cards_with_stats",
                return_value=(
                    [lead_card],
                    types.SimpleNamespace(
                        total_found=1,
                        shown=1,
                        hidden_in_work=0,
                        hidden_negative_feedback=0,
                    ),
                ),
            ),
            patch.object(telegram_bot, "send_tender_cards", new=AsyncMock()) as sender,
        ):
            asyncio.run(telegram_bot.digest_today(update, types.SimpleNamespace()))

        self.assertIn("Новых лидов сегодня: 1", replies[0])
        self.assertEqual(len(replies), 1)
        sender.assert_awaited_once_with(
            update,
            [lead_card],
            keyboard_factory=telegram_bot.lead_queue_keyboard,
        )

    def test_filter_audit_sends_cards_read_only_without_action_buttons(self) -> None:
        replies = []
        hidden_card = types.SimpleNamespace(
            tender_id="hidden",
            external_id="hidden",
            status="ОТЛОЖЕН",
            message_text="Отложены\nhidden card\nСсылка: https://example.test/hidden",
        )
        shown_card = types.SimpleNamespace(
            tender_id="shown",
            external_id="shown",
            status="ОТОБРАН",
            message_text="Отобраны как лиды\nshown card\nСсылка: https://example.test/shown",
        )

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_today_filter_audit_cards_with_stats",
                return_value=(
                    [hidden_card, shown_card],
                    types.SimpleNamespace(
                        total_found=2,
                        would_show=1,
                        manual_documents_needed=0,
                        hidden_no_go=1,
                        hidden_deadline=0,
                        hidden_other=0,
                        listed=2,
                        limit=50,
                        go_leads=1,
                        maybe_leads=1,
                        rejected_triage=0,
                        hard_noise_total=0,
                        hard_noise_diagnostics_available=False,
                        processed_in_work=0,
                        processed_not_relevant=0,
                        processed_total=0,
                    ),
                ),
            ) as audit_builder,
            patch.object(telegram_bot, "send_tender_cards", new=AsyncMock()) as sender,
            patch.object(telegram_bot, "build_recent_llm_digest_cards_with_stats") as recent_builder,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot.asyncio, "sleep", new=AsyncMock()),
        ):
            asyncio.run(telegram_bot.digest_today_filter_audit(update, types.SimpleNamespace()))

        audit_builder.assert_called_once_with(limit=50)
        sender.assert_not_awaited()
        recent_builder.assert_not_called()
        generate_report.assert_not_called()
        prepare_tender.assert_not_called()
        self.assertEqual(
            [reply[0] for reply in replies],
            [
                telegram_bot.today_filter_audit_summary_text(audit_builder.return_value[1]),
                hidden_card.message_text,
                shown_card.message_text,
            ],
        )
        self.assertIn("reply_markup", replies[0][1])
        self.assertIsNone(replies[1][1]["reply_markup"])
        self.assertIsNone(replies[2][1]["reply_markup"])
        full_text = "\n".join(reply[0] for reply in replies)
        self.assertNotIn("Проверка фильтра", full_text)
        self.assertNotIn("🔎 Разобрать", full_text)
        self.assertNotIn("✅ В работу", full_text)
        self.assertNotIn("🚫 Мимо", full_text)
        self.assertNotIn(FORBIDDEN_ROLE_WORD, full_text.lower())
        self.assertTrue(replies[0][1]["disable_web_page_preview"])
        self.assertTrue(replies[1][1]["disable_web_page_preview"])
        self.assertTrue(replies[2][1]["disable_web_page_preview"])

    def test_filter_feedback_callback_saves_statuses_and_answers(self) -> None:
        saved = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                raise AssertionError("filter feedback should not send extra chat messages")

        class Query:
            def __init__(self, status):
                self.message = Message()
                self.data = f"fb:tender-1:{status}"
                self.from_user = types.SimpleNamespace(
                    username="tester",
                    first_name="Test",
                    last_name="User",
                    id=321,
                )
                self.answers = []

            async def answer(self, text, **kwargs):
                self.answers.append((text, kwargs))

        cases = {
            "filter_hidden_correct": "Записал: фильтр сработал верно",
            "filter_hidden_wrong": "Записал: фильтр ошибся",
            "filter_docs_correct": "Записал: фильтр сработал верно",
            "filter_docs_wrong": "Записал: фильтр ошибся",
        }

        def fake_save_feedback(tender_id, status, user_name, **kwargs):
            saved.append((tender_id, status, user_name, kwargs))

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback", side_effect=fake_save_feedback),
        ):
            queries = []
            for status in cases:
                query = Query(status)
                queries.append(query)
                update = types.SimpleNamespace(callback_query=query)
                asyncio.run(telegram_bot.feedback_callback(update, types.SimpleNamespace()))

        self.assertEqual(
            saved,
            [
                (
                    "tender-1",
                    status,
                    "tester",
                    {"chat_id": 123, "user_id": 321},
                )
                for status in cases
            ],
        )
        self.assertEqual(
            [query.answers[0][0] for query in queries],
            list(cases.values()),
        )

    def test_regular_feedback_callback_still_saves_and_replies(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:interesting",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
        ):
            asyncio.run(telegram_bot.feedback_callback(update, types.SimpleNamespace()))

        save_feedback.assert_called_once_with(
            tender_id="tender-1",
            status="interesting",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        self.assertEqual(query.answers[0][0], "Сохранено: Интересно")
        self.assertEqual(replies, ["Обратная связь сохранена: Интересно"])

    def test_sales_in_work_callback_saves_new_status_and_replies(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:sales_in_work",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
        ):
            asyncio.run(telegram_bot.feedback_callback(update, types.SimpleNamespace()))

        save_feedback.assert_called_once_with(
            tender_id="tender-1",
            status="sales_in_work",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        expected = "Отмечено: в работу. Закупка скрыта из рабочих списков."
        self.assertEqual(query.answers[0][0], expected)
        self.assertEqual(replies, [expected])

    def test_sales_not_relevant_callback_saves_new_status_and_replies(self) -> None:
        replies = []
        edits = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

            async def edit_reply_markup(self, **kwargs):
                edits.append(kwargs)

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:sales_not_relevant",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)
        context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
            patch.object(telegram_bot, "reject_reason_keyboard", return_value="reason-keyboard") as reason_keyboard,
        ):
            asyncio.run(telegram_bot.feedback_callback(update, context))

        save_feedback.assert_called_once_with(
            tender_id="tender-1",
            status="sales_not_relevant",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        expected = "Отмечено: мимо. Закупка скрыта из рабочих списков. Можно уточнить причину ниже."
        self.assertEqual(query.answers[0][0], expected)
        self.assertEqual(replies, [])
        self.assertEqual(edits, [{"reply_markup": "reason-keyboard"}])
        context.bot.send_message.assert_not_awaited()
        reason_keyboard.assert_called_once_with("tender-1")

    def test_sales_not_relevant_callback_falls_back_to_chat_message_when_edit_fails(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

            async def edit_reply_markup(self, **kwargs):
                raise RuntimeError("edit failed")

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:sales_not_relevant",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)
        context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback"),
            patch.object(telegram_bot, "reject_reason_keyboard", return_value="reason-keyboard"),
        ):
            asyncio.run(telegram_bot.feedback_callback(update, context))

        expected = "Отмечено: мимо. Закупка скрыта из рабочих списков. Можно уточнить причину ниже."
        self.assertEqual(replies, [])
        context.bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text=expected,
            reply_markup="reason-keyboard",
        )

    def test_reject_reason_callback_saves_reason_and_replies(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:service_only",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)
        context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=AsyncMock()))

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
        ):
            asyncio.run(telegram_bot.feedback_callback(update, context))

        save_feedback.assert_called_once_with(
            tender_id="tender-1",
            status="service_only",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        self.assertEqual(query.answers[0][0], "Причина: Услуги/ПО без инфраструктуры")
        self.assertEqual(replies, [])
        context.bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="Причина сохранена: Услуги/ПО без инфраструктуры. Лид остается скрытым из рабочих списков.",
        )

    def test_sales_returned_callback_saves_return_status_and_replies(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:sales_returned",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
        ):
            asyncio.run(telegram_bot.feedback_callback(update, types.SimpleNamespace()))

        save_feedback.assert_called_once_with(
            tender_id="tender-1",
            status="sales_returned",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        expected = "Закупка возвращена в рабочие списки."
        self.assertEqual(query.answers[0][0], expected)
        self.assertEqual(replies, [expected])

    def test_sales_existing_client_callback_saves_status_and_replies(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        query = types.SimpleNamespace(
            message=Message(),
            data="fb:tender-1:sales_existing_client",
            from_user=types.SimpleNamespace(
                username="tester",
                first_name="Test",
                last_name="User",
                id=321,
            ),
            answers=[],
        )

        async def answer(text, **kwargs):
            query.answers.append((text, kwargs))

        query.answer = answer
        update = types.SimpleNamespace(callback_query=query)

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
        ):
            asyncio.run(telegram_bot.feedback_callback(update, types.SimpleNamespace()))

        save_feedback.assert_called_once_with(
            tender_id="tender-1",
            status="sales_existing_client",
            user_name="tester",
            chat_id=123,
            user_id=321,
        )
        expected = (
            "Отмечено: существующий клиент. Закупка скрыта из рабочих списков, "
            "а новые процедуры этого заказчика будут скрываться по точному имени клиента."
        )
        self.assertEqual(query.answers[0][0], expected)
        self.assertEqual(replies, [expected])

    def test_llm_callback_does_not_save_sales_status(self) -> None:
        update, replies, _ = self.make_llm_update()
        existing_report = {
            "external_id": "EXT-1",
            "model": "deepseek/deepseek-v4-pro",
            "analysis_type": "llm_presales_report_routerai_fallback",
            "report": {"summary": "ready"},
        }

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("EXT-1", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=existing_report),
            patch.object(telegram_bot, "send_llm_success_card_reply", new=AsyncMock()),
            patch.object(telegram_bot, "save_feedback") as save_feedback,
        ):
            asyncio.run(telegram_bot.llm_callback(update, types.SimpleNamespace()))

        save_feedback.assert_not_called()
        self.assertEqual(replies, [telegram_bot.TELEGRAM_LLM_ALREADY_READY_MESSAGE])

    def test_recent_command_uses_read_only_recent_digest_logic(self) -> None:
        replies = []

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append(text)

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_recent_llm_digest_cards_with_stats",
                return_value=([], 2, 1),
            ) as recent_builder,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
        ):
            asyncio.run(telegram_bot.recent(update, types.SimpleNamespace()))

        recent_builder.assert_called_once_with(24, limit=15, include_no_go=False)
        generate_report.assert_not_called()
        prepare_tender.assert_not_called()
        self.assertIn("свежих рабочих LLM-отчетов", replies[0])
        self.assertIn("Скрыто no_go: 2", replies[0])

    def test_in_work_command_sends_in_work_cards_with_return_keyboard(self) -> None:
        replies = []
        cards = [
            types.SimpleNamespace(
                tender_id="tender-1",
                external_id="external-1",
                message_text="in work card",
            )
        ]

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_operational_lead_in_work_cards_with_stats",
                return_value=(
                    cards,
                    types.SimpleNamespace(total_found=1, shown=1, hidden_expired=0),
                ),
            ) as in_work_builder,
            patch.object(telegram_bot, "send_tender_cards", new=AsyncMock()) as sender,
        ):
            asyncio.run(telegram_bot.in_work(update, types.SimpleNamespace()))

        in_work_builder.assert_called_once_with(limit=100)
        sender.assert_awaited_once_with(
            update,
            cards,
            keyboard_factory=telegram_bot.in_work_keyboard,
        )
        self.assertIn("AI Tender Radar - лиды в работе", replies[0][0])
        self.assertIn("Лидов в работе: 1", replies[0][0])

    def test_send_all_targets_digest_uses_operational_go_leads(self) -> None:
        replies = []
        cards = [
            types.SimpleNamespace(
                tender_id=f"tender-{index}",
                external_id=f"external-{index}",
                message_text=f"Статус: Новый\ncard {index}",
            )
            for index in range(2)
        ]

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_operational_lead_all_cards_with_stats",
                return_value=(
                    cards,
                    types.SimpleNamespace(
                        total_found=3,
                        shown=2,
                        new_count=2,
                        in_work_count=0,
                        hidden_in_work=1,
                        hidden_negative_feedback=1,
                    ),
                ),
            ) as lead_builder,
            patch.object(telegram_bot, "send_tender_cards", new=AsyncMock()) as sender,
            patch.object(telegram_bot, "build_digest_cards_with_stats") as digest_builder,
            patch.object(telegram_bot, "build_recent_llm_digest_cards_with_stats") as recent_builder,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
        ):
            asyncio.run(telegram_bot.send_all_targets_digest(update, limit=None))

        lead_builder.assert_called_once_with(limit=None)
        digest_builder.assert_not_called()
        sender.assert_awaited_once_with(
            update,
            cards,
            keyboard_factory=telegram_bot.all_leads_keyboard,
            keyboard_from_card=True,
        )
        recent_builder.assert_not_called()
        generate_report.assert_not_called()
        prepare_tender.assert_not_called()
        self.assertIn("AI Tender Radar - необработанные клиентские лиды", replies[0][0])
        self.assertIn("Необработанных лидов в очереди: 2", replies[0][0])
        self.assertIn('Скрыто как "в работе": 1', replies[0][0])
        self.assertIn("Исключено негативным feedback: 1", replies[0][0])
        self.assertIn("reply_markup", replies[0][1])

    def test_digest_command_uses_full_backlog_digest(self) -> None:
        replies = []

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append(text)

        update = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=123),
            message=Message(),
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(
                telegram_bot,
                "build_operational_lead_all_cards_with_stats",
                return_value=(
                    [],
                    types.SimpleNamespace(
                        total_found=0,
                        shown=0,
                        new_count=0,
                        in_work_count=0,
                        hidden_in_work=0,
                        hidden_negative_feedback=0,
                    ),
                ),
            ) as lead_builder,
            patch.object(telegram_bot, "build_digest_cards_with_stats") as digest_builder,
            patch.object(telegram_bot, "build_recent_llm_digest_cards_with_stats") as recent_builder,
        ):
            asyncio.run(telegram_bot.digest(update, types.SimpleNamespace()))

        lead_builder.assert_called_once_with(limit=None)
        digest_builder.assert_not_called()
        recent_builder.assert_not_called()
        self.assertIn("AI Tender Radar - необработанные клиентские лиды", replies[0])
        self.assertIn("Необработанных лидов в очереди: 0", replies[0])

    def test_compact_llm_report_supports_full_report_schema(self) -> None:
        text = telegram_bot.compact_llm_report_text(
            {
                "external_id": "old",
                "model": "gigachat",
                "report": {
                    "summary": "Full summary",
                    "recommendation": {
                        "decision": "go",
                        "confidence": "high",
                        "reasoning": ["good fit"],
                    },
                    "delivery_feasibility": {
                        "delivery_term": "30 days",
                        "assessment": "realistic",
                    },
                    "technical_spec_analysis": {
                        "what_is_required": ["server"],
                        "mentioned_models_or_vendors": ["VendorX"],
                        "equivalent_allowed": "unclear",
                    },
                    "tailoring_risk": {"level": "low", "reasons": ["generic spec"]},
                    "potential_offer": {"what_to_offer": ["server config"]},
                    "questions_to_customer": ["confirm warranty"],
                    "commercial_assessment": {"fit_for_integrator": "good"},
                    "next_action": ["prepare offer"],
                },
            }
        )

        self.assertIn("go /", text)
        self.assertIn("30 days / realistic", text)
        self.assertIn("server config", text)
        self.assertIn("confirm warranty", text)

    def test_compact_llm_report_supports_routerai_two_pass_schema(self) -> None:
        text = telegram_bot.compact_llm_report_text(
            {
                "external_id": "new",
                "model": "deepseek",
                "report": {
                    "summary": "RouterAI summary",
                    "verdict": "maybe",
                    "confidence": "medium",
                    "delivery_feasibility": "30 days, check stock",
                    "potential_offer": "storage candidate",
                    "questions_to_customer": "Can analogs be supplied?",
                    "next_action": "Ask distributor for price",
                },
            }
        )

        self.assertIn("maybe /", text)
        self.assertIn("30 days, check stock", text)
        self.assertIn("storage candidate", text)
        self.assertIn("Can analogs be supplied?", text)
        self.assertIn("Ask distributor for price", text)
        self.assertIn("Рекомендованные действия:", text)
        self.assertNotIn("Следующее действие:", text)

    def test_routerai_two_pass_formatter_uses_native_mapping_without_legacy_placeholders(self) -> None:
        text = telegram_bot.compact_llm_report_text(
            {
                "external_id": "router",
                "model": "deepseek",
                "meta": {"pass_mode": "routerai_two_pass"},
                "report": {
                    "summary": "RouterAI summary",
                    "verdict": "maybe",
                    "confidence": "medium",
                    "what_is_required": ["storage system"],
                    "top_risks": ["short delivery term"],
                    "delivery_feasibility": "possible only from stock",
                    "potential_offer": ["stock model"],
                    "questions_to_customer": ["confirm analogs"],
                    "next_action": ["request distributor quote"],
                },
            }
        )

        self.assertIn("Резюме:", text)
        self.assertIn("Критичные требования ТЗ:", text)
        self.assertIn("storage system", text)
        self.assertIn("Почему / риски:", text)
        self.assertIn("short delivery term", text)
        self.assertIn("Срок поставки / исполнимость:", text)
        self.assertIn("Что уточнить у заказчика:", text)
        self.assertNotIn("Что спросить заказчику", text)
        self.assertIn("Рекомендованные действия:", text)
        self.assertNotIn("не указано", text)
        self.assertNotIn("Риск заточки:", text)
        self.assertNotIn("Fit для интегратора:", text)

    def test_compact_llm_report_renders_delivery_list_as_bullets(self) -> None:
        text = telegram_bot.compact_llm_report_text(
            {
                "external_id": "delivery-list",
                "model": "deepseek",
                "report": {
                    "summary": "Check delivery",
                    "recommendation": {"decision": "maybe", "confidence": "medium"},
                    "delivery_feasibility": [
                        "Общий срок исполнения 90 дней",
                        "ПНР зависит от доступа на объект",
                    ],
                    "technical_spec_analysis": {"what_is_required": ["server"]},
                    "tailoring_risk": {"level": "low"},
                },
            }
        )

        self.assertIn("Срок поставки:", text)
        self.assertIn("— Общий срок исполнения 90 дней", text)
        self.assertIn("— ПНР зависит от доступа на объект", text)
        self.assertNotIn("['Общий срок", text)

    def test_next_action_sanitizes_past_absolute_control_date(self) -> None:
        text = telegram_bot.sanitize_next_action_text(
            "Контрольная точка – 02.05.2026",
            today=date(2026, 5, 3),
        )

        self.assertNotIn("02.05.2026", text)
        self.assertIn("Актуализировать", text)

    def test_next_action_sanitizes_past_date_and_keeps_action_text(self) -> None:
        text = telegram_bot.sanitize_next_action_text(
            "Запросить КП до 02.05.2026",
            today=date(2026, 5, 3),
        )

        self.assertEqual(text, "Запросить КП")

    def test_best_existing_report_prefers_routerai_over_newer_legacy_full(self) -> None:
        full = {
            "external_id": "old",
            "model": "gigachat",
            "analysis_type": "llm_presales_report",
            "_telegram_created_at": datetime(2026, 5, 3),
            "report": {
                "summary": "Full summary",
                "recommendation": {"decision": "go", "confidence": "high"},
                "technical_spec_analysis": {"what_is_required": ["server"]},
                "tailoring_risk": {"level": "low"},
            },
        }
        routerai = {
            "external_id": "new",
            "model": "deepseek/deepseek-v4-pro",
            "analysis_type": "llm_presales_report",
            "meta": {"pass_mode": "routerai_two_pass"},
            "_telegram_created_at": datetime(2026, 5, 1),
            "report": {
                "summary": "RouterAI summary",
                "verdict": "maybe",
                "confidence": "medium",
                "what_is_required": ["storage"],
                "top_risks": ["short delivery"],
            },
        }

        best, metadata = telegram_bot.select_best_existing_llm_report([routerai, full])

        self.assertEqual(best["model"], "deepseek/deepseek-v4-pro")
        self.assertEqual(metadata["selection_source"], "routerai_existing")
        self.assertEqual(metadata["selected_analysis_type"], "llm_presales_report")

    def test_best_existing_report_uses_legacy_when_routerai_absent(self) -> None:
        legacy = {
            "external_id": "old",
            "model": "gigachat",
            "analysis_type": "llm_presales_report",
            "_telegram_created_at": datetime(2026, 5, 2),
            "report": {
                "summary": "Full summary",
                "recommendation": {"decision": "go", "confidence": "high"},
                "technical_spec_analysis": {"what_is_required": ["server"]},
                "tailoring_risk": {"level": "low"},
            },
        }

        best, metadata = telegram_bot.select_best_existing_llm_report([legacy])

        self.assertEqual(best["model"], "gigachat")
        self.assertEqual(metadata["selection_source"], "legacy_existing")
        self.assertEqual(metadata["selected_model"], "gigachat")

    def test_best_existing_report_returns_generate_new_when_no_existing_match(self) -> None:
        failed_routerai = {
            "external_id": "new",
            "model": "deepseek/deepseek-v4-pro",
            "analysis_type": "llm_presales_report",
            "meta": {"parse_status": "failed", "pass_mode": "routerai_two_pass"},
            "_telegram_created_at": datetime(2026, 5, 3),
            "report": {
                "summary": "RouterAI summary",
                "verdict": "maybe",
                "what_is_required": ["storage"],
            },
        }

        best, metadata = telegram_bot.select_best_existing_llm_report([failed_routerai])

        self.assertIsNone(best)
        self.assertEqual(metadata["selection_source"], "generate_new")
        self.assertIsNone(metadata["selected_analysis_type"])

    def test_routerai_fallback_analysis_type_is_routerai_preferred(self) -> None:
        result = {
            "external_id": "new",
            "model": "deepseek/deepseek-v4-pro",
            "analysis_type": "llm_presales_report_routerai_fallback",
            "_telegram_created_at": datetime(2026, 5, 3),
            "report": {"summary": "RouterAI fallback summary"},
        }

        self.assertTrue(telegram_bot.is_routerai_preferred_report(result))

    def test_llm_success_reply_sends_full_tender_card(self) -> None:
        replies = []
        card = types.SimpleNamespace(
            tender_id="tender-1",
            external_id="EXT-1",
            message_text=(
                "🤖 Разбор готов\n\n"
                "Номер: EXT-1\n"
                "Заказчик: Customer\n"
                "Сроки:\nПодача до: 10.05.2026\n"
                "Финансы:\nНМЦК: 1 000 000 ₽\n"
                "Контакт заказчика:\nТелефон: +7\n"
                "Документы:\n— ТЗ.pdf\n"
                "LLM-пресейл-анализ:\nРезюме: готово\n"
                "Ссылка: https://example.test/tender"
            ),
        )

        class Message:
            async def reply_text(self, text, **kwargs):
                replies.append((text, kwargs))

        with (
            patch.object(telegram_bot, "build_tender_digest_card", return_value=card) as builder,
            patch.object(telegram_bot, "send_long_reply", new=AsyncMock()) as send_long_reply,
        ):
            asyncio.run(
                telegram_bot.send_llm_success_card_reply(
                    Message(),
                    "tender-1",
                    {"report": {"summary": "ready"}},
                )
            )

        builder.assert_called_once_with(
            "tender-1",
            prefix=telegram_bot.TELEGRAM_LLM_SUCCESS_PREFIX,
            llm_result_override={"report": {"summary": "ready"}},
        )
        send_long_reply.assert_not_awaited()
        self.assertEqual(len(replies), 1)
        self.assertIn("🤖 Разбор готов", replies[0][0])
        self.assertIn("Заказчик: Customer", replies[0][0])
        self.assertIn("Ссылка: https://example.test/tender", replies[0][0])
        self.assertIn("reply_markup", replies[0][1])

    def test_active_telegram_llm_analysis_detects_running_started_event(self) -> None:
        now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
        running = telegram_bot.active_telegram_llm_analysis_from_events(
            [
                {
                    "tender_id": "tender-1",
                    "external_id": "EXT-1",
                    "event_type": telegram_bot.TELEGRAM_LLM_ANALYSIS_STARTED,
                    "message": "{}",
                    "created_at": now - timedelta(minutes=2),
                }
            ],
            now=now,
        )

        self.assertIsNotNone(running)
        self.assertEqual(running.tender_id, "tender-1")
        self.assertEqual(running.external_id, "EXT-1")

    def test_active_telegram_llm_analysis_ignores_terminal_and_stale_events(self) -> None:
        now = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
        terminal_events = [
            {
                "tender_id": "tender-1",
                "external_id": "EXT-1",
                "event_type": telegram_bot.TELEGRAM_LLM_ANALYSIS_STARTED,
                "message": "{}",
                "created_at": now - timedelta(minutes=4),
            },
            {
                "tender_id": "tender-1",
                "external_id": "EXT-1",
                "event_type": telegram_bot.TELEGRAM_LLM_ANALYSIS_SUCCEEDED,
                "message": "{}",
                "created_at": now - timedelta(minutes=3),
            },
        ]
        stale_events = [
            {
                "tender_id": "tender-2",
                "external_id": "EXT-2",
                "event_type": telegram_bot.TELEGRAM_LLM_ANALYSIS_STARTED,
                "message": "{}",
                "created_at": now - timedelta(minutes=31),
            }
        ]

        self.assertIsNone(
            telegram_bot.active_telegram_llm_analysis_from_events(terminal_events, now=now)
        )
        self.assertIsNone(
            telegram_bot.active_telegram_llm_analysis_from_events(stale_events, now=now)
        )

    def test_telegram_llm_analysis_event_type_maps_background_statuses(self) -> None:
        self.assertEqual(
            telegram_bot.telegram_llm_analysis_event_type("succeeded"),
            telegram_bot.TELEGRAM_LLM_ANALYSIS_SUCCEEDED,
        )
        self.assertEqual(
            telegram_bot.telegram_llm_analysis_event_type("failed"),
            telegram_bot.TELEGRAM_LLM_ANALYSIS_FAILED,
        )

    def test_llm_callback_reuses_existing_report_without_starting_llm(self) -> None:
        update, replies, _ = self.make_llm_update()
        existing_report = {
            "external_id": "EXT-1",
            "model": "deepseek/deepseek-v4-pro",
            "analysis_type": "llm_presales_report_routerai_fallback",
            "report": {"summary": "ready"},
        }

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("EXT-1", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=existing_report),
            patch.object(telegram_bot, "send_llm_success_card_reply", new=AsyncMock()) as send_card_reply,
            patch.object(telegram_bot, "begin_telegram_llm_analysis_run") as begin_run,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
        ):
            asyncio.run(telegram_bot.llm_callback(update, types.SimpleNamespace()))

        self.assertEqual(replies, [telegram_bot.TELEGRAM_LLM_ALREADY_READY_MESSAGE])
        send_card_reply.assert_awaited_once()
        begin_run.assert_not_called()
        prepare_tender.assert_not_called()
        generate_report.assert_not_called()

    def test_llm_callback_generation_disabled_does_not_start_paid_analysis(self) -> None:
        update, replies, _ = self.make_llm_update()

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("EXT-1", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=None),
            patch.object(
                telegram_bot.settings,
                "telegram_llm_generation_enabled",
                False,
                create=True,
            ),
            patch.object(telegram_bot, "llm_document_not_ready_message") as readiness,
            patch.object(telegram_bot, "begin_telegram_llm_analysis_run") as begin_run,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
        ):
            asyncio.run(telegram_bot.llm_callback(update, types.SimpleNamespace()))

        self.assertEqual(
            replies,
            [telegram_bot.TELEGRAM_LLM_GENERATION_DISABLED_MESSAGE],
        )
        readiness.assert_not_called()
        begin_run.assert_not_called()
        prepare_tender.assert_not_called()
        generate_report.assert_not_called()

    def test_llm_callback_same_tender_running_does_not_start_llm_again(self) -> None:
        update, replies, _ = self.make_llm_update()
        running = telegram_bot.TelegramLLMRunningAnalysis(
            tender_id="tender-1",
            external_id="EXT-1",
            started_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
            payload={},
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("EXT-1", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=None),
            patch.object(
                telegram_bot,
                "begin_telegram_llm_analysis_run",
                return_value=telegram_bot.TelegramLLMAnalysisStartResult(
                    status=telegram_bot.TELEGRAM_LLM_START_SAME_RUNNING,
                    running=running,
                ),
            ),
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
        ):
            asyncio.run(telegram_bot.llm_callback(update, types.SimpleNamespace()))

        self.assertEqual(replies, [telegram_bot.TELEGRAM_LLM_SAME_RUNNING_MESSAGE])
        prepare_tender.assert_not_called()
        generate_report.assert_not_called()

    def test_llm_callback_other_tender_running_gets_busy_message(self) -> None:
        update, replies, _ = self.make_llm_update()
        running = telegram_bot.TelegramLLMRunningAnalysis(
            tender_id="tender-2",
            external_id="EXT-2",
            started_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
            payload={},
        )

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("EXT-1", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=None),
            patch.object(
                telegram_bot,
                "begin_telegram_llm_analysis_run",
                return_value=telegram_bot.TelegramLLMAnalysisStartResult(
                    status=telegram_bot.TELEGRAM_LLM_START_BUSY_OTHER,
                    running=running,
                ),
            ),
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
        ):
            asyncio.run(telegram_bot.llm_callback(update, types.SimpleNamespace()))

        self.assertEqual(replies, [telegram_bot.TELEGRAM_LLM_BUSY_OTHER_MESSAGE])
        prepare_tender.assert_not_called()
        generate_report.assert_not_called()

    def test_llm_callback_manual_document_card_does_not_start_llm(self) -> None:
        update, replies, _ = self.make_llm_update()
        not_ready_message = telegram_bot.TELEGRAM_DOCUMENT_NOT_READY_MESSAGE

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("RST32615987873", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=None),
            patch.object(
                telegram_bot,
                "get_tender_digest_row",
                return_value={
                    "tender_id": "tender-1",
                    "external_id": "RST32615987873",
                    "docs_count": 0,
                    "docs_with_text": 0,
                    "preparation_blocked_event": '{"status": "blocked_by_marketplace_auth"}',
                },
            ),
            patch.object(
                telegram_bot,
                "manual_document_reason_for_tender",
                return_value="preparation_blocked=marketplace_auth",
            ),
            patch.object(telegram_bot, "begin_telegram_llm_analysis_run") as begin_run,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
        ):
            asyncio.run(telegram_bot.llm_callback(update, types.SimpleNamespace()))

        self.assertEqual(replies, [not_ready_message])
        begin_run.assert_not_called()
        prepare_tender.assert_not_called()
        generate_report.assert_not_called()

    def test_llm_callback_accepts_and_creates_background_task_without_inline_llm(self) -> None:
        update, replies, _ = self.make_llm_update()
        llm_lock = object()

        class Application:
            def __init__(self):
                self.created_tasks = []

            def create_task(self, coro):
                self.created_tasks.append(coro)
                return types.SimpleNamespace(add_done_callback=lambda callback: None)

        application = Application()
        context = types.SimpleNamespace(application=application)

        with (
            patch.object(telegram_bot, "is_allowed", return_value=True),
            patch.object(telegram_bot, "get_tender_external_id", return_value=("EXT-1", "Tender")),
            patch.object(telegram_bot, "get_best_existing_llm_report", return_value=None),
            patch.object(
                telegram_bot,
                "begin_telegram_llm_analysis_run",
                return_value=telegram_bot.TelegramLLMAnalysisStartResult(
                    status=telegram_bot.TELEGRAM_LLM_START_STARTED,
                    lock=llm_lock,
                ),
            ) as begin_run,
            patch.object(telegram_bot, "prepare_tender_for_analysis") as prepare_tender,
            patch.object(telegram_bot, "generate_presales_report") as generate_report,
        ):
            try:
                asyncio.run(telegram_bot.llm_callback(update, context))
            finally:
                for coro in application.created_tasks:
                    coro.close()

        self.assertEqual(replies, ["Принял в работу: EXT-1. Разбор запущен."])
        self.assertEqual(len(application.created_tasks), 1)
        self.assertEqual(begin_run.call_args.kwargs["requested_by"]["user_id"], 321)
        self.assertEqual(begin_run.call_args.kwargs["requested_by"]["user_name"], "tester")
        self.assertEqual(begin_run.call_args.kwargs["requested_by"]["chat_id"], 123)
        prepare_tender.assert_not_called()
        generate_report.assert_not_called()

    def test_background_llm_success_marks_succeeded_and_sends_report(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        class Lock:
            released = False

            def release(self):
                self.released = True

        lock = Lock()
        preparation = types.SimpleNamespace(
            status=types.SimpleNamespace(value="ready"),
            ready_for_llm=True,
            document_counts={"downloaded": 1},
        )
        result = {
            "external_id": "EXT-1",
            "model": "model",
            "report": {"summary": "done"},
        }
        telegram_bot.set_current_telegram_llm_analysis(
            telegram_bot.TelegramLLMRunningAnalysis(
                tender_id="tender-1",
                external_id="EXT-1",
                started_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
                payload={},
            )
        )

        try:
            with (
                patch.object(telegram_bot, "prepare_tender_for_analysis", return_value=preparation),
                patch.object(telegram_bot, "preparation_block_message", return_value=None),
                patch.object(telegram_bot, "generate_presales_report", return_value=result) as generate_report,
                patch.object(telegram_bot, "record_telegram_llm_analysis_event") as record_event,
                patch.object(telegram_bot, "send_llm_success_card_reply", new=AsyncMock()) as send_card_reply,
            ):
                asyncio.run(
                    telegram_bot.run_telegram_llm_analysis_background(
                        message=Message(),
                        lock=lock,
                        tender_id="tender-1",
                        external_id="EXT-1",
                        requested_by={"user_id": 321},
                    )
                )
        finally:
            telegram_bot.clear_current_telegram_llm_analysis()

        self.assertIn("Документы готовы. Запускаю анализ, это может занять 1-2 минуты.", replies)
        self.assertEqual(generate_report.call_args.kwargs["analysis_depth"], "deep")
        self.assertEqual(generate_report.call_args.kwargs["max_spec_chars"], 240000)
        self.assertEqual(generate_report.call_args.kwargs["max_other_chars"], 40000)
        self.assertEqual(generate_report.call_args.kwargs["max_output_tokens"], 16384)
        self.assertEqual(record_event.call_args.kwargs["status"], "succeeded")
        self.assertIsNone(record_event.call_args.kwargs["error_message"])
        send_card_reply.assert_awaited_once()
        self.assertIsNone(telegram_bot.get_local_current_telegram_llm_analysis())
        self.assertTrue(lock.released)

    def test_background_llm_failed_run_marks_failed_and_clears_state(self) -> None:
        replies = []

        class Message:
            chat_id = 123

            async def reply_text(self, text, **kwargs):
                replies.append(text)

        class Lock:
            released = False

            def release(self):
                self.released = True

        lock = Lock()
        preparation = types.SimpleNamespace(
            status=types.SimpleNamespace(value="ready"),
            ready_for_llm=True,
            document_counts={"downloaded": 1},
        )
        telegram_bot.set_current_telegram_llm_analysis(
            telegram_bot.TelegramLLMRunningAnalysis(
                tender_id="tender-1",
                external_id="EXT-1",
                started_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
                payload={},
            )
        )

        try:
            with (
                patch.object(telegram_bot, "prepare_tender_for_analysis", return_value=preparation),
                patch.object(telegram_bot, "preparation_block_message", return_value=None),
                patch.object(telegram_bot, "generate_presales_report", side_effect=RuntimeError("boom")),
                patch.object(telegram_bot, "record_telegram_llm_analysis_event") as record_event,
                patch.object(telegram_bot, "send_long_reply", new=AsyncMock()) as send_long_reply,
            ):
                asyncio.run(
                    telegram_bot.run_telegram_llm_analysis_background(
                        message=Message(),
                        lock=lock,
                        tender_id="tender-1",
                        external_id="EXT-1",
                        requested_by={"user_id": 321},
                    )
                )
        finally:
            telegram_bot.clear_current_telegram_llm_analysis()

        self.assertIn("Ошибка разбора: boom", replies)
        self.assertEqual(record_event.call_args.kwargs["status"], "failed")
        self.assertEqual(record_event.call_args.kwargs["error_message"], "boom")
        send_long_reply.assert_not_awaited()
        self.assertIsNone(telegram_bot.get_local_current_telegram_llm_analysis())
        self.assertTrue(lock.released)

    def test_finish_telegram_llm_analysis_clears_state_and_records_failed_event(self) -> None:
        class Lock:
            released = False

            def release(self):
                self.released = True

        lock = Lock()
        telegram_bot.set_current_telegram_llm_analysis(
            telegram_bot.TelegramLLMRunningAnalysis(
                tender_id="tender-1",
                external_id="EXT-1",
                started_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
                payload={},
            )
        )

        try:
            with patch.object(telegram_bot, "record_telegram_llm_analysis_event") as record_event:
                telegram_bot.finish_telegram_llm_analysis_run(
                    lock=lock,
                    tender_id="tender-1",
                    external_id="EXT-1",
                    requested_by={"user_id": 321},
                    status="failed",
                    error_message="boom",
                )
        finally:
            telegram_bot.clear_current_telegram_llm_analysis()

        record_event.assert_called_once_with(
            tender_id="tender-1",
            external_id="EXT-1",
            requested_by={"user_id": 321},
            status="failed",
            error_message="boom",
        )
        self.assertIsNone(telegram_bot.get_local_current_telegram_llm_analysis())
        self.assertTrue(lock.released)

    def test_finish_telegram_llm_analysis_records_succeeded_event(self) -> None:
        class Lock:
            released = False

            def release(self):
                self.released = True

        lock = Lock()

        with patch.object(telegram_bot, "record_telegram_llm_analysis_event") as record_event:
            telegram_bot.finish_telegram_llm_analysis_run(
                lock=lock,
                tender_id="tender-1",
                external_id="EXT-1",
                requested_by={"user_id": 321},
                status="succeeded",
            )

        record_event.assert_called_once_with(
            tender_id="tender-1",
            external_id="EXT-1",
            requested_by={"user_id": 321},
            status="succeeded",
            error_message=None,
        )
        self.assertTrue(lock.released)


if __name__ == "__main__":
    unittest.main()
