"""Synthetic Telegram workflow with a caller-owned adapter; never creates a second poller."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class RadarMenuCopyTest(unittest.TestCase):
    def test_menu_has_two_export_buttons_and_keeps_freeform_input(self):
        from agent_radar.telegram_menu import keyboard, WELCOME, COMMANDS
        markup = keyboard()
        self.assertEqual([[button.text for button in row] for row in markup.keyboard], [
            ["Выгрузить новые лиды", "Выгрузить в работе"],
        ])
        self.assertTrue(markup.resize_keyboard)
        self.assertTrue(markup.is_persistent)
        self.assertFalse(markup.one_time_keyboard)
        self.assertIn("напишите вопрос", WELCOME)
        self.assertNotIn("MCP", WELCOME)
        self.assertEqual([name for name, _ in COMMANDS],
                         ["leads", "work", "guide"])
        self.assertTrue(all(len(description) <= 40 for _, description in COMMANDS))


class MenuHandlerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.allowlist = patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": "123"})
        self.allowlist.start()
        self.addCleanup(self.allowlist.stop)

    async def test_control_command_dispatches_to_the_same_adapter_and_denies_unauthorized(self):
        from agent_radar.telegram_menu import MenuController
        events = []
        class Adapter:
            def is_authorized(self, message):
                return message.from_user.id == 123
            def should_process(self, message, **kwargs):
                return True
            async def build_control_event(self, message, update):
                return SimpleNamespace(text=message.text, source=SimpleNamespace(user_id=str(message.from_user.id)), type="command")
            async def handle_message(self, event):
                events.append(event)
        controller = MenuController(Adapter())
        message = SimpleNamespace(text="/dialog", from_user=SimpleNamespace(id=123),
                                  chat=SimpleNamespace(type="private", id=123), reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, message=message, update_id=1)
        await controller.handle(update, None)
        self.assertEqual(events[0].text, "/new")
        self.assertEqual(events[0].source.user_id, "123")
        self.assertEqual(events[0].type, "command")
        message.from_user.id = 999
        await controller.handle(update, None)
        self.assertEqual(len(events), 1)
        message.reply_text.assert_not_awaited()

    async def test_menu_and_freshness_are_local_and_dialog_dispatches_to_adapter(self):
        from agent_radar.telegram_menu import MenuController, WELCOME, HELP
        events = []
        class Adapter:
            def is_authorized(self, message): return True
            def should_process(self, message, **kwargs): return True
            async def build_control_event(self, message, update):
                return SimpleNamespace(text=message.text, type="command")
            async def handle_message(self, event): events.append(event)
        controller = MenuController(Adapter(), "/nonexistent-source")
        message = SimpleNamespace(text="/start", from_user=SimpleNamespace(id=123), chat=SimpleNamespace(type="private"), reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        for text, expected in (("/start", WELCOME), ("/guide", HELP)):
            message.text = text
            await controller.handle(update, None)
            self.assertEqual(message.reply_text.await_args.args[0], expected)
        message.text = "/data"
        await controller.handle(update, None)
        self.assertIn("Не удалось проверить", message.reply_text.await_args.args[0])
        self.assertFalse(events)
        message.text = "/dialog"
        await controller.handle(update, None)
        self.assertEqual(events[0].text, "/new")
        self.assertEqual(events[0].type, "command")
        message.text = "/halt"
        await controller.handle(update, None)
        self.assertEqual(events[1].text, "/stop")
        self.assertEqual(events[1].type, "command")
        message.text = "Обычный вопрос по закупке"
        await controller.handle(update, None)
        self.assertEqual(len(events), 2)

    def test_freshness_uses_only_the_injected_reader_and_marks_stale_data(self):
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from unittest.mock import Mock
        from agent_radar.telegram_menu import MenuController
        now = datetime.now(timezone.utc)
        reader = Mock(return_value={"source_collected_at": (now - timedelta(minutes=60)).isoformat(),
                                    "source_window_from": (now - timedelta(days=14)).isoformat()})
        controller = MenuController(SimpleNamespace(), "synthetic-bundles", freshness_reader=reader)
        text = controller.freshness()
        self.assertIn("Данные старше 45 минут", text)
        reader.assert_called_once_with(Path("synthetic-bundles"))
        reader.return_value = {}
        self.assertIn("пока не подтверждено", controller.freshness())
        reader.side_effect = ValueError("invalid synthetic bundle")
        self.assertIn("Не удалось проверить", controller.freshness())

    async def test_exports_send_work_and_new_leads_as_xlsx_only_to_the_requesting_manager(self):
        import io
        import tempfile
        import zipfile
        from pathlib import Path
        from agent_radar.telegram_menu import MenuController, EXPORT_EMPTY
        from agent_radar.lead_agent.store import LeadStore
        adapter = SimpleNamespace(is_authorized=lambda message: True,
                                  should_process=lambda message, **kwargs: True)
        result = {"verdict": "lead", "grade": "A", "customer": {"name": "ООО Учебный заказчик"},
                  "signal": {"what_they_buy": "2 сервера", "deadline": "05.10.2026"},
                  "contacts": [{"name": "Иванов Иван", "role": "закупки", "phone": "+7 (000) 000-00-00", "email": None}],
                  "talk_track": {"hooks": ["первая зацепка"]}}
        message = SimpleNamespace(text="Выгрузить в работе", from_user=SimpleNamespace(id=123),
                                  chat=SimpleNamespace(type="private", id=123), reply_text=AsyncMock(), reply_document=AsyncMock())
        update = SimpleNamespace(effective_message=message)

        def sheet_of(call):
            archive = zipfile.ZipFile(io.BytesIO(call.kwargs["document"].getvalue()))
            return archive.read("xl/worksheets/sheet1.xml").decode("utf-8"), archive.read("xl/workbook.xml").decode("utf-8")
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"RADAR_DIGEST_STATE_ROOT": directory}):
            store = LeadStore(Path(directory) / "leads.sqlite3")
            lead = store.save(tender_id="t", fingerprint="f", model="m", cost_usd=0, tokens_in=0, tokens_out=0, tool_calls=0,
                              result=result, card_html='<a href="https://tenders.example.org/p">x</a>', deliverable=True)
            controller = MenuController(adapter)
            # nothing delivered yet: both exports say so
            await controller.handle(update, None)
            self.assertEqual(message.reply_text.await_args.args[0], EXPORT_EMPTY["work"])
            message.text = "Выгрузить новые лиды"
            await controller.handle(update, None)
            self.assertEqual(message.reply_text.await_args.args[0], EXPORT_EMPTY["new"])
            message.reply_document.assert_not_awaited()
            # delivered but unanswered -> "new" export
            store.claim(lead, 123)
            store.record_sent(lead, 123, 55)
            await controller.handle(update, None)
            sent = message.reply_document.await_args
            self.assertEqual((sent.kwargs["caption"], sent.kwargs["filename"].startswith("leady-novye-")), ("Новых лидов: 1", True))
            sheet, workbook = sheet_of(sent)
            self.assertIn("Иванов Иван", sheet)
            self.assertIn("Новые лиды", workbook)
            # pressing "В работу" moves it from "new" to "work"
            store.add_feedback(lead, 123, 123, "work")
            message.reply_document.reset_mock()
            await controller.handle(update, None)
            message.reply_document.assert_not_awaited()
            message.text = "Выгрузить в работе"
            await controller.handle(update, None)
            sent = message.reply_document.await_args
            self.assertEqual((sent.kwargs["caption"], sent.kwargs["filename"].startswith("leady-v-rabote-")), ("Лидов в работе: 1", True))
            sheet, workbook = sheet_of(sent)
            self.assertIn("+7 (000) 000-00-00", sheet)
            self.assertIn("Лиды в работе", workbook)
            archive = zipfile.ZipFile(io.BytesIO(sent.kwargs["document"].getvalue()))
            self.assertIn("https://tenders.example.org/p", archive.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8"))
            # "Мимо" removes it from both exports
            store.add_feedback(lead, 123, 123, "skip")
            message.reply_document.reset_mock()
            for text in ("Выгрузить в работе", "Выгрузить новые лиды"):
                message.text = text
                await controller.handle(update, None)
            message.reply_document.assert_not_awaited()
            message.from_user.id = 999  # not on the native allowlist
            message.reply_text.reset_mock()
            await controller.handle(update, None)
            message.reply_text.assert_not_awaited()

    async def test_local_menu_fails_closed_when_native_allowlist_is_missing_or_malformed(self):
        from agent_radar.telegram_menu import MenuController
        adapter = SimpleNamespace(is_authorized=lambda message: True,
                                  should_process=lambda message, **kwargs: True)
        controller = MenuController(adapter)
        message = SimpleNamespace(text="/menu", from_user=SimpleNamespace(id=123),
                                  chat=SimpleNamespace(type="private"), reply_text=AsyncMock())
        for configured in ("", "*", "123,invalid", "999"):
            with patch.dict("os.environ", {"TELEGRAM_ALLOWED_USERS": configured}):
                await controller.handle(SimpleNamespace(effective_message=message), None)
        message.reply_text.assert_not_awaited()

    async def test_subscriptions_observe_authorized_freeform_without_reenabling_optout(self):
        import tempfile
        from pathlib import Path
        from agent_radar.telegram_menu import MenuController
        from agent_radar.subscriptions import Subscriptions
        with tempfile.TemporaryDirectory() as directory:
            store = Subscriptions(Path(directory)/"digests.sqlite3")
            controller = MenuController(SimpleNamespace(), subscriptions=store)
            message=SimpleNamespace(text="Обычный вопрос", from_user=SimpleNamespace(id=123,is_bot=False),
                                    chat=SimpleNamespace(id=123,type="private"))
            await controller.observe(SimpleNamespace(effective_message=message), None)
            self.assertEqual(store.recipients({123}),[123])
            store.unsubscribe(123)
            await controller.observe(SimpleNamespace(effective_message=message), None)
            self.assertEqual(store.recipients({123}),[])

    async def test_lead_feedback_button_reaches_journal_through_the_real_application(self):
        import tempfile
        from pathlib import Path
        from agent_radar.telegram_menu import wire_telegram_menu
        from agent_radar.lead_agent.store import LeadStore
        from telegram import Update
        from telegram.ext import Application, MessageHandler, filters, ExtBot
        answers, edits = [], []

        class Bot(ExtBot):
            async def initialize(self): self._bot_user = SimpleNamespace(id=789, username="synthetic_bot", is_bot=True, first_name="Synthetic")
            async def shutdown(self): pass
            async def answer_callback_query(self, callback_query_id, text=None, **kwargs): answers.append(text)
            async def edit_message_reply_markup(self, *args, **kwargs): edits.append(kwargs.get("reply_markup"))
        adapter = SimpleNamespace(is_authorized=lambda message: True)
        events = []

        async def freeform(update, context): events.append(update.effective_message.text)

        def callback(data, index, user_id=123):
            return {"update_id": index, "callback_query": {"id": str(index), "chat_instance": "x", "data": data,
                    "from": {"id": user_id, "first_name": "Synthetic", "is_bot": False},
                    "message": {"message_id": 5, "date": 0, "chat": {"id": user_id, "type": "private"}}}}
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {
                "RADAR_DIGEST_STATE_ROOT": temporary, "RADAR_LEAD_FEEDBACK": "1", "TELEGRAM_ALLOWED_USERS": "123"}):
            app = Application.builder().bot(Bot("123:synthetic")).updater(None).build()
            wire_telegram_menu(app, adapter)
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, freeform))
            store = LeadStore(Path(temporary) / "leads.sqlite3")
            lead = store.save(tender_id="t", fingerprint="f", model="m", cost_usd=0, tokens_in=0, tokens_out=0, tool_calls=0,
                              result={"verdict": "lead", "grade": "A"}, card_html="<b>c</b>", deliverable=True)
            store.claim(lead, 123)
            async with app:
                await app.process_update(Update.de_json(callback(f"lf:{lead}:work", 1), app.bot))
                self.assertEqual(store.feedback(lead), ["work"])
                self.assertEqual(answers, ["Записано"])
                self.assertEqual(len(edits), 1)
                await app.process_update(Update.de_json(callback(f"lf:{lead}:skip", 2, user_id=999), app.bot))  # not allowed
                await app.process_update(Update.de_json(callback("other:1", 3), app.bot))
                self.assertEqual(store.feedback(lead), ["work"])
            self.assertEqual(events, [])

    async def test_reply_to_a_lead_card_is_saved_as_a_note_and_other_replies_stay_native(self):
        import tempfile
        from pathlib import Path
        from agent_radar.telegram_menu import wire_telegram_menu
        from agent_radar.lead_agent.store import LeadStore
        from telegram import Update
        from telegram.ext import Application, MessageHandler, filters, ExtBot
        sent = []

        class Bot(ExtBot):
            async def initialize(self): self._bot_user = SimpleNamespace(id=789, username="synthetic_bot", is_bot=True, first_name="Synthetic")
            async def shutdown(self): pass
            async def send_message(self, *args, **kwargs):
                sent.append(kwargs.get("text") or args[1])
                return SimpleNamespace(message_id=900)
        adapter = SimpleNamespace(is_authorized=lambda message: True)
        events = []

        async def freeform(update, context): events.append(update.effective_message.text)

        def reply(text, reply_to, index, user_id=123):
            return {"update_id": index, "message": {"message_id": 100 + index, "date": 0, "text": text,
                    "chat": {"id": user_id, "type": "private"}, "from": {"id": user_id, "first_name": "S", "is_bot": False},
                    "reply_to_message": {"message_id": reply_to, "date": 0, "chat": {"id": user_id, "type": "private"},
                                         "from": {"id": 789, "first_name": "Bot", "is_bot": True}, "text": "card"}}}
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {
                "RADAR_DIGEST_STATE_ROOT": temporary, "RADAR_LEAD_FEEDBACK": "1", "TELEGRAM_ALLOWED_USERS": "123"}):
            app = Application.builder().bot(Bot("123:synthetic")).updater(None).build()
            wire_telegram_menu(app, adapter)
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, freeform))
            store = LeadStore(Path(temporary) / "leads.sqlite3")
            lead = store.save(tender_id="t", fingerprint="f", model="m", cost_usd=0, tokens_in=0, tokens_out=0, tool_calls=0,
                              result={"verdict": "lead", "grade": "A"}, card_html="<b>c</b>", deliverable=True)
            store.claim(lead, 123)
            store.record_sent(lead, 123, 55)
            async with app:
                await app.process_update(Update.de_json(reply("позвонил, перезвонить во вторник", 55, 1), app.bot))
                self.assertEqual([c["text"] for c in store.new_leads(123, 123)[0]["comments"]], ["позвонил, перезвонить во вторник"])
                self.assertTrue(any("Заметка записана" in text for text in sent))
                self.assertEqual(events, [])  # the note is not forwarded to the model
                await app.process_update(Update.de_json(reply("обычный ответ на другое сообщение", 56, 2), app.bot))
                self.assertEqual(events, ["обычный ответ на другое сообщение"])  # not a lead card: native flow
                self.assertEqual(len(store.new_leads(123, 123)[0]["comments"]), 1)

    async def test_subscription_commands_and_observer_groups_preserve_native_freeform(self):
        import tempfile
        from pathlib import Path
        from agent_radar.telegram_menu import wire_telegram_menu
        from agent_radar.subscriptions import Subscriptions
        from telegram import Update
        from telegram.ext import Application,MessageHandler,filters,ExtBot
        class Bot(ExtBot):
            async def initialize(self):self._bot_user=SimpleNamespace(id=789,username="synthetic_bot",is_bot=True,first_name="Synthetic")
            async def shutdown(self):pass
            async def send_message(self,*args,**kwargs):return SimpleNamespace(message_id=1)
        adapter=SimpleNamespace(is_authorized=lambda message:True)
        events=[]
        async def freeform(update,context):events.append(update.effective_message.text)
        def update(text,index):
            entity=[{"type":"bot_command","offset":0,"length":len(text)}] if text.startswith("/") else []
            return Update.de_json({"update_id":index,"message":{"message_id":index,"date":0,"chat":{"id":123,"type":"private"},
                "from":{"id":123,"first_name":"Synthetic","is_bot":False},"text":text,"entities":entity}},app.bot)
        with tempfile.TemporaryDirectory() as temporary,patch.dict("os.environ",{"RADAR_DIGEST_STATE_ROOT":temporary}):
            app=Application.builder().bot(Bot("123:synthetic")).updater(None).build()
            wire_telegram_menu(app,adapter)
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,freeform))
            async with app:
                await app.process_update(update("Обычный вопрос",1))
                self.assertEqual(events,["Обычный вопрос"])
                store=Subscriptions(Path(temporary)/"digests.sqlite3")
                self.assertEqual(store.recipients({123}),[123])
                await app.process_update(update("/digest_off",2))
                self.assertEqual(store.recipients({123}),[])
                await app.process_update(update("Второй вопрос",3))
                self.assertEqual(store.recipients({123}),[])
                await app.process_update(update("/digest_on",4))
                self.assertEqual(store.recipients({123}),[123])
                self.assertEqual(events,["Обычный вопрос","Второй вопрос"])

    async def test_real_ptb_filters_leave_freeform_and_native_control_commands_alone(self):
        from agent_radar.telegram_menu import wire_telegram_menu
        from telegram import Update
        handlers = []
        app = SimpleNamespace(add_handler=handlers.append)
        wire_telegram_menu(app, SimpleNamespace())
        self.assertEqual(len(handlers), 2)
        def update(text, chat_type="private"):
            entity = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
            return Update.de_json({"update_id": 1, "message": {
                "message_id": 1, "date": 0, "chat": {"id": 123, "type": chat_type},
                "from": {"id": 123, "first_name": "Synthetic", "is_bot": False},
                "text": text, "entities": entity}}, None)
        self.assertTrue(handlers[0].check_update(update("Выгрузить новые лиды")))
        self.assertFalse(handlers[0].check_update(update("Проанализируй закупку по СХД")))
        self.assertFalse(handlers[0].check_update(update("/stop")))
        self.assertFalse(handlers[0].check_update(update("Выгрузить новые лиды", "group")))
        self.assertTrue(handlers[1].filters.check_update(update("/menu")))
        self.assertEqual(handlers[1].commands,
                         frozenset(("start", "menu", "help", "guide", "leads", "new", "work", "export", "data", "dialog", "halt")))


if __name__ == "__main__":
    unittest.main()
