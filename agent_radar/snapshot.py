"""Read-only, bounded, versioned tender snapshots for the agent."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "agent-radar-snapshot-v1"
MAX_SNAPSHOT_BYTES = 2_000_000
MAX_TENDERS = 500
MAX_CANDIDATES_PER_PAGE = 100
MAX_DOCS_PER_TENDER = 20
MAX_DOC_CHARS = 200_000
MAX_TEXT_CHUNK = 4_000
MAX_TOTAL_TEXT_CHARS = 1_000_000
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_:.\-]{0,199}$")
_CARD_FIELDS = {"id", "title", "customer_name", "source_url", "description", "legacy_score", "documents", "document_state", "goods_state", "goods_count"}
_DOCUMENT_STATES = {"unknown", "not_retrieved", "extraction_failed", "retrieved_partial", "available", "source_reports_none"}
_GOODS_STATES = {"complete", "source_reports_none", "unknown", "omitted_oversized"}
_DOCUMENT_FIELDS = {"id", "title", "text"}


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SnapshotStore:
    """Materialized source data. No network, DB, arbitrary path or write operations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> tuple[list[dict[str, Any]], str]:
        if not self.path.is_file() or self.path.is_symlink():
            raise ValueError("snapshot must be a regular file")
        with self.path.open("rb") as snapshot_file:
            raw = snapshot_file.read(MAX_SNAPSHOT_BYTES + 1)
            if len(raw) > MAX_SNAPSHOT_BYTES or snapshot_file.read(1):
                raise ValueError("snapshot is too large")
        text = raw.decode("utf-8")
        payload = json.loads(text)
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported snapshot format")
        rows = payload.get("tenders")
        if not isinstance(rows, list) or len(rows) > MAX_TENDERS or set(payload) != {"schema_version", "tenders"}:
            raise ValueError("invalid tender list or unknown snapshot fields")
        seen: set[str] = set()
        total_text_chars = 0
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise ValueError("invalid tender")
            if not _IDENTIFIER.fullmatch(row["id"]) or row["id"] in seen or set(row) - _CARD_FIELDS:
                raise ValueError("duplicate, unknown fields or invalid tender id")
            seen.add(row["id"])
            if not isinstance(row.get("title"), str) or not row["title"] or len(row["title"]) > 2_000:
                raise ValueError("invalid tender title")
            for optional_field in ("customer_name", "source_url", "description", "legacy_score"):
                value = row.get(optional_field)
                if value is not None and (not isinstance(value, str) or len(value) > 10_000):
                    raise ValueError(f"invalid tender {optional_field}")
            documents = row.get("documents")
            if not isinstance(documents, list) or len(documents) > MAX_DOCS_PER_TENDER:
                raise ValueError("invalid document list")
            document_state = row.get("document_state", "unknown")
            if not isinstance(document_state, str) or document_state not in _DOCUMENT_STATES:
                raise ValueError("invalid document state")
            if "goods_state" in row and row["goods_state"] not in _GOODS_STATES:
                raise ValueError("invalid goods state")
            if "goods_count" in row and (type(row["goods_count"]) is not int or not 0 <= row["goods_count"] <= 1500):
                raise ValueError("invalid goods count")
            if row.get("goods_state") == "omitted_oversized" and not row.get("goods_count"):
                raise ValueError("omitted goods require a source count")
            if document_state == "available" and not documents:
                raise ValueError("available state requires extracted documents")
            if document_state == "source_reports_none" and documents:
                raise ValueError("source_reports_none state cannot have documents")
            doc_ids: set[str] = set()
            for document in documents:
                if not isinstance(document, dict) or not isinstance(document.get("id"), str):
                    raise ValueError("invalid document")
                if not _IDENTIFIER.fullmatch(document["id"]) or document["id"] in doc_ids or set(document) - _DOCUMENT_FIELDS:
                    raise ValueError("duplicate, unknown fields or invalid document id")
                doc_ids.add(document["id"])
                if not isinstance(document.get("text"), str) or len(document["text"]) > MAX_DOC_CHARS:
                    raise ValueError("invalid document text")
                if not isinstance(document.get("title"), str) or len(document["title"]) > 2_000:
                    raise ValueError("invalid document title")
                total_text_chars += len(document["text"])
                if total_text_chars > MAX_TOTAL_TEXT_CHARS:
                    raise ValueError("snapshot contains too much document text")
        return rows, _fingerprint(text)

    def list_candidates(self, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES_PER_PAGE:
            raise ValueError("invalid page limit")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid page offset")
        rows, snapshot_hash = self._load()
        if offset > len(rows):
            raise ValueError("page offset out of range")
        page = rows[offset : offset + limit]
        return {
            "snapshot_sha256": snapshot_hash,
            "candidates": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "customer_name": row.get("customer_name"),
                    "legacy_score": row.get("legacy_score"),
                    "document_count": len(row["documents"]),
                    "document_state": row.get("document_state", "unknown"),
                }
                for row in page
            ],
            "next_offset": offset + len(page) if offset + len(page) < len(rows) else None,
        }

    def _tender(self, tender_id: str, snapshot_sha256: str) -> dict[str, Any]:
        rows, actual_hash = self._load()
        if actual_hash != snapshot_sha256:
            raise ValueError("snapshot changed; restart analysis")
        for row in rows:
            if row["id"] == tender_id:
                return row
        raise ValueError("unknown tender")

    def get_tender(self, *, tender_id: str, snapshot_sha256: str) -> dict[str, Any]:
        row = self._tender(tender_id, snapshot_sha256)
        return {
            "snapshot_sha256": snapshot_sha256,
            "id": row["id"],
            "title": row["title"],
            "customer_name": row.get("customer_name"),
            "source_url": row.get("source_url"),
            "description": row.get("description"),
            "legacy_score": row.get("legacy_score"),
            "document_state": row.get("document_state", "unknown"),
            "goods_state": row.get("goods_state", "unknown"),
            "goods_count": row.get("goods_count"),
        }

    def list_documents(self, *, tender_id: str, snapshot_sha256: str) -> dict[str, Any]:
        row = self._tender(tender_id, snapshot_sha256)
        return {
            "snapshot_sha256": snapshot_sha256,
            "tender_id": tender_id,
            "document_state": row.get("document_state", "unknown"),
            "documents": [
                {"id": doc["id"], "title": doc["title"], "content_sha256": _fingerprint(doc["text"]), "text_length": len(doc["text"])}
                for doc in row["documents"]
            ],
        }

    def read_document_chunk(
        self, *, tender_id: str, document_id: str, snapshot_sha256: str, offset: int = 0, limit: int = MAX_TEXT_CHUNK
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TEXT_CHUNK:
            raise ValueError("invalid chunk limit")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid chunk offset")
        row = self._tender(tender_id, snapshot_sha256)
        for doc in row["documents"]:
            if doc["id"] == document_id:
                text = doc["text"]
                if offset > len(text):
                    raise ValueError("chunk offset out of range")
                end = min(offset + limit, len(text))
                return {
                    "snapshot_sha256": snapshot_sha256,
                    "tender_id": tender_id,
                    "document_id": document_id,
                    "content_sha256": _fingerprint(text),
                    "offset": offset,
                    "text": text[offset:end],
                    "next_offset": end if end < len(text) else None,
                }
        raise ValueError("unknown document")
