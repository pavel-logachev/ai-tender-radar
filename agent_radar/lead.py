"""Strict offline evidence checks before an agent suggestion may be reviewed."""

from __future__ import annotations

from typing import Any

from agent_radar.snapshot import MAX_TEXT_CHUNK, SnapshotStore

LEAD_SCHEMA_VERSION = "agent-radar-lead-v1"
_ALLOWED = {
    "schema_version", "tender_id", "snapshot_sha256", "decision", "customer_opportunity",
    "contact_reason", "suggested_roles", "questions", "facts", "hypotheses", "gaps", "external_sources",
}


def _text(value: Any, *, label: str, max_length: int = 2000, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > max_length or (required and not value.strip()):
        raise ValueError(f"invalid {label}")
    return value.strip()


def _strings(value: Any, *, label: str, max_items: int = 8) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"invalid {label}")
    return [_text(item, label=label, max_length=500, required=True) for item in value]


def validate_lead(payload: dict[str, Any], store: SnapshotStore) -> dict[str, Any]:
    """Anchor suggested claims to local quotes; truth and source rights require review."""
    if not isinstance(payload, dict) or set(payload) != _ALLOWED:
        raise ValueError("invalid lead fields")
    if payload["schema_version"] != LEAD_SCHEMA_VERSION:
        raise ValueError("unsupported lead schema")
    tender_id = _text(payload["tender_id"], label="tender id", max_length=200, required=True)
    snapshot_hash = _text(payload["snapshot_sha256"], label="snapshot hash", max_length=64, required=True)
    if len(snapshot_hash) != 64 or any(char not in "0123456789abcdef" for char in snapshot_hash):
        raise ValueError("invalid snapshot hash")
    card = store.get_tender(tender_id=tender_id, snapshot_sha256=snapshot_hash)
    if not isinstance(payload["decision"], str) or payload["decision"] not in ("lead", "review", "pass"):
        raise ValueError("invalid decision")
    if card.get("goods_state") == "omitted_oversized" and payload["decision"] != "review":
        raise ValueError("oversized goods omitted: only review is allowed")
    opportunity = _text(payload["customer_opportunity"], label="customer opportunity", required=payload["decision"] == "lead")
    reason = _text(payload["contact_reason"], label="contact reason", required=payload["decision"] == "lead")
    roles = _strings(payload["suggested_roles"], label="suggested roles")
    questions = _strings(payload["questions"], label="questions")
    hypotheses = _strings(payload["hypotheses"], label="hypotheses")
    gaps = _strings(payload["gaps"], label="gaps")
    sources = payload["external_sources"]
    if not isinstance(sources, list) or sources:
        raise ValueError("external research is not supported in the offline shadow contract")
    facts = payload["facts"]
    if not isinstance(facts, list) or len(facts) > 12 or (payload["decision"] == "lead" and not facts):
        raise ValueError("invalid facts")
    document_ids = {doc["id"] for doc in store.list_documents(tender_id=tender_id, snapshot_sha256=snapshot_hash)["documents"]}
    grounded_document = False
    grounded_description = False
    description = card.get("description") or ""
    card_text = "\n".join(str(card.get(key) or "") for key in ("title", "customer_name", "description"))
    description_start = len(str(card.get("title") or "")) + len(str(card.get("customer_name") or "")) + 2
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"claim", "evidence"}:
            raise ValueError("invalid fact")
        _text(fact["claim"], label="fact claim", max_length=500, required=True)
        evidence = fact["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {"document_id", "offset", "quote"}:
            raise ValueError("invalid evidence")
        quote = _text(evidence["quote"], label="quote", max_length=MAX_TEXT_CHUNK, required=True)
        if quote != evidence["quote"]:
            raise ValueError("quote must be verbatim without padding")
        offset = evidence["offset"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid quote offset")
        if not isinstance(evidence["document_id"], str):
            raise ValueError("invalid evidence document id")
        if evidence["document_id"] == "card":
            if card_text[offset : offset + len(quote)] != quote:
                raise ValueError("card quote does not match snapshot")
            # Artificial newlines separate fields; one quote must not straddle them.
            if "\n" in quote:
                raise ValueError("card quote must belong to a single field")
            if description.strip() and description_start <= offset and offset + len(quote) <= description_start + len(description):
                grounded_description = True
        elif evidence["document_id"] in document_ids:
            chunk = store.read_document_chunk(tender_id=tender_id, document_id=evidence["document_id"], snapshot_sha256=snapshot_hash, offset=offset, limit=len(quote))
            if chunk["text"] != quote:
                raise ValueError("document quote does not match snapshot")
            grounded_document = True
        else:
            raise ValueError("unknown quote source")
    if payload["decision"] == "lead":
        if document_ids and not grounded_document:
            raise ValueError("lead requires document evidence when extracted documents are available")
        if not document_ids and not grounded_description:
            raise ValueError("lead requires description evidence when extracted documents are unavailable")
    # Byte-exact local quote anchors are not proof of the source's authenticity or of claim truth.
    return {
        "schema_version": LEAD_SCHEMA_VERSION,
        "tender_id": tender_id,
        "snapshot_sha256": snapshot_hash,
        "decision": payload["decision"],
        "customer_opportunity": opportunity,
        "contact_reason": reason,
        "suggested_roles": roles,
        "questions": questions,
        "facts": facts,
        "hypotheses": hypotheses,
        "gaps": gaps,
        "external_sources": sources,
    }
