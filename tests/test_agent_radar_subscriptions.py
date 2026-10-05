"""Offline public-boundary checks for the list of chats that receive lead cards."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from agent_radar.subscriptions import Subscriptions


class SubscriptionsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "digests.sqlite3"
        self.store = Subscriptions(self.path)

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
        self.assertEqual(Subscriptions(self.path).recipients({123, 456}), [123, 456])

    def test_touch_preserves_opt_out_until_explicit_subscribe(self):
        self.store.touch(chat_id=123, user_id=123)
        self.store.touch(chat_id=456, user_id=456)
        self.store.unsubscribe(chat_id=123)
        self.store.touch(chat_id=123, user_id=123)
        self.store.touch(chat_id=789, user_id=789)
        self.assertEqual(Subscriptions(self.path).recipients({123, 456, 789}), [456, 789])
        self.store.subscribe(chat_id=123, user_id=123)
        self.assertEqual(self.store.recipients({123, 456}), [123, 456])

    def test_opt_out_before_observer_connection_stays_disabled(self):
        self.store.unsubscribe(chat_id=123)
        self.store.touch(chat_id=123, user_id=123)
        self.assertEqual(self.store.recipients({123}), [])
        with self.assertRaises(ValueError):
            self.store.subscribe(chat_id=-123, user_id=123)

    def test_reads_the_list_kept_by_the_previous_digest_journal(self):
        # The production file predates this module and still carries the digest-era tables.
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE deliveries (slot TEXT NOT NULL, chat_id INTEGER NOT NULL)")
            connection.execute("INSERT INTO subscriptions(chat_id,user_id,enabled) VALUES (123,123,1),(456,456,0)")
        connection.close()
        self.assertEqual(Subscriptions(self.path).recipients({123, 456}), [123])

    def test_private_posix_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions")
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
                    Subscriptions(candidate)
                if link.name.endswith("-wal"):
                    with self.assertRaises(ValueError):
                        self.store.recipients({123})
                self.assertEqual(target.read_text(encoding="utf-8"), "must not modify")
            finally:
                link.unlink()


if __name__ == "__main__":
    unittest.main()
