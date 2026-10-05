"""Who receives lead cards: a private SQLite list of opted-in private chats.

The caller registers already-authorized private connections.
The list never discovers users or reads runtime state on its own.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat


def _private_chat(chat_id: int, user_id: int | None = None) -> None:
    if type(chat_id) is not int or not 0 < chat_id <= 2**63 - 1:
        raise ValueError("a subscription requires a positive private chat id")
    if user_id is not None and (type(user_id) is not int or user_id != chat_id):
        raise ValueError("the subscribed chat must equal the authorized private user")


class Subscriptions:
    """Durable local subscriptions; authorization stays with the caller."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self._check_paths()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with closing(self._connection()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions ("
                "chat_id INTEGER PRIMARY KEY CHECK(chat_id > 0), "
                "user_id INTEGER NOT NULL CHECK(user_id = chat_id), "
                "enabled INTEGER NOT NULL CHECK(enabled IN (0,1)))"
            )

    def _check_paths(self) -> None:
        for path in (self.path, *self.path.parents,
                     Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"),
                     Path(str(self.path) + "-journal")):
            if path.is_symlink():
                raise ValueError("subscription paths must not be symlinks")
        if self.path.exists() and not stat.S_ISREG(self.path.stat().st_mode):
            raise ValueError("the subscription list must be a regular file")

    def _connection(self) -> sqlite3.Connection:
        self._check_paths()
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def touch(self, chat_id: int, user_id: int) -> None:
        """Enable the first connection only; never undo an explicit opt-out."""
        if type(user_id) is not int:
            raise ValueError("a subscription requires an explicit private user id")
        _private_chat(chat_id, user_id)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO subscriptions(chat_id,user_id,enabled) VALUES (?,?,1) "
                "ON CONFLICT(chat_id) DO NOTHING", (chat_id, user_id)
            )

    def subscribe(self, chat_id: int, user_id: int) -> None:
        """Explicit user action may re-enable previously disabled cards."""
        if type(user_id) is not int:
            raise ValueError("a subscription requires an explicit private user id")
        _private_chat(chat_id, user_id)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO subscriptions(chat_id,user_id,enabled) VALUES (?,?,1) "
                "ON CONFLICT(chat_id) DO UPDATE SET enabled=1", (chat_id, user_id)
            )

    def unsubscribe(self, chat_id: int) -> None:
        _private_chat(chat_id)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO subscriptions(chat_id,user_id,enabled) VALUES (?,?,0) "
                "ON CONFLICT(chat_id) DO UPDATE SET enabled=0", (chat_id, chat_id)
            )

    def recipients(self, allowed_users: set[int]) -> list[int]:
        allowed = {user for user in allowed_users if type(user) is int and user > 0}
        with closing(self._connection()) as connection:
            return [row[0] for row in connection.execute(
                "SELECT chat_id FROM subscriptions WHERE enabled=1 "
                "AND chat_id=user_id AND chat_id>0 ORDER BY chat_id"
            ) if row[0] in allowed]
