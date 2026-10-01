"""Private SQLite digest journal, independent of bot construction and polling.

Only the parent observer may register already-authorized private connections.
The journal never reads host application state or discovers users on its own.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import stat


def _private_chat(chat_id: int, user_id: int | None = None) -> None:
    if type(chat_id) is not int or not 0 < chat_id <= 2**63 - 1:
        raise ValueError("digest requires a positive private chat id")
    if user_id is not None and (type(user_id) is not int or user_id != chat_id):
        raise ValueError("digest chat must equal the authorized private user")


def _key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("invalid digest journal key")


def _sha256(value: str) -> None:
    if type(value) is not str or len(value) != 64 or any(char not in '0123456789abcdef' for char in value):
        raise ValueError("analysis cache key must be a lowercase sha256")


def _fingerprints(values: list[str]) -> set[str]:
    if type(values) not in (list, tuple, set) or len(values) > 5000:
        raise ValueError("invalid digest fingerprints")
    if any(type(value) is not str or not value or len(value) > 256 for value in values):
        raise ValueError("invalid digest fingerprint")
    return set(values)


def _json_payload(payload: dict) -> str:
    if type(payload) is not dict:
        raise ValueError("digest payload must be a JSON object")

    def check(value, depth=0):
        if depth > 50:
            raise ValueError("digest JSON is too deeply nested")
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError("digest JSON keys must be strings")
            for item in value.values():
                check(item, depth + 1)
        elif type(value) is list:
            for item in value:
                check(item, depth + 1)
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("digest JSON numbers must be finite")
        elif value is not None and type(value) not in (str, bool, int):
            raise ValueError("digest payload must contain only JSON values")

    try:
        check(payload)
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                sort_keys=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > 500_000:
            raise ValueError("digest JSON exceeds 500000 bytes")
        return serialized
    except (TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("invalid digest JSON") from exc


def _decode_payload(serialized: str) -> dict:
    try:
        payload = json.loads(serialized)
        _json_payload(payload)
        return payload
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("invalid stored digest JSON") from exc


class DigestStore:
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

            connection.execute(
                "CREATE TABLE IF NOT EXISTS prepared_slots ("
                "slot TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

            connection.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                "slot TEXT NOT NULL, chat_id INTEGER NOT NULL CHECK(chat_id>0), "
                "state TEXT NOT NULL CHECK(state IN ('claimed','sent','uncertain')), "
                "message_id INTEGER, failure_kind TEXT, "
                "PRIMARY KEY(slot,chat_id))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS seen ("
                "chat_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, "
                "PRIMARY KEY(chat_id,fingerprint))"
            )

            connection.execute(
                "CREATE TABLE IF NOT EXISTS analysis_cache ("
                "sha256 TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def load_analysis(self, sha256: str) -> dict | None:
        _sha256(sha256)
        with closing(self._connection()) as connection:
            row = connection.execute("SELECT payload FROM analysis_cache WHERE sha256=?", (sha256,)).fetchone()
        return None if row is None else _decode_payload(row[0])

    def save_analysis(self, sha256: str, payload: dict) -> None:
        _sha256(sha256)
        serialized = _json_payload(payload)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO analysis_cache(sha256,payload) VALUES (?,?) "
                "ON CONFLICT(sha256) DO UPDATE SET payload=excluded.payload", (sha256, serialized)
            )

    def delivery_state(self, slot: str, chat_id: int) -> str | None:
        """Return a non-sensitive state; no reservation is ever automatically reset."""
        _key(slot)
        _private_chat(chat_id)
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT state FROM deliveries WHERE slot=? AND chat_id=?", (slot, chat_id)
            ).fetchone()
        return None if row is None else row[0]

    def claim_delivery(self, slot: str, chat_id: int, *, require_subscription: bool = False) -> bool:
        """Commit an at-most-once reservation BEFORE any Telegram attempt."""
        _key(slot)
        _private_chat(chat_id)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if require_subscription and connection.execute("SELECT 1 FROM subscriptions WHERE chat_id=? AND user_id=chat_id AND enabled=1",(chat_id,)).fetchone() is None:
                return False
            return connection.execute(
                "INSERT INTO deliveries(slot,chat_id,state) VALUES (?,?,'claimed') "
                "ON CONFLICT(slot,chat_id) DO NOTHING", (slot, chat_id)
            ).rowcount == 1

    def record_delivery(self, slot: str, chat_id: int, message_id: int,
                        fingerprints: list[str]) -> None:
        _key(slot)
        _private_chat(chat_id)
        if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
            raise ValueError("invalid Telegram receipt")
        checked = _fingerprints(fingerprints)
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,message_id FROM deliveries WHERE slot=? AND chat_id=?", (slot, chat_id)
            ).fetchone()
            if row is None:
                raise ValueError("digest delivery was not claimed")
            if row[0] == 'sent' and row[1] != message_id:
                raise ValueError("conflicting Telegram receipt")
            connection.execute(
                "UPDATE deliveries SET state='sent',message_id=?,failure_kind=NULL "
                "WHERE slot=? AND chat_id=?", (message_id, slot, chat_id)
            )
            connection.executemany(
                "INSERT INTO seen(chat_id,fingerprint) VALUES (?,?) "
                "ON CONFLICT(chat_id,fingerprint) DO NOTHING", [(chat_id, value) for value in checked]
            )

    def record_failure(self, slot: str, chat_id: int, kind: str) -> None:
        """Retain the claim permanently; failure NEVER means safe to retry."""
        _key(slot)
        _private_chat(chat_id)
        if kind not in {'uncertain', 'timeout', 'forbidden', 'invalid_receipt', 'cancelled'}:
            raise ValueError("failure kind must be a non-sensitive classification")
        with closing(self._connection()) as connection, connection:
            changed = connection.execute(
                "UPDATE deliveries SET state='uncertain',failure_kind=? "
                "WHERE slot=? AND chat_id=? AND state!='sent'", (kind, slot, chat_id)
            ).rowcount
            if not changed and connection.execute(
                "SELECT 1 FROM deliveries WHERE slot=? AND chat_id=?", (slot, chat_id)
            ).fetchone() is None:
                raise ValueError("digest delivery was not claimed")

    def unseen(self, chat_id: int, fingerprints: list[str]) -> set[str]:
        _private_chat(chat_id)
        checked = _fingerprints(fingerprints)
        with closing(self._connection()) as connection:
            # Keep SQL variables bounded even for a full 500-item prepared slot.
            return {value for value in checked if connection.execute(
                "SELECT 1 FROM seen WHERE chat_id=? AND fingerprint=?", (chat_id, value)
            ).fetchone() is None}

    def prepared(self, slot: str) -> dict | None:
        _key(slot)
        with closing(self._connection()) as connection:
            row = connection.execute("SELECT payload FROM prepared_slots WHERE slot=?", (slot,)).fetchone()
        return None if row is None else _decode_payload(row[0])

    def save_prepared(self, slot: str, payload: dict) -> None:
        """Seal one slot; replay may reuse it but cannot change its contents."""
        _key(slot)
        serialized = _json_payload(payload)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO prepared_slots(slot,payload) VALUES (?,?) "
                "ON CONFLICT(slot) DO NOTHING", (slot, serialized)
            )
            if connection.execute("SELECT payload FROM prepared_slots WHERE slot=?", (slot,)).fetchone()[0] != serialized:
                raise ValueError("digest slot already has different prepared content")

    def _check_paths(self) -> None:
        for path in (self.path, *self.path.parents,
                     Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"),
                     Path(str(self.path) + "-journal")):
            if path.is_symlink():
                raise ValueError("digest journal paths must not be symlinks")
        if self.path.exists() and not stat.S_ISREG(self.path.stat().st_mode):
            raise ValueError("digest journal must be a regular file")

    def _connection(self) -> sqlite3.Connection:
        self._check_paths()
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def touch(self, chat_id: int, user_id: int) -> None:
        """Enable the first connection only; never undo an explicit opt-out."""
        if type(user_id) is not int:
            raise ValueError("digest requires an explicit private user id")
        _private_chat(chat_id, user_id)
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "INSERT INTO subscriptions(chat_id,user_id,enabled) VALUES (?,?,1) "
                "ON CONFLICT(chat_id) DO NOTHING", (chat_id, user_id)
            )

    def subscribe(self, chat_id: int, user_id: int) -> None:
        """Explicit user action may re-enable a previously disabled digest."""
        if type(user_id) is not int:
            raise ValueError("digest requires an explicit private user id")
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

    def seed(self, allowed_users: set[int]) -> None:
        """Deployment-only seed of historically confirmed private users.

        Parent calls this explicitly once, not from scheduled jobs. Existing
        opt-outs are retained even if deployment initialization is repeated.
        """
        for user_id in sorted(allowed_users):
            self.touch(chat_id=user_id, user_id=user_id)

    def recipients(self, allowed_users: set[int]) -> list[int]:
        allowed = {user for user in allowed_users if type(user) is int and user > 0}
        with closing(self._connection()) as connection:
            return [row[0] for row in connection.execute(
                "SELECT chat_id FROM subscriptions WHERE enabled=1 "
                "AND chat_id=user_id AND chat_id>0 ORDER BY chat_id"
            ) if row[0] in allowed]


def _delivery_text(text: str) -> None:
    if type(text) is not str or not text.strip():
        raise ValueError("digest text must be nonempty")
    try:
        units = len(text.encode("utf-16-le")) // 2
    except UnicodeError as exc:
        raise ValueError("digest text contains invalid Unicode") from exc
    if units > 4000:
        # Reject, don't truncate: omitted items must not be marked seen.
        raise ValueError("digest text exceeds 4000 UTF-16 units")
    lowered = text.lower()
    if any(marker in lowered for marker in (
        "<|", "[inst]", "[/inst]", "<think", "</think", "<analysis", "</analysis",
        "<reasoning", "</reasoning", "[analysis]", "[final]", "<<sys>>", "<tool_call",
        "<function_call", "<scratchpad", "</scratchpad",
    )):
        raise ValueError("digest text contains internal model markers")


def _forbidden(error: Exception) -> bool:
    # Keep import/use of the journal independent of python-telegram-bot.
    try:
        from telegram.error import Forbidden
    except ImportError:
        return False
    return isinstance(error, Forbidden)


async def deliver(store: DigestStore, slot: str, recipients, render, bot,
                  *, sleep=asyncio.sleep) -> dict[str, int]:
    """Send through the existing bot only, never construct a consumer.

    Parent passes recipients from store.recipients(current_authorized_users).
    render is a synchronous pure function returning (text, fingerprints).
    Counts contain no chat identifiers or sensitive exception strings.
    """
    counts = {"sent": 0, "skipped": 0, "uncertain": 0, "blocked": 0}
    recipients = tuple(recipients)
    allowed_snapshot = set(recipients)
    for chat_id in recipients:
        _private_chat(chat_id)
        if store.delivery_state(slot, chat_id) is not None:
            counts["skipped"] += 1
            continue
        text, fingerprints = render(chat_id)
        _delivery_text(text)
        fingerprints = sorted(_fingerprints(fingerprints))
        # The caller's authorized snapshot is not a durable subscription: an
        # opt-out may arrive during preparation, rendering or earlier sends.
        if chat_id not in store.recipients(allowed_snapshot):
            counts["skipped"] += 1
            continue
        if not store.claim_delivery(slot, chat_id):
            counts["skipped"] += 1
            continue
        try:
            receipt = await bot.send_message(chat_id=chat_id, text=text,
                                             disable_web_page_preview=True)
            store.record_delivery(slot, chat_id, receipt.message_id, fingerprints)
            counts["sent"] += 1
        except asyncio.CancelledError:
            store.record_failure(slot, chat_id, "cancelled")
            raise
        except Exception as error:
            if _forbidden(error):
                store.record_failure(slot, chat_id, "forbidden")
                store.unsubscribe(chat_id)
                counts["blocked"] += 1
            else:
                store.record_failure(slot, chat_id, "uncertain")
                counts["uncertain"] += 1
        await sleep(0.2)
    return counts
