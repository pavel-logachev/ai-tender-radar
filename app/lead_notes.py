import logging
from datetime import datetime, timezone
from typing import Iterable

import psycopg

from app.config import settings


logger = logging.getLogger(__name__)

LEAD_NOTE_TEXT_LIMIT = 2000


class LeadNoteTooLong(ValueError):
    pass


def normalize_lead_note_text(text: str | None) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) > LEAD_NOTE_TEXT_LIMIT:
        raise LeadNoteTooLong(f"Lead note is longer than {LEAD_NOTE_TEXT_LIMIT} characters")
    return normalized


def ensure_lead_notes_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_notes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tender_id UUID NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
            user_name TEXT,
            chat_id BIGINT,
            user_id BIGINT,
            note_text TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        );
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lead_notes_tender_id_created_at
        ON lead_notes(tender_id, created_at DESC);
        """
    )


def save_lead_note(
    tender_id: str,
    note_text: str,
    user_name: str | None,
    *,
    chat_id: int | None = None,
    user_id: int | None = None,
) -> None:
    note = normalize_lead_note_text(note_text)
    if not note:
        raise ValueError("Lead note is empty")

    created_at = datetime.now(timezone.utc)
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            ensure_lead_notes_table(cur)
            cur.execute(
                """
                INSERT INTO lead_notes (
                    tender_id,
                    user_name,
                    chat_id,
                    user_id,
                    note_text,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (tender_id, user_name, chat_id, user_id, note, created_at),
            )
            conn.commit()


def lead_notes_table_exists(cur) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'lead_notes'
        );
        """
    )
    row = cur.fetchone()
    return bool(row and row[0])


def fetch_latest_lead_notes_for_tenders(tender_ids: Iterable[str | None]) -> dict[str, str]:
    ids: list[str] = []
    seen: set[str] = set()
    for value in tender_ids:
        tender_id = str(value or "").strip()
        if tender_id and tender_id not in seen:
            ids.append(tender_id)
            seen.add(tender_id)

    if not ids:
        return {}

    placeholders = ", ".join("%s" for _ in ids)
    try:
        with psycopg.connect(settings.database_url) as conn:
            with conn.cursor() as cur:
                if not lead_notes_table_exists(cur):
                    return {}

                cur.execute(
                    f"""
                    SELECT DISTINCT ON (tender_id)
                        tender_id::text,
                        note_text
                    FROM lead_notes
                    WHERE deleted_at IS NULL
                      AND tender_id IN ({placeholders})
                    ORDER BY tender_id, created_at DESC, id DESC;
                    """,
                    tuple(ids),
                )
                return {
                    str(row[0]): str(row[1] or "")
                    for row in cur.fetchall()
                    if row and row[1]
                }
    except Exception as exc:
        logger.warning("Lead notes lookup failed; continuing without notes: %s", exc)
        return {}
