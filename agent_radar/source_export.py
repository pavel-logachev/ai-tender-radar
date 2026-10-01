"""Offline, explicit-completeness materialization of operator-owned source exports.

This module never calls a provider API and is not a substitute for a licensed
source connector or a trustworthy extraction/provenance chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from agent_radar.snapshot import MAX_SNAPSHOT_BYTES, MAX_TENDERS, SnapshotStore

MAX_SOURCE_EXPORT_BYTES = 3_000_000
MAX_DETAIL_COUNT = 500
MAX_DOCUMENTS_PER_ORDER = 20
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2_147_483_647:
        raise ValueError(f"invalid {label}")
    return value


def _dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}")
    return value


def _list(value: Any, label: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"invalid {label}")
    return value


def _text(value: Any, label: str, limit: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {label}")
    return value


def build_snapshot(source: dict[str, Any], *, max_search_results: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Require full operator attestation; never silently truncate the declared search."""
    source = _dict(source, "source export")
    if set(source) != {"schema_version", "source", "search", "details"} or source["schema_version"] != "agent-radar-source-export-v1" or source["source"] != "example-source":
        raise ValueError("unsupported source export")
    if isinstance(max_search_results, bool) or not isinstance(max_search_results, int) or not 1 <= max_search_results <= MAX_TENDERS:
        raise ValueError("invalid search result budget")
    search = _dict(source["search"], "search declaration")
    if set(search) != {"publish_date_from", "publish_date_to", "query", "operator_declared_complete", "results"}:
        raise ValueError("invalid search declaration")
    for key in ("publish_date_from", "publish_date_to"):
        if not isinstance(search[key], str) or not _DATE.fullmatch(search[key]):
            raise ValueError("invalid publication date window")
        try:
            date.fromisoformat(search[key])
        except ValueError as exc:
            raise ValueError("invalid publication date window") from exc
    if search["publish_date_from"] > search["publish_date_to"]:
        raise ValueError("inverted publication date window")
    if not isinstance(search["query"], str) or len(search["query"]) > 300:
        raise ValueError("invalid search query")
    if search["operator_declared_complete"] is not True:
        raise ValueError("source search incomplete or not attested")
    results = _list(search["results"], "search results", max_search_results)
    if len(results) >= max_search_results:
        raise ValueError("search result budget saturated; coverage unknown")
    ids: set[int] = set()
    for row in results:
        row = _dict(row, "search row")
        order_id = _positive_id(row.get("orderId"), "search order id")
        _text(row.get("name"), "search order title")
        if order_id in ids:
            raise ValueError("duplicate search order id")
        ids.add(order_id)

    details: dict[int, dict[str, Any]] = {}
    for item in _list(source["details"], "order details", MAX_DETAIL_COUNT):
        item = _dict(item, "order detail wrapper")
        if set(item) not in ({"order_id", "order", "extracted_documents"},
                             {"order_id", "order", "extracted_documents", "failed_document_ids"}):
            raise ValueError("invalid order detail wrapper")
        order_id = _positive_id(item["order_id"], "detail order id")
        if order_id not in ids or order_id in details:
            raise ValueError("unrequested or duplicate order detail")
        details[order_id] = item
    if ids != set(details):
        raise ValueError("missing detail for one or more search results")

    tenders: list[dict[str, Any]] = []
    gaps: list[str] = []
    for order_id in sorted(ids):
        item = details[order_id]
        order = _dict(item["order"], "full order")
        title = _text(order.get("name"), "order title")
        raw_documents = order.get("documents")
        if raw_documents is None:
            raw_documents = []
            state_if_no_docs = "unknown"
        else:
            _list(raw_documents, "order documents", MAX_DOCUMENTS_PER_ORDER)
            state_if_no_docs = "source_reports_none"
        wanted_docs: dict[int, str] = {}
        for doc in raw_documents:
            doc = _dict(doc, "source document")
            doc_id = _positive_id(doc.get("id"), "source document id")
            if doc_id in wanted_docs:
                raise ValueError("duplicate document id")
            wanted_docs[doc_id] = _text(doc.get("title"), "document title")
        extracted: dict[int, str] = {}
        for doc in _list(item["extracted_documents"], "extracted documents", MAX_DOCUMENTS_PER_ORDER):
            doc = _dict(doc, "extracted document")
            if set(doc) != {"document_id", "text"}:
                raise ValueError("invalid extracted document shape")
            doc_id = _positive_id(doc["document_id"], "extracted document id")
            if doc_id not in wanted_docs or doc_id in extracted:
                raise ValueError("unrequested or duplicate extracted document")
            extracted[doc_id] = _text(doc["text"], "extracted document text", 200_000)
        failed_ids: set[int] = set()
        for doc_id in _list(item.get("failed_document_ids", []), "failed document ids", MAX_DOCUMENTS_PER_ORDER):
            doc_id = _positive_id(doc_id, "failed document id")
            if doc_id not in wanted_docs or doc_id in extracted or doc_id in failed_ids:
                raise ValueError("unrequested or duplicate failed document id")
            failed_ids.add(doc_id)
        documents = [{"id": f"z360:{doc_id}", "title": wanted_docs[doc_id], "text": extracted[doc_id]}
                     for doc_id in sorted(extracted)]
        for doc_id in sorted(set(wanted_docs) - set(extracted)):
            gaps.append(f"example-source:{order_id}:{doc_id}")
        state = ("available" if len(extracted) == len(wanted_docs) else
                 "retrieved_partial" if extracted else
                 "extraction_failed" if failed_ids else "not_retrieved") if wanted_docs else state_if_no_docs
        tender: dict[str, Any] = {
            "id": f"example-source:{order_id}", "title": title, "documents": documents, "document_state": state
        }
        customer = order.get("placerOrganizationName")
        if isinstance(customer, str) and customer.strip():
            tender["customer_name"] = customer
        # An explicit, bounded excerpt of two named order-detail fields supplies
        # quoteable card evidence even when no document text was retrieved. Never
        # serialize contact fields, arbitrary descriptions or whole provider DTOs.
        description_parts = []
        for field, label in (("deliveryTerm", "Срок поставки"), ("deliveryPlace", "Место поставки")):
            value = order.get(field)
            if isinstance(value, str):
                value = value.strip()
                if value and len(value) <= 2_000 and not any(ord(char) < 32 or ord(char) == 127 for char in value):
                    description_parts.append(f"{label}: {value}")
        if description_parts:
            tender["description"] = "\n".join(description_parts)
        tenders.append(tender)
    snapshot = {"schema_version": "agent-radar-snapshot-v1", "tenders": tenders}
    manifest = {"source": "example-source", "search": {key: search[key] for key in ("publish_date_from", "publish_date_to", "query")},
                "search_result_count": len(results), "document_gaps": gaps,
                "completeness": "operator_declared_only; not independently verified"}
    return snapshot, manifest


