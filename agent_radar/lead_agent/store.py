"""Durable lead journal: one agent result per procurement version, at-most-once delivery, manager feedback.

Same file conventions as DigestStore (private directory, regular file, no symlinks). Subscriptions and
authorization stay with the caller; this store never decides who may read a lead.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

# Two buttons under every card: the manager's verdict is the ground truth for later evaluation.
ACTIONS = {
    "work": "🔥 В работу",
    "skip": "❌ Мимо",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LeadStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self._check_paths()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        with closing(self._connection()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS leads ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, tender_id TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                "model TEXT NOT NULL, cost_usd REAL NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, "
                "tool_calls INTEGER NOT NULL, verdict TEXT NOT NULL, grade TEXT, deliverable INTEGER NOT NULL, "
                "result_json TEXT NOT NULL, card_html TEXT, created_at TEXT NOT NULL, "
                "UNIQUE(tender_id, fingerprint))")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS attempts ("
                "tender_id TEXT NOT NULL, fingerprint TEXT NOT NULL, n INTEGER NOT NULL, "
                "PRIMARY KEY(tender_id, fingerprint))")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                "lead_id INTEGER NOT NULL REFERENCES leads(id), chat_id INTEGER NOT NULL CHECK(chat_id>0), "
                "state TEXT NOT NULL CHECK(state IN ('claimed','sent','uncertain')), message_id INTEGER, "
                "updated_at TEXT NOT NULL, PRIMARY KEY(lead_id, chat_id))")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id), "
                "chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL)")

        with closing(self._connection()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS comments ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL REFERENCES leads(id), "
                "chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL)")

    def _check_paths(self) -> None:
        for path in (self.path, *self.path.parents, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if path.is_symlink():
                raise ValueError("lead journal paths must not be symlinks")
        if self.path.exists() and not stat.S_ISREG(self.path.stat().st_mode):
            raise ValueError("lead journal must be a regular file")

    def _connection(self) -> sqlite3.Connection:
        self._check_paths()
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    # ---- research results -------------------------------------------------
    def has(self, tender_id: str, fingerprint: str) -> bool:
        with closing(self._connection()) as connection:
            return connection.execute("SELECT 1 FROM leads WHERE tender_id=? AND fingerprint=?",
                                      (tender_id, fingerprint)).fetchone() is not None

    def save(self, *, tender_id: str, fingerprint: str, model: str, cost_usd: float, tokens_in: int,
             tokens_out: int, tool_calls: int, result: dict, card_html: str | None, deliverable: bool) -> int | None:
        """Insert once per procurement version; returns the lead id, or None if it already existed."""
        with closing(self._connection()) as connection, connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO leads(tender_id,fingerprint,model,cost_usd,tokens_in,tokens_out,tool_calls,"
                "verdict,grade,deliverable,result_json,card_html,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tender_id, fingerprint, model, cost_usd, tokens_in, tokens_out, tool_calls,
                 str(result.get("verdict") or "unknown"), result.get("grade"), int(deliverable),
                 json.dumps(result, ensure_ascii=False), card_html, _now()))
            return cursor.lastrowid if cursor.rowcount else None

    def bump_attempt(self, tender_id: str, fingerprint: str) -> int:
        with closing(self._connection()) as connection, connection:
            connection.execute("INSERT INTO attempts(tender_id,fingerprint,n) VALUES (?,?,1) "
                               "ON CONFLICT(tender_id,fingerprint) DO UPDATE SET n=n+1", (tender_id, fingerprint))
            return connection.execute("SELECT n FROM attempts WHERE tender_id=? AND fingerprint=?",
                                      (tender_id, fingerprint)).fetchone()[0]

    def get(self, lead_id: int) -> dict | None:
        with closing(self._connection()) as connection:
            row = connection.execute("SELECT id,tender_id,verdict,grade,card_html,result_json FROM leads WHERE id=?",
                                     (lead_id,)).fetchone()
        return None if row is None else {"id": row[0], "tender_id": row[1], "verdict": row[2], "grade": row[3],
                                         "card_html": row[4], "result": json.loads(row[5])}

    # ---- delivery: claim before send, never retry an uncertain outcome ---------
    def undelivered(self, chat_id: int, limit: int = 10) -> list[dict]:
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT id,tender_id,grade,card_html FROM leads WHERE deliverable=1 AND card_html IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.lead_id=leads.id AND d.chat_id=?) "
                "ORDER BY CASE grade WHEN 'A' THEN 0 ELSE 1 END, id LIMIT ?", (chat_id, int(limit))).fetchall()
        return [{"id": r[0], "tender_id": r[1], "grade": r[2], "card_html": r[3]} for r in rows]

    def claim(self, lead_id: int, chat_id: int) -> bool:
        with closing(self._connection()) as connection, connection:
            return connection.execute(
                "INSERT OR IGNORE INTO deliveries(lead_id,chat_id,state,updated_at) VALUES (?,?,'claimed',?)",
                (lead_id, chat_id, _now())).rowcount == 1

    def release(self, lead_id: int, chat_id: int) -> None:
        """Drop a claim only when Telegram definitely did not accept the message (e.g. flood-control)."""
        with closing(self._connection()) as connection, connection:
            connection.execute("DELETE FROM deliveries WHERE lead_id=? AND chat_id=? AND state='claimed'", (lead_id, chat_id))

    def record_sent(self, lead_id: int, chat_id: int, message_id: int) -> None:
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("missing Telegram receipt")
        with closing(self._connection()) as connection, connection:
            connection.execute("UPDATE deliveries SET state='sent',message_id=?,updated_at=? WHERE lead_id=? AND chat_id=?",
                               (message_id, _now(), lead_id, chat_id))

    def record_uncertain(self, lead_id: int, chat_id: int) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute("UPDATE deliveries SET state='uncertain',updated_at=? WHERE lead_id=? AND chat_id=? "
                               "AND state='claimed'", (_now(), lead_id, chat_id))

    # ---- manager feedback -------------------------------------------------
    def add_feedback(self, lead_id: int, chat_id: int, user_id: int, action: str) -> bool:
        """Record a button press only for a lead that was actually delivered to this chat."""
        if action not in ACTIONS:
            raise ValueError("unknown feedback action")
        with closing(self._connection()) as connection, connection:
            delivered = connection.execute("SELECT 1 FROM deliveries WHERE lead_id=? AND chat_id=?",
                                           (lead_id, chat_id)).fetchone()
            if not delivered:
                return False
            connection.execute("INSERT INTO feedback(lead_id,chat_id,user_id,action,created_at) VALUES (?,?,?,?,?)",
                               (lead_id, chat_id, user_id, action, _now()))
            return True

    def feedback(self, lead_id: int) -> list[str]:
        with closing(self._connection()) as connection:
            return [row[0] for row in connection.execute(
                "SELECT action FROM feedback WHERE lead_id=? ORDER BY id", (lead_id,))]

    def in_work(self, user_id: int) -> list[dict]:
        """Leads whose latest verdict by this user is "В работу", oldest first (the export sheet)."""
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT l.id,l.tender_id,l.grade,l.result_json,l.card_html,f.created_at FROM feedback f "
                "JOIN leads l ON l.id=f.lead_id WHERE f.user_id=? AND f.action='work' "
                "AND f.id=(SELECT MAX(id) FROM feedback WHERE lead_id=f.lead_id AND user_id=?) ORDER BY f.id",
                (user_id, user_id)).fetchall()
            comments = self._comments(connection, [r[0] for r in rows], user_id)
        return [{"id": r[0], "tender_id": r[1], "grade": r[2], "result": json.loads(r[3]), "card_html": r[4], "taken_at": r[5], "comments": comments.get(r[0], [])}
                for r in rows]

    def new_leads(self, chat_id: int, user_id: int) -> list[dict]:
        """Cards delivered to this chat that this manager has not answered with any button yet, oldest first."""
        with closing(self._connection()) as connection:
            rows = connection.execute(
                "SELECT l.id,l.tender_id,l.grade,l.result_json,l.card_html,d.updated_at FROM deliveries d "
                "JOIN leads l ON l.id=d.lead_id WHERE d.chat_id=? AND d.state='sent' "
                "AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.lead_id=l.id AND f.user_id=?) "
                "ORDER BY d.updated_at, l.id", (chat_id, user_id)).fetchall()
            comments = self._comments(connection, [r[0] for r in rows], user_id)
        return [{"id": r[0], "tender_id": r[1], "grade": r[2], "result": json.loads(r[3]), "card_html": r[4], "stamp": r[5], "comments": comments.get(r[0], [])}
                for r in rows]

    # ---- manager notes: a reply to a delivered card ------------------------------
    def has_card(self, chat_id: int, message_id: int) -> bool:
        """True when this Telegram message is a lead card that was delivered (and confirmed) to this chat."""
        with closing(self._connection()) as connection:
            return connection.execute("SELECT 1 FROM deliveries WHERE chat_id=? AND message_id=? AND state='sent'",
                                      (chat_id, message_id)).fetchone() is not None

    def add_comment(self, chat_id: int, user_id: int, message_id: int, text: str) -> bool:
        lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
        text = "\n".join(line for line in lines if line)[:2000]
        if not text:
            return False
        with closing(self._connection()) as connection, connection:
            row = connection.execute("SELECT lead_id FROM deliveries WHERE chat_id=? AND message_id=? AND state='sent'",
                                     (chat_id, message_id)).fetchone()
            if row is None:
                return False
            connection.execute("INSERT INTO comments(lead_id,chat_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
                               (row[0], chat_id, user_id, text, _now()))
            return True

    def _comments(self, connection, lead_ids: list[int], user_id: int) -> dict[int, list[dict]]:
        if not lead_ids:
            return {}
        marks = ",".join("?" * len(lead_ids))
        found: dict[int, list[dict]] = {}
        for lead_id, text, created in connection.execute(
                f"SELECT lead_id,text,created_at FROM comments WHERE user_id=? AND lead_id IN ({marks}) ORDER BY id",
                [user_id, *lead_ids]):
            found.setdefault(lead_id, []).append({"text": text, "at": created})
        return found

    def stats(self) -> dict:
        with closing(self._connection()) as connection:
            total, cost, deliverable = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost_usd),0), COALESCE(SUM(deliverable),0) FROM leads").fetchone()
            feedback = dict(connection.execute("SELECT action, COUNT(*) FROM feedback GROUP BY action").fetchall())
        return {"leads": total, "deliverable": deliverable, "cost_usd": round(cost, 4), "feedback": feedback}
