"""Offline public-boundary checks for the pure digest journal."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent_radar.digest_store import DigestStore


class DigestStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "digests.sqlite3"
        self.store = DigestStore(self.path)

    def test_first_connection_enables_only_authorized_private_users(self):
        self.store.touch(chat_id=123, user_id=123)
        self.store.touch(chat_id=456, user_id=456)
        self.store.touch(chat_id=789, user_id=789)
        self.assertEqual(self.store.recipients({123, 456}), [123, 456])
        self.assertEqual(self.store.recipients({456}), [456])
        self.assertEqual(self.store.recipients(set()), [])
        for chat_id, user_id in [(-123, 123), (123, 456), (0, 0), (True, True), (123, None)]:
            with self.subTest(chat_id=chat_id, user_id=user_id):
                with self.assertRaises(ValueError):
                    self.store.touch(chat_id=chat_id, user_id=user_id)
        self.assertEqual(DigestStore(self.path).recipients({123, 456}), [123, 456])

    def test_touch_and_seed_preserve_opt_out_until_explicit_subscribe(self):
        self.store.seed({123, 456})
        self.store.unsubscribe(chat_id=123)
        self.store.touch(chat_id=123, user_id=123)
        self.store.touch(chat_id=789, user_id=789)
        self.store.seed({123, 456})
        self.assertEqual(DigestStore(self.path).recipients({123, 456, 789}), [456, 789])
        self.store.subscribe(chat_id=123, user_id=123)
        self.assertEqual(self.store.recipients({123, 456}), [123, 456])

    def test_opt_out_before_observer_connection_stays_disabled(self):
        self.store.unsubscribe(chat_id=123)
        self.store.touch(chat_id=123, user_id=123)
        self.assertEqual(self.store.recipients({123}), [])
        with self.assertRaises(ValueError):
            self.store.subscribe(chat_id=-123, user_id=123)

    def test_prepared_slot_survives_restart_and_is_immutable_bounded_json(self):
        slot = "2026-09-29T09:00:00+03:00"
        payload = {"text": "Закупки", "items": [{"fingerprint": "alpha"}]}
        self.assertIsNone(self.store.prepared(slot))
        self.store.save_prepared(slot, payload)
        payload["items"].clear()
        reopened = DigestStore(self.path)
        self.assertEqual(reopened.prepared(slot), {"text": "Закупки", "items": [{"fingerprint": "alpha"}]})
        reopened.save_prepared(slot, reopened.prepared(slot))
        with self.assertRaises(ValueError):
            reopened.save_prepared(slot, {"text": "different"})
        for invalid in [[], {"text": "я" * 250001}, {"score": float("nan")}, {1: "non-string key"}]:
            with self.subTest(invalid_type=type(invalid)):
                with self.assertRaises(ValueError):
                    reopened.save_prepared("bad", invalid)
        self.assertIsNone(reopened.prepared("bad"))

    def test_subscription_guard_is_checked_atomically_by_claim_not_only_the_renderer(self):
        self.store.subscribe(123,123)
        self.store.unsubscribe(123)
        self.assertFalse(self.store.claim_delivery("guarded",123,require_subscription=True))
        self.assertIsNone(self.store.delivery_state("guarded",123))
        self.store.subscribe(123,123)
        self.assertTrue(self.store.claim_delivery("guarded",123,require_subscription=True))

    def test_delivery_claim_is_durable_per_slot_and_recipient_and_seen_is_private(self):
        self.assertTrue(self.store.claim_delivery("morning", 123))
        reopened = DigestStore(self.path)
        self.assertFalse(reopened.claim_delivery("morning", 123))
        self.assertTrue(reopened.claim_delivery("morning", 456))
        self.assertTrue(reopened.claim_delivery("noon", 123))
        self.assertEqual(reopened.unseen(123, ["alpha", "beta", "alpha"]), {"alpha", "beta"})
        reopened.record_delivery("morning", 123, 901, ["alpha"])
        self.assertEqual(DigestStore(self.path).unseen(123, ["alpha", "beta"]), {"beta"})
        self.assertEqual(reopened.unseen(456, ["alpha", "beta"]), {"alpha", "beta"})
        reopened.record_failure("morning", 456, "timeout")
        self.assertFalse(DigestStore(self.path).claim_delivery("morning", 456))
        self.assertEqual(reopened.unseen(456, ["alpha"]), {"alpha"})

    def test_competing_process_connections_only_one_claim_wins(self):
        stores = [DigestStore(self.path) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as workers:
            results = list(workers.map(lambda store: store.claim_delivery("race", 123), stores))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.store.delivery_state("race", 123), "claimed")

    def test_receipt_requires_claim_and_only_receipt_marks_seen(self):
        with self.assertRaises(ValueError):
            self.store.record_delivery("unknown", 123, 9, ["alpha"])
        self.store.claim_delivery("morning", 123)
        self.store.record_failure("morning", 123, "timeout")
        self.assertEqual(self.store.delivery_state("morning", 123), "uncertain")
        self.store.record_delivery("morning", 123, 9, ["alpha"])
        self.assertEqual(self.store.delivery_state("morning", 123), "sent")
        self.store.record_delivery("morning", 123, 9, ["alpha"])
        self.store.record_failure("morning", 123, "timeout")
        self.assertEqual(self.store.delivery_state("morning", 123), "sent")
        with self.assertRaises(ValueError):
            self.store.record_delivery("morning", 123, 10, ["beta"])
        self.assertEqual(self.store.unseen(123, ["alpha", "beta"]), {"beta"})
        with self.assertRaises(ValueError):
            self.store.record_failure("morning", 123, "secret endpoint/token")

    @unittest.skipUnless(os.name == "posix", "0600/0700 are POSIX mode guarantees, not Windows ACLs")
    def test_private_posix_permissions(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_rejects_database_parent_and_sqlite_sidecar_symlinks(self):
        target = Path(self.temporary.name) / "target.sqlite3"
        target.write_text("must not modify", encoding="utf-8")
        directory = Path(self.temporary.name) / "real-directory"
        directory.mkdir()
        cases = [(Path(self.temporary.name) / "linked.sqlite3", target, False),
                 (Path(self.temporary.name) / "linked-directory", directory, True),
                 (Path(str(self.path) + "-wal"), target, False)]
        for link, destination, is_directory in cases:
            try:
                link.symlink_to(destination, target_is_directory=is_directory)
            except (OSError, NotImplementedError) as error:
                self.skipTest("host cannot create symlinks: " + type(error).__name__)
            try:
                candidate = link / "digest.sqlite3" if is_directory else (
                    self.path if link.name.endswith("-wal") else link)
                with self.assertRaises(ValueError):
                    DigestStore(candidate)
                if link.name.endswith("-wal"):
                    with self.assertRaises(ValueError):
                        self.store.recipients({123})
                self.assertEqual(target.read_text(encoding="utf-8"), "must not modify")
            finally:
                link.unlink()

    def test_analysis_cache_is_sha256_keyed_bounded_safe_json(self):
        key = "a" * 64
        other = "b" * 64
        self.assertIsNone(self.store.load_analysis(key))
        payload = {"decision": "relevant", "evidence": ["quote"]}
        self.store.save_analysis(key, payload)
        loaded = DigestStore(self.path).load_analysis(key)
        self.assertEqual(loaded, payload)
        loaded["evidence"].clear()
        self.assertEqual(self.store.load_analysis(key)["evidence"], ["quote"])
        self.assertIsNone(self.store.load_analysis(other))
        for invalid in ["not-a-hash", "A" * 64, "a" * 63]:
            with self.assertRaises(ValueError):
                self.store.save_analysis(invalid, payload)
        with self.assertRaises(ValueError):
            self.store.save_analysis(other, {"text": "x" * 500001})
        self.assertIsNone(self.store.load_analysis(other))


class DigestDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "digests.sqlite3"
        self.store = DigestStore(self.path)
        self.store.seed({123, 456})
        self.sleep = AsyncMock()

    async def test_opt_out_after_recipient_snapshot_prevents_send_and_claim(self):
        from agent_radar.digest_store import deliver
        recipients = self.store.recipients({123, 456})
        self.store.unsubscribe(123)
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
        result = await deliver(self.store, "opt-out-race", recipients,
                               lambda chat: ("Закупки", ["alpha"]), bot, sleep=self.sleep)
        self.assertEqual(result, {"sent": 1, "skipped": 1, "uncertain": 0, "blocked": 0})
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["chat_id"], 456)
        self.assertIsNone(self.store.delivery_state("opt-out-race", 123))
        self.assertEqual(self.store.unseen(123, ["alpha"]), {"alpha"})

    async def test_opt_out_during_render_is_rechecked_before_claim(self):
        from agent_radar.digest_store import deliver
        def render(chat_id):
            self.store.unsubscribe(chat_id)
            return "Закупки", ["alpha"]
        bot = SimpleNamespace(send_message=AsyncMock())
        result = await deliver(self.store, "render-opt-out", [123], render, bot, sleep=self.sleep)
        self.assertEqual(result["skipped"], 1)
        bot.send_message.assert_not_awaited()
        self.assertIsNone(self.store.delivery_state("render-opt-out", 123))

    async def test_claimed_replay_skips_render_and_cancellation_retains_claim(self):
        from agent_radar.digest_store import deliver
        self.store.claim_delivery("already", 123)
        def render(chat_id):
            raise AssertionError("already claimed slot must not be rendered again")
        bot = SimpleNamespace(send_message=AsyncMock())
        result = await deliver(self.store, "already", [123], render, bot, sleep=self.sleep)
        self.assertEqual(result["skipped"], 1)
        bot.send_message.assert_not_awaited()
        bot.send_message = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await deliver(self.store, "cancelled", [123], lambda chat: ("Закупки", ["alpha"]),
                          bot, sleep=self.sleep)
        self.assertFalse(DigestStore(self.path).claim_delivery("cancelled", 123))
        self.assertEqual(self.store.unseen(123, ["alpha"]), {"alpha"})

    async def test_text_is_plain_bounded_utf16_and_invalid_render_does_not_claim(self):
        from agent_radar.digest_store import deliver
        invalid = ["", "x" * 4001, "😀" * 2001, "<think>private reasoning</think>",
                   "<|im_start|>assistant", "[INST]private[/INST]", "<analysis>hidden</analysis>",
                   "bad\ud800text"]
        for index, text in enumerate(invalid):
            with self.subTest(index=index):
                bot = SimpleNamespace(send_message=AsyncMock())
                slot = "unsafe-" + str(index)
                with self.assertRaises(ValueError):
                    await deliver(self.store, slot, [123], lambda chat: (text, ["alpha"]), bot, sleep=self.sleep)
                bot.send_message.assert_not_awaited()
                self.assertTrue(self.store.claim_delivery(slot, 123))
        for index, text in enumerate(["x" * 4000, "😀" * 2000]):
            bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
            result = await deliver(self.store, "safe-" + str(index), [123],
                                   lambda chat: (text, ["alpha"]), bot, sleep=self.sleep)
            self.assertEqual(result["sent"], 1)
            self.assertEqual(bot.send_message.await_args.kwargs["text"], text)
        bot = SimpleNamespace(send_message=AsyncMock())
        with self.assertRaises(ValueError):
            await deliver(self.store, "invalid-fingerprint", [123], lambda chat: ("Good", [None]),
                          bot, sleep=self.sleep)
        bot.send_message.assert_not_awaited()
        self.assertTrue(self.store.claim_delivery("invalid-fingerprint", 123))

    async def test_forbidden_unsubscribes_blocked_user_without_stopping_other_user(self):
        from agent_radar.digest_store import deliver
        class Forbidden(Exception):
            pass
        telegram = ModuleType("telegram")
        error = ModuleType("telegram.error")
        error.Forbidden = Forbidden
        telegram.error = error
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=[
            Forbidden("blocked private user"), SimpleNamespace(message_id=42)]))
        render = lambda chat_id: ("Закупки", ["alpha"])
        with patch.dict("sys.modules", {"telegram": telegram, "telegram.error": error}):
            result = await deliver(self.store, "blocked", [123, 456], render, bot, sleep=self.sleep)
        self.assertEqual(result, {"sent": 1, "skipped": 0, "uncertain": 0, "blocked": 1})
        reopened = DigestStore(self.path)
        reopened.touch(123, 123)
        self.assertEqual(reopened.recipients({123, 456}), [456])
        self.assertFalse(reopened.claim_delivery("blocked", 123))
        self.assertEqual(reopened.unseen(123, ["alpha"]), {"alpha"})
        self.assertEqual(reopened.unseen(456, ["alpha"]), set())

    async def test_timeout_missing_receipt_and_crash_never_retry_after_restart(self):
        from agent_radar.digest_store import deliver
        for outcome in [TimeoutError("secret network detail"), SimpleNamespace(message_id=0),
                        SimpleNamespace(message_id=True), SimpleNamespace()]:
            with self.subTest(outcome=type(outcome)):
                slot = "slot-" + str(id(outcome))
                bot = SimpleNamespace(send_message=AsyncMock(
                    side_effect=outcome if isinstance(outcome, Exception) else None,
                    return_value=outcome))
                render = lambda chat_id: ("Закупки", ["alpha"])
                result = await deliver(self.store, slot, [123], render, bot, sleep=self.sleep)
                replay = await deliver(DigestStore(self.path), slot, [123], render, bot, sleep=self.sleep)
                self.assertEqual(result["uncertain"], 1)
                self.assertEqual(replay["skipped"], 1)
                bot.send_message.assert_awaited_once()
                self.assertEqual(self.store.unseen(123, ["alpha"]), {"alpha"})
        # Process loss after committed claim (including after Telegram accepted
        # but before recording a receipt) has exactly the same no-retry outcome.
        self.assertTrue(self.store.claim_delivery("crash", 123))
        bot = SimpleNamespace(send_message=AsyncMock())
        result = await deliver(DigestStore(self.path), "crash", [123], render, bot, sleep=self.sleep)
        self.assertEqual(result["skipped"], 1)
        bot.send_message.assert_not_awaited()

    async def test_two_recipients_receive_once_with_durable_claim_before_boundary(self):
        from agent_radar.digest_store import deliver
        messages = []

        async def send_message(**kwargs):
            chat_id = kwargs["chat_id"]
            self.assertFalse(DigestStore(self.path).claim_delivery("morning", chat_id))
            messages.append(kwargs)
            return SimpleNamespace(message_id=900 + len(messages))

        bot = SimpleNamespace(send_message=send_message)
        render = lambda chat_id: ("Закупки сегодня", ["alpha"])
        first = await deliver(self.store, "morning", [123, 123, 456], render, bot, sleep=self.sleep)
        replay = await deliver(DigestStore(self.path), "morning", [123, 456], render, bot, sleep=self.sleep)
        self.assertEqual(first, {"sent": 2, "skipped": 1, "uncertain": 0, "blocked": 0})
        self.assertEqual(replay, {"sent": 0, "skipped": 2, "uncertain": 0, "blocked": 0})
        self.assertEqual([message["chat_id"] for message in messages], [123, 456])
        self.assertTrue(all(message["disable_web_page_preview"] for message in messages))
        self.assertEqual(self.store.unseen(123, ["alpha"]), set())
        self.assertEqual(self.store.unseen(456, ["alpha"]), set())
        self.assertEqual(self.sleep.await_count, 2)
        self.assertTrue(all(call.args == (0.2,) for call in self.sleep.await_args_list))


if __name__ == "__main__":
    unittest.main()