def _read_bounded_json(path: Path, *, max_bytes: int) -> tuple[dict[str, Any], str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("source export must be a regular file")
    with path.open("rb") as file:
        raw = file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("source export too large")
    return _dict(json.loads(raw.decode("utf-8")), "source export"), hashlib.sha256(raw).hexdigest()


def materialize_source_export(input_path: Path, snapshot_path: Path, manifest_path: Path, *, max_search_results: int) -> dict[str, Any]:
    """Create new files only; no external calls, overwrite, or silent truncation."""
    if snapshot_path == manifest_path or snapshot_path.exists() or manifest_path.exists():
        raise ValueError("snapshot or manifest already exists")
    raw, export_sha256 = _read_bounded_json(input_path, max_bytes=MAX_SOURCE_EXPORT_BYTES)
    snapshot, manifest = build_snapshot(raw, max_search_results=max_search_results)
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("materialized snapshot too large")
    # Validate the serialized form before publishing. Keep preflight temp local to destination.
    with tempfile.NamedTemporaryFile(mode="wb", prefix=".radar-validate-", dir=snapshot_path.parent, delete=False) as preflight:
        preflight.write(encoded)
        preflight_path = Path(preflight.name)
    try:
        hash_value = SnapshotStore(preflight_path).list_candidates(limit=1)["snapshot_sha256"]
        manifest["snapshot_sha256"] = hash_value
        manifest["source_export_sha256"] = export_sha256
        manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        # Publish snapshot LAST via same-directory atomic rename. The manifest
        # is a companion, not a proof of ingestion rights or a delivery receipt.
        published_snapshot = False
        manifest_owned = False
        try:
            # A private file at creation time, independent of the process umask.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(manifest_path, flags, 0o600)
            manifest_owned = True
            with os.fdopen(fd, "wb") as out:
                out.write(manifest_text.encode("utf-8"))
                out.flush()
                os.fsync(out.fileno())
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".radar-publish-", dir=snapshot_path.parent, delete=False) as staging:
                staging.write(encoded)
                staging.flush()
                os.fsync(staging.fileno())
                staging_path = Path(staging.name)
            try:
                if snapshot_path.exists():
                    raise ValueError("snapshot already exists")
                # A cooperating single-writer operator must choose unique output
                # names. Do not call this concurrent, multi-process publication.
                staging_path.rename(snapshot_path)
                published_snapshot = True
            finally:
                staging_path.unlink(missing_ok=True)
        finally:
            if manifest_owned and not published_snapshot:
                manifest_path.unlink(missing_ok=True)
    finally:
        preflight_path.unlink(missing_ok=True)
    return manifest
