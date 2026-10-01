"""Report delivery through the public journal and injected Telegram transport."""
from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from agent_radar.digest_store import DigestStore


class ReportDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_document_per_chat_is_acknowledged_and_replay_sends_nothing(self):
        from agent_radar.digest_delivery import deliver_report

        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101, 202})
            slot = "2026-09-30-profile-v2-bootstrap"
            calls = []
            pauses = []
            report = "# Серверы и СХД\n\nТочные данные 😀\n"

            class Bot:
                async def send_document(self, **kwargs):
                    self_test.assertEqual(store.delivery_state(slot, kwargs["chat_id"]), "claimed")
                    self_test.assertIsInstance(kwargs["document"], io.BytesIO)
                    calls.append({**kwargs, "document": kwargs["document"].read()})
                    return SimpleNamespace(message_id=7)

            self_test = self

            async def sleep(seconds):
                pauses.append(seconds)

            def render(chat):
                return "Серверы и СХД", report, ["purchase-1"]

            result = await deliver_report(store, slot, [101, 202, 101], render, Bot(), sleep=sleep)
            self.assertEqual(result, {"sent": 2, "skipped": 0, "uncertain": 0, "blocked": 0})
            self.assertEqual(calls, [
                {"chat_id": chat, "document": report.encode("utf-8"),
                 "filename": "servers-storage-report-2026-09-30-profile-v2-bootstrap.md",
                 "caption": "Серверы и СХД", "parse_mode": None}
                for chat in (101, 202)
            ])
            self.assertEqual(pauses, [0.2, 0.2])
            for chat in (101, 202):
                self.assertEqual(store.delivery_state(slot, chat), "sent")
                self.assertEqual(store.unseen(chat, ["purchase-1", "new"]), {"new"})

            def should_not_render(chat):
                self.fail("Replays must not prepare another Telegram response")

            result = await deliver_report(store, slot, [101, 202], should_not_render, Bot(), sleep=sleep)
            self.assertEqual(result, {"sent": 0, "skipped": 2, "uncertain": 0, "blocked": 0})
            self.assertEqual(len(calls), 2)

    async def test_invalid_report_payload_is_rejected_before_reserving_or_sending(self):
        from agent_radar.digest_delivery import deliver_report, validate_report, validate_fingerprints

        unsafe = [
            ("😀" * 501, "report", []),
            ("caption", "😀" * 100001, []),
            ("caption", "report", [""]),
            ("caption", "report", ["x" * 257]),
            ("caption", "report", [True]),
            ("caption", "report", ["\ud800"]),
            ("caption", "report", "not-a-list"),
            ("caption", "report", ["x"] * 5001),
            ("caption", "\ud800", []),
            ("\ud800", "report", []),
            (42, "report", []),
            ("caption", b"report", []),
            ("caption", "   ", []),
        ]
        markers = ("<|im_start|>", "[INST]", "[/INST]", "<think>", "</THINK>",
                   "<analysis>", "</analysis>", "<reasoning>", "</reasoning>",
                   "[analysis]", "[final]", "<<SYS>>", "<tool_call>",
                   "<function_call>", "<scratchpad>", "</scratchpad>")
        for marker in markers:
            unsafe.extend([("caption " + marker, "report", []),
                           ("caption", "report\n" + marker, [])])
        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101})

            class Bot:
                async def send_document(self, **kwargs):
                    raise AssertionError("Invalid payload must never reach Telegram")

            for index, payload in enumerate(unsafe):
                with self.subTest(index=index):
                    with self.assertRaises(ValueError):
                        validate_report(*payload)
                    with self.assertRaises(ValueError):
                        await deliver_report(store, str(index), [101], lambda chat: payload, Bot())
                    self.assertIsNone(store.delivery_state(str(index), 101))
            self.assertEqual(validate_fingerprints(["a", "a", "b"]), ["a", "b"])

    async def test_opt_out_while_rendering_or_after_prior_send_prevents_claim(self):
        from agent_radar.digest_delivery import deliver_report

        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101, 202, 303})
            calls = []

            class Bot:
                async def send_document(self, **kwargs):
                    calls.append(kwargs["chat_id"])
                    store.unsubscribe(303)
                    return SimpleNamespace(message_id=1)

            def render(chat):
                if chat == 101:
                    store.unsubscribe(101)
                return "caption", "report", []

            async def sleep(seconds):
                pass

            result = await deliver_report(store, "slot", [101, 202, 303, 404], render, Bot(), sleep=sleep)
            self.assertEqual(result, {"sent": 1, "skipped": 3, "uncertain": 0, "blocked": 0})
            self.assertEqual(calls, [202])
            for chat in (101, 303, 404):
                self.assertIsNone(store.delivery_state("slot", chat))

    async def test_timeout_or_unknown_transport_outcome_is_permanent_and_not_seen(self):
        from agent_radar.digest_delivery import deliver_report

        for error in (TimeoutError("private data"), RuntimeError("private data")):
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as root:
                store = DigestStore(Path(root) / "digests.sqlite3")
                store.seed({101})
                calls = []
                pauses = []

                class Bot:
                    async def send_document(self, **kwargs):
                        calls.append(kwargs["chat_id"])
                        raise error

                async def sleep(seconds):
                    pauses.append(seconds)

                render = lambda chat: ("caption", "report", ["purchase-1"])
                result = await deliver_report(store, "slot", [101], render, Bot(), sleep=sleep)
                self.assertEqual(result, {"sent": 0, "skipped": 0, "uncertain": 1, "blocked": 0})
                self.assertEqual(store.delivery_state("slot", 101), "uncertain")
                self.assertEqual(store.unseen(101, ["purchase-1"]), {"purchase-1"})
                retry = await deliver_report(store, "slot", [101], render, Bot(), sleep=sleep)
                self.assertEqual(retry, {"sent": 0, "skipped": 1, "uncertain": 0, "blocked": 0})
                self.assertEqual(calls, [101])
                self.assertEqual(pauses, [0.2])

    async def test_forbidden_disables_subscription_and_never_retries(self):
        from agent_radar.digest_delivery import deliver_report
        from telegram.error import Forbidden

        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101})
            calls = []

            class Bot:
                async def send_document(self, **kwargs):
                    calls.append(kwargs["chat_id"])
                    raise Forbidden("private transport response")

            async def sleep(seconds):
                pass

            render = lambda chat: ("caption", "report", [])
            result = await deliver_report(store, "slot", [101], render, Bot(), sleep=sleep)
            self.assertEqual(result, {"sent": 0, "skipped": 0, "uncertain": 0, "blocked": 1})
            self.assertEqual(store.recipients({101}), [])
            self.assertEqual(store.delivery_state("slot", 101), "uncertain")
            await deliver_report(store, "slot", [101], render, Bot(), sleep=sleep)
            await deliver_report(store, "next-slot", [101], render, Bot(), sleep=sleep)
            self.assertEqual(calls, [101])

    async def test_every_recipient_is_a_strict_positive_private_id_before_any_send(self):
        from agent_radar.digest_delivery import deliver_report

        invalid = (True, False, 0, -101, "101", 1.0, 2**63, None, [])
        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({1})

            class Bot:
                async def send_document(self, **kwargs):
                    raise AssertionError("Bad recipient batch must not partially send")

            for index, recipient in enumerate(invalid):
                slot = str(index)
                with self.subTest(recipient=repr(recipient)), self.assertRaises(ValueError):
                    await deliver_report(store, slot, [1, recipient],
                                         lambda chat: ("caption", "report", []), Bot())
                self.assertIsNone(store.delivery_state(slot, 1))

    async def test_cancellation_records_unknown_outcome_and_propagates_without_retry(self):
        import asyncio
        from agent_radar.digest_delivery import deliver_report

        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101})
            calls = []

            class Bot:
                async def send_document(self, **kwargs):
                    calls.append(kwargs["chat_id"])
                    raise asyncio.CancelledError()

            render = lambda chat: ("caption", "report", [])
            with self.assertRaises(asyncio.CancelledError):
                await deliver_report(store, "slot", [101], render, Bot())
            self.assertEqual(store.delivery_state("slot", 101), "uncertain")
            retry = await deliver_report(store, "slot", [101], render, Bot())
            self.assertEqual(retry["skipped"], 1)
            self.assertEqual(calls, [101])

    async def test_filename_sanitization_preserves_the_unrestricted_journal_slot(self):
        from agent_radar.digest_delivery import deliver_report

        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101})
            slot = "2026-09-30/profile_v2:bootstrap"
            calls = []

            class Bot:
                async def send_document(self, **kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(message_id=1)

            async def sleep(seconds):
                pass

            await deliver_report(store, slot, [101], lambda chat: ("caption", "report", []), Bot(), sleep=sleep)
            self.assertEqual(calls[0]["filename"], "servers-storage-report-2026-09-30-profile-v2-bootstrap.md")
            self.assertEqual(store.delivery_state(slot, 101), "sent")
            self.assertIsNone(store.delivery_state("2026-09-30-profile-v2-bootstrap", 101))

    async def test_only_an_actual_positive_integer_receipt_marks_report_seen(self):
        from agent_radar.digest_delivery import deliver_report

        receipts = (True, False, 0, -1, "7", 7.0, None, 2**63)
        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101})

            async def sleep(seconds):
                pass

            for index, message_id in enumerate(receipts):
                with self.subTest(message_id=message_id):
                    class Bot:
                        async def send_document(self, **kwargs):
                            return SimpleNamespace(message_id=message_id)

                    result = await deliver_report(store, str(index), [101],
                        lambda chat: ("caption", "report", ["purchase"]), Bot(), sleep=sleep)
                    self.assertEqual(result, {"sent": 0, "skipped": 0, "uncertain": 1, "blocked": 0})
                    self.assertEqual(store.delivery_state(str(index), 101), "uncertain")
                    self.assertEqual(store.unseen(101, ["purchase"]), {"purchase"})
                    replay = await deliver_report(store, str(index), [101],
                        lambda chat: self.fail("Invalid receipts cannot be retried"), Bot(), sleep=sleep)
                    self.assertEqual(replay["skipped"], 1)

    async def test_caption_and_report_exact_unicode_budgets_are_sent_without_truncation(self):
        from agent_radar.digest_delivery import deliver_report, validate_report

        caption = "😀" * 500
        report = "😀" * 100000
        validate_report(caption, report, [])
        with TemporaryDirectory() as root:
            store = DigestStore(Path(root) / "digests.sqlite3")
            store.seed({101})
            calls = []

            class Bot:
                async def send_document(self, **kwargs):
                    calls.append((kwargs["caption"], kwargs["document"].read()))
                    return SimpleNamespace(message_id=1)

            async def sleep(seconds):
                pass

            result = await deliver_report(store, "slot", [101],
                lambda chat: (caption, report, []), Bot(), sleep=sleep)
            self.assertEqual(result["sent"], 1)
            self.assertEqual(calls, [(caption, report.encode("utf-8"))])

    async def test_base_exceptions_keep_claimed_reservation_without_retry(self):
        from agent_radar.digest_delivery import deliver_report

        class Shutdown(BaseException):
            pass

        for error in (KeyboardInterrupt(), Shutdown()):
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as root:
                store = DigestStore(Path(root) / "digests.sqlite3")
                store.seed({101})
                calls = []

                class Bot:
                    async def send_document(self, **kwargs):
                        calls.append(kwargs["chat_id"])
                        raise error

                with self.assertRaises(type(error)):
                    await deliver_report(store, "slot", [101],
                        lambda chat: ("caption", "report", []), Bot())
                self.assertEqual(store.delivery_state("slot", 101), "claimed")
                replay = await deliver_report(store, "slot", [101],
                    lambda chat: self.fail("Claimed shutdown cannot retry"), Bot())
                self.assertEqual(replay["skipped"], 1)
                self.assertEqual(calls, [101])


if __name__ == "__main__":
    unittest.main()
