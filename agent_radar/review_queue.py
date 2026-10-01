"""Local suggestions, manual decisions and durable Telegram delivery claims."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from contextlib import closing
from pathlib import Path
from typing import Any

from agent_radar.lead import validate_lead
from agent_radar.snapshot import SnapshotStore


class ReviewQueue:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def _ensure_schema(self) -> None:
        with closing(self._connection()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS suggestions ("
                "review_id TEXT PRIMARY KEY, tender_id TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, "
                "payload_sha256 TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_decisions ("
                "review_id TEXT PRIMARY KEY REFERENCES suggestions(review_id), "
                "verdict TEXT NOT NULL CHECK (verdict IN ('approved', 'rejected')), "
                "reviewer TEXT NOT NULL, reason TEXT NOT NULL, decided_at TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS suggestions_tender_status ON suggestions(tender_id, status)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS telegram_delivery ("
                "chat_id INTEGER NOT NULL, tender_id TEXT NOT NULL, review_id TEXT NOT NULL, "
                "state TEXT NOT NULL CHECK (state IN ('claimed', 'delivered')), "
                "claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, message_id INTEGER, "
                "PRIMARY KEY (chat_id, tender_id))"
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def submit(self, payload: dict[str, Any], store: SnapshotStore) -> dict[str, str]:
        checked = validate_lead(payload, store)
        self._ensure_schema()
        serialized = json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        identity = f"{checked['tender_id']}\0{checked['snapshot_sha256']}"
        review_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            # A new snapshot is not an explicit version/review decision. Reuse the
            # first approved tender instead of silently requesting another review.
            prior = connection.execute(
                "SELECT s.review_id, s.payload, s.payload_sha256 FROM suggestions s "
                "JOIN review_decisions d ON d.review_id = s.review_id "
                "WHERE s.tender_id = ? AND s.status = 'approved' AND d.verdict = 'approved' "
                "ORDER BY s.created_at, s.review_id LIMIT 1", (checked["tender_id"],)
            ).fetchone()
            if prior is not None and prior["review_id"] != review_id:
                if hashlib.sha256(prior["payload"].encode("utf-8")).hexdigest() != prior["payload_sha256"]:
                    raise ValueError("review payload checksum mismatch")
                # No decision or approval is recorded for the new snapshot payload.
                return {"review_id": prior["review_id"], "status": "approved"}
            connection.execute(
                "INSERT OR IGNORE INTO suggestions "
                "(review_id, tender_id, snapshot_sha256, payload_sha256, payload, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending_review')",
                (review_id, checked["tender_id"], checked["snapshot_sha256"], digest, serialized),
            )
            row = connection.execute(
                "SELECT payload_sha256, status FROM suggestions WHERE review_id = ?", (review_id,)
            ).fetchone()
            if row is None or row["payload_sha256"] != digest:
                raise ValueError("different suggestion for this tender snapshot; human revision required")
            return {"review_id": review_id, "status": row["status"]}

    def lookup_receipt(self, *, tender_id: str, snapshot_sha256: str,
                       store: SnapshotStore) -> dict[str, str] | None:
        """Recover an exact durable proposal, not approval or delivery permission."""
        store.get_tender(tender_id=tender_id, snapshot_sha256=snapshot_sha256)
        identity = f"{tender_id}\0{snapshot_sha256}"
        review_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if not self.database_path.exists() and not self.database_path.is_symlink():
            return None
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        with closing(sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'suggestions'"
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT tender_id, snapshot_sha256, payload, payload_sha256, status "
                "FROM suggestions WHERE review_id = ?", (review_id,),
            ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise ValueError("review payload checksum mismatch")
        checked = validate_lead(json.loads(row["payload"]), store)
        if (row["tender_id"] != tender_id or row["snapshot_sha256"] != snapshot_sha256
                or checked["tender_id"] != tender_id or checked["snapshot_sha256"] != snapshot_sha256):
            raise ValueError("review receipt identity mismatch")
        if row["status"] not in ("pending_review", "approved", "rejected"):
            raise ValueError("invalid review receipt status")
        return {"review_id": review_id, "status": row["status"],
                "tender_id": tender_id, "snapshot_sha256": snapshot_sha256}

    def record_decision(self, *, review_id: str, verdict: str, reviewer: str, reason: str, decided_at: str,
                        expected_payload_sha256: str | None = None,
                        expected_snapshot_sha256: str | None = None) -> dict[str, str]:
        """Capture an operator-asserted manual decision; never sends or publishes."""
        if not isinstance(review_id, str) or len(review_id) != 64 or any(c not in "0123456789abcdef" for c in review_id):
            raise ValueError("invalid review id")
        if not isinstance(verdict, str) or verdict not in ("approved", "rejected"):
            raise ValueError("invalid review verdict")
        if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 150:
            raise ValueError("invalid reviewer identity")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("invalid review reason")
        if not isinstance(decided_at, str):
            raise ValueError("invalid review time")
        if not decided_at.endswith("Z"):
            raise ValueError("review time must be canonical UTC with Z suffix")
        try:
            parsed = datetime.fromisoformat(decided_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid review time") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("review time must be UTC")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status, payload_sha256, snapshot_sha256 FROM suggestions WHERE review_id = ?", (review_id,)
            ).fetchone()
            if current is None:
                raise ValueError("unknown review id")
            if ((expected_payload_sha256 is not None and current["payload_sha256"] != expected_payload_sha256)
                    or (expected_snapshot_sha256 is not None and current["snapshot_sha256"] != expected_snapshot_sha256)):
                raise ValueError("review suggestion changed before decision")
            previous = connection.execute(
                "SELECT verdict, reviewer, reason, decided_at FROM review_decisions WHERE review_id = ?", (review_id,)
            ).fetchone()
            decision = {"verdict": verdict, "reviewer": reviewer, "reason": reason, "decided_at": decided_at}
            if previous:
                if dict(previous) != decision:
                    raise ValueError("conflicting review decision")
                return {"review_id": review_id, "status": previous["verdict"]}
            if current["status"] != "pending_review":
                raise ValueError("conflicting review state")
            if verdict == "approved" and connection.execute(
                "SELECT 1 FROM suggestions s JOIN suggestions prior ON prior.tender_id = s.tender_id "
                "WHERE s.review_id = ? AND prior.review_id != s.review_id AND prior.status = 'approved' LIMIT 1",
                (review_id,),
            ).fetchone():
                raise ValueError("tender already approved; explicit version change required")
            connection.execute("INSERT INTO review_decisions (review_id, verdict, reviewer, reason, decided_at) "
                               "VALUES (?, ?, ?, ?, ?)", (review_id, verdict, reviewer, reason, decided_at))
            connection.execute("UPDATE suggestions SET status = ? WHERE review_id = ? AND status = 'pending_review'", (verdict, review_id))
            return {"review_id": review_id, "status": verdict}

    def get_review(self, review_id: str) -> dict[str, Any]:
        if not isinstance(review_id, str) or len(review_id) != 64 or any(c not in "0123456789abcdef" for c in review_id):
            raise ValueError("invalid review id")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT payload, payload_sha256, status FROM suggestions WHERE review_id = ?", (review_id,)).fetchone()
            if row is None:
                raise ValueError("unknown review id")
            if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise ValueError("review payload checksum mismatch")
            decision = connection.execute(
                "SELECT verdict, reviewer, reason, decided_at FROM review_decisions WHERE review_id = ?", (review_id,)
            ).fetchone()
        return {"review_id": review_id, "status": row["status"], "suggestion": json.loads(row["payload"]),
                "payload_sha256": row["payload_sha256"], "decision": dict(decision) if decision else None}

    def list_approved(self, limit: int = 20, *, snapshot_sha256: str,
                      reviewer_prefix: str | None = "telegram:user:",
                      reviewer_user_ids: set[int] | None = None,
                      undelivered_chat_id: int | None = None) -> list[dict[str, Any]]:
        """Read approved leads; public view requires allowlisted Telegram decisions."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid review limit")
        if not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64 or any(c not in "0123456789abcdef" for c in snapshot_sha256):
            raise ValueError("invalid snapshot hash")
        if reviewer_prefix is not None and reviewer_prefix != "telegram:user:":
            raise ValueError("invalid reviewer trust policy")
        if reviewer_user_ids is not None and (not isinstance(reviewer_user_ids, set)
                                              or any(type(value) is not int or value <= 0 for value in reviewer_user_ids)):
            raise ValueError("invalid reviewer allowlist")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        if undelivered_chat_id is not None:
            if type(undelivered_chat_id) is not int or undelivered_chat_id <= 0:
                raise ValueError("invalid private Telegram chat ID")
            # Add the delivery table to pre-existing queues before the read-only feed.
            self._ensure_schema()
        reviewer_filter = "AND d.reviewer GLOB 'telegram:user:[0-9]*' " if reviewer_prefix else ""
        params: list[Any] = [snapshot_sha256]
        if reviewer_user_ids is not None:
            if not reviewer_user_ids:
                return []
            reviewer_filter += "AND d.reviewer IN (" + ",".join("?" for _ in reviewer_user_ids) + ") "
            params.extend(f"telegram:user:{value}" for value in sorted(reviewer_user_ids))
        delivery_filter = ""
        if undelivered_chat_id is not None:
            delivery_filter = ("AND NOT EXISTS (SELECT 1 FROM telegram_delivery t "
                               "WHERE t.chat_id = ? AND t.tender_id = s.tender_id) ")
            params.append(undelivered_chat_id)
        params.append(limit)
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT s.review_id, s.payload, s.payload_sha256, d.verdict, d.reviewer, d.reason, d.decided_at "
                "FROM suggestions AS s JOIN review_decisions AS d ON d.review_id = s.review_id "
                "WHERE s.status = 'approved' AND d.verdict = 'approved' AND s.snapshot_sha256 = ? "
                "AND json_valid(s.payload) AND json_extract(s.payload, '$.decision') = 'lead' "
                + reviewer_filter + delivery_filter + "ORDER BY julianday(d.decided_at) DESC, s.review_id LIMIT ?", params
            ).fetchall()
        records = []
        for row in rows:
            if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise ValueError("review payload checksum mismatch")
            suggestion = json.loads(row["payload"])
            if suggestion.get("decision") == "lead":
                records.append({"review_id": row["review_id"], "suggestion": suggestion,
                                "decision": {key: row[key] for key in ("verdict", "reviewer", "reason", "decided_at")}})
        return records

    def has_pending_review(self, snapshot_sha256: str) -> bool:
        """Guard rotation on every pending suggestion, including hidden duplicates."""
        if (not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64
                or any(c not in "0123456789abcdef" for c in snapshot_sha256)):
            raise ValueError("invalid snapshot hash")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute(
                "SELECT payload, payload_sha256 FROM suggestions "
                "WHERE snapshot_sha256 = ? AND status = 'pending_review'", (snapshot_sha256,)
            ).fetchall()
        for payload, digest in rows:
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
                raise ValueError("review payload checksum mismatch")
        return bool(rows)

    def has_undelivered_approved(self, snapshot_sha256: str, chat_ids: set[int], *,
                                 reviewer_user_ids: set[int] | None = None) -> bool:
        """Whether a verified lead is still automatically deliverable to any private chat.

        A durable claim (including uncertain network outcomes) is excluded: it
        requires operator reconciliation, not an automatic resend or endless gate.
        This is an operator rotation check, not a substitute for bundle verification.
        """
        if (not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64
                or any(c not in "0123456789abcdef" for c in snapshot_sha256)):
            raise ValueError("invalid snapshot hash")
        if (not isinstance(chat_ids, set) or any(type(value) is not int or value <= 0 for value in chat_ids)):
            raise ValueError("invalid private chat IDs")
        if (reviewer_user_ids is not None and
                (not isinstance(reviewer_user_ids, set) or
                 any(type(value) is not int or value <= 0 for value in reviewer_user_ids))):
            raise ValueError("invalid reviewer allowlist")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        if not chat_ids or reviewer_user_ids == set():
            return False
        self._ensure_schema()  # Older queues may not yet contain telegram_delivery.
        reviewer_filter = ""
        params: list[Any] = [snapshot_sha256]
        if reviewer_user_ids is not None:
            reviewer_filter = "AND d.reviewer IN (" + ",".join("?" for _ in reviewer_user_ids) + ") "
            params.extend(f"telegram:user:{value}" for value in sorted(reviewer_user_ids))
        params.append(json.dumps(sorted(chat_ids)))
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            rows = connection.execute(
                "SELECT s.payload, s.payload_sha256 FROM suggestions s "
                "JOIN review_decisions d ON d.review_id = s.review_id "
                "WHERE s.snapshot_sha256 = ? AND s.status = 'approved' AND d.verdict = 'approved' "
                "AND d.reviewer GLOB 'telegram:user:[0-9]*' " + reviewer_filter +
                "AND json_valid(s.payload) AND json_extract(s.payload, '$.decision') = 'lead' "
                "AND EXISTS (SELECT 1 FROM json_each(?) chats "
                "WHERE NOT EXISTS (SELECT 1 FROM telegram_delivery t "
                "WHERE t.chat_id = chats.value AND t.tender_id = s.tender_id))",
                params,
            ).fetchall()
        for payload, digest in rows:
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
                raise ValueError("review payload checksum mismatch")
        return bool(rows)

    def claim_telegram_delivery(self, *, chat_id: int, review_id: str, tender_id: str) -> bool:
        """Atomically reserve one card per private chat and tender, including ambiguous sends.

        A claim is durable before network I/O. False does not mean Telegram delivered:
        a prior process may have crashed after claiming and before sending.
        """
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError("invalid private Telegram chat ID")
        if not isinstance(review_id, str) or len(review_id) != 64 or any(c not in "0123456789abcdef" for c in review_id):
            raise ValueError("invalid review id")
        if not isinstance(tender_id, str) or not tender_id or len(tender_id) > 200:
            raise ValueError("invalid tender id")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        self._ensure_schema()
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            approved = connection.execute(
                "SELECT s.payload, s.payload_sha256 FROM suggestions s "
                "JOIN review_decisions d ON d.review_id = s.review_id "
                "WHERE s.review_id = ? AND s.tender_id = ? AND s.status = 'approved' AND d.verdict = 'approved' "
                "AND json_valid(s.payload) AND json_extract(s.payload, '$.decision') = 'lead'",
                (review_id, tender_id),
            ).fetchone()
            if approved is None:
                raise ValueError("lead is not approved for this tender")
            if hashlib.sha256(approved["payload"].encode("utf-8")).hexdigest() != approved["payload_sha256"]:
                raise ValueError("review payload checksum mismatch")
            return bool(connection.execute(
                "INSERT OR IGNORE INTO telegram_delivery (chat_id, tender_id, review_id, state) "
                "VALUES (?, ?, ?, 'claimed')", (chat_id, tender_id, review_id),
            ).rowcount)

    def record_telegram_delivery(self, *, chat_id: int, review_id: str, message_id: int) -> None:
        """Record a successful API return; a timeout remains claimed, not a retry signal."""
        if (type(chat_id) is not int or chat_id <= 0 or not isinstance(review_id, str)
                or len(review_id) != 64 or any(c not in "0123456789abcdef" for c in review_id)):
            raise ValueError("invalid Telegram delivery identity")
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("Telegram API reply did not include a valid message ID")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        with closing(self._connection()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT message_id, state FROM telegram_delivery WHERE chat_id = ? AND review_id = ?",
                (chat_id, review_id),
            ).fetchone()
            if existing is None or (existing["state"] == "delivered" and existing["message_id"] != message_id):
                raise ValueError("delivery claim not found or conflicting receipt")
            if existing["state"] == "claimed":
                connection.execute(
                    "UPDATE telegram_delivery SET state = 'delivered', message_id = ? "
                    "WHERE chat_id = ? AND review_id = ? AND state = 'claimed'",
                    (message_id, chat_id, review_id),
                )

    def telegram_delivery_status(self, *, chat_id: int, tender_id: str) -> str | None:
        """Inspect durable delivery state; claimed does not prove a Telegram send."""
        if type(chat_id) is not int or chat_id <= 0 or not isinstance(tender_id, str) or not tender_id or len(tender_id) > 200:
            raise ValueError("invalid Telegram delivery identity")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            row = connection.execute("SELECT state FROM telegram_delivery WHERE chat_id = ? AND tender_id = ?",
                                     (chat_id, tender_id)).fetchone()
        return row[0] if row else None

    def list_pending(self, limit: int = 20, *, snapshot_sha256: str | None = None,
                     leads_only: bool = False, offset: int = 0) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("invalid review limit")
        if type(offset) is not int or not 0 <= offset <= 500:
            raise ValueError("invalid review offset")
        if snapshot_sha256 is not None and (not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64
                                            or any(c not in "0123456789abcdef" for c in snapshot_sha256)):
            raise ValueError("invalid snapshot hash")
        if not isinstance(leads_only, bool):
            raise ValueError("invalid lead filter")
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise ValueError("review queue does not exist or is not a regular file")
        query = ("SELECT review_id, payload, payload_sha256 FROM suggestions AS s "
                 "WHERE s.status = 'pending_review' ")
        params: list[Any] = []
        if snapshot_sha256 is not None:
            query += "AND snapshot_sha256 = ? "
            params.append(snapshot_sha256)
        if leads_only:
            query += "AND json_valid(payload) AND json_extract(payload, '$.decision') = 'lead' "
        query += "ORDER BY created_at, review_id LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        with closing(sqlite3.connect(self.database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise ValueError("review payload checksum mismatch")
            result.append({"review_id": row["review_id"], "suggestion": json.loads(row["payload"])})
        return result
