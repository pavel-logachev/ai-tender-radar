from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import settings
from app.llm.factory import create_llm_client


PLANNER_ROLES = {
    "technical_requirements",
    "specification",
    "object_description",
    "documentation",
    "contract",
    "nmck",
    "application_form",
    "protocol",
    "other",
    "unknown",
}
PLANNER_CONFIDENCES = {"high", "medium", "low"}

TZ_ABBREV_RE = re.compile(r"(?<![а-яa-z0-9])тз(?![а-яa-z0-9])", re.IGNORECASE)
KD_ABBREV_RE = re.compile(r"(?<![а-яa-z0-9])кд(?![а-яa-z0-9])", re.IGNORECASE)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _compact(value: Any, *, limit: int) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _normalize(value: Any) -> str:
    return _text(value).casefold().replace("ё", "е")


def _alnum_compact(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", _normalize(value))


def document_id(doc: dict[str, Any]) -> str:
    for key in ("id", "document_id", "documentId", "external_document_id"):
        value = _text(doc.get(key)).strip()
        if value:
            return value
    return ""


def document_title(doc: dict[str, Any]) -> str:
    return (
        _text(
            doc.get("title")
            or doc.get("filename")
            or doc.get("fileName")
            or doc.get("name")
        ).strip()
        or "document"
    )


def document_extension(doc: dict[str, Any]) -> str:
    value = _text(doc.get("fileExtension") or doc.get("extension")).strip()
    if value:
        return value if value.startswith(".") else f".{value}"
    for key in ("filename", "fileName", "title", "name"):
        suffix = Path(_text(doc.get(key))).suffix
        if suffix:
            return suffix
    return ""


def document_source_host(doc: dict[str, Any]) -> str:
    for key in ("url", "downloadUrl", "documentUrl", "sourceUrl", "href", "link"):
        value = _text(doc.get(key)).strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.netloc:
            return parsed.netloc
        return value[:80]
    source = doc.get("source")
    if isinstance(source, dict):
        return document_source_host(source)
    return ""


def document_planner_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": document_id(doc),
        "title": document_title(doc),
        "fileExtension": document_extension(doc),
        "publishDate": doc.get("publishDate") or doc.get("publishedAt"),
        "source": document_source_host(doc),
    }


def _document_text(doc: dict[str, Any]) -> str:
    return " ".join(
        _text(doc.get(key))
        for key in (
            "title",
            "filename",
            "fileName",
            "name",
            "fileExtension",
            "extension",
        )
    )


def _has_explicit_technical_metadata(doc: dict[str, Any]) -> bool:
    text = _normalize(_document_text(doc))
    compact = _alnum_compact(text)
    if TZ_ABBREV_RE.search(text):
        return True
    if "техзад" in compact:
        return True
    if "техническ" in text and "задан" in text:
        return True
    if (
        "описан" in text
        and ("объект" in text or "предмет" in text)
        and "закуп" in text
    ):
        return True
    if "техническ" in text and "требован" in text:
        return True
    if "техническ" in text and "спецификац" in text:
        return True
    if "спецификац" in text and "договор" not in text and "контракт" not in text:
        return True
    if "характеристик" in text and ("оборудован" in text or "товар" in text):
        return True
    return False


def _has_procurement_document_metadata(doc: dict[str, Any]) -> bool:
    text = _normalize(_document_text(doc))
    return bool(
        KD_ABBREV_RE.search(text)
        or "конкурсн" in text and "документац" in text
        or "закупочн" in text and "документац" in text
        or "извещен" in text and "документац" in text
        or "документац" in text
        or "материал" in text and "закуп" in text
    )


def _has_price_metadata(doc: dict[str, Any]) -> bool:
    text = _normalize(_document_text(doc))
    return any(
        marker in text
        for marker in ("нмцк", "нмцд", "расчет", "расчёт", "обоснован")
    )


def _has_contract_metadata(doc: dict[str, Any]) -> bool:
    text = _normalize(_document_text(doc))
    return any(
        marker in text
        for marker in ("проект договор", "проект контракт", "договор", "контракт")
    )


def _has_application_metadata(doc: dict[str, Any]) -> bool:
    text = _normalize(_document_text(doc))
    return any(marker in text for marker in ("форма заяв", "заявк", "требован"))


def _has_protocol_metadata(doc: dict[str, Any]) -> bool:
    return "протокол" in _normalize(_document_text(doc))


def infer_expected_role(
    doc: dict[str, Any],
    *,
    all_docs: list[dict[str, Any]] | None = None,
) -> str:
    text = _normalize(_document_text(doc))
    if _has_explicit_technical_metadata(doc):
        if "спецификац" in text or "характеристик" in text:
            return "specification"
        if "описан" in text:
            return "object_description"
        return "technical_requirements"
    if _has_price_metadata(doc):
        return "nmck"
    if _has_contract_metadata(doc):
        return "contract"
    if _has_protocol_metadata(doc):
        return "protocol"
    if _has_application_metadata(doc):
        return "application_form"
    if _has_procurement_document_metadata(doc):
        return "documentation"
    return "unknown"


def is_high_value_technical_metadata(
    doc: dict[str, Any],
    *,
    all_docs: list[dict[str, Any]] | None = None,
) -> bool:
    if _has_explicit_technical_metadata(doc):
        return True
    if (
        all_docs is not None
        and len(all_docs) == 1
        and _has_procurement_document_metadata(doc)
    ):
        return True
    return False


def _tender_payload(tender: dict[str, Any] | None) -> dict[str, Any]:
    tender = tender or {}
    raw = tender.get("raw") if isinstance(tender.get("raw"), dict) else {}
    full = raw.get("full") if isinstance(raw.get("full"), dict) else {}
    placer = (
        full.get("placerOrganization")
        if isinstance(full.get("placerOrganization"), dict)
        else {}
    )
    result = tender.get("result") if isinstance(tender.get("result"), dict) else {}
    return {
        "title": tender.get("title") or full.get("orderName") or full.get("purchaseName"),
        "customer": (
            tender.get("customer_name")
            or tender.get("customer")
            or placer.get("fullName")
            or placer.get("shortName")
        ),
        "price": (
            tender.get("initial_price")
            or tender.get("price")
            or full.get("initialPrice")
        ),
        "category": (
            tender.get("category")
            or tender.get("target_category")
            or tender.get("business_category")
            or tender.get("detected_category")
            or tender.get("_llm_category")
            or result.get("category")
            or result.get("detected_category")
        ),
        "lead_signal": result.get("summary") or result.get("why_relevant"),
    }


def build_document_download_planner_prompt(
    tender: dict[str, Any] | None,
    docs: list[dict[str, Any]],
) -> str:
    metadata = [document_planner_metadata(doc) for doc in docs[:40]]
    return f"""
Plan which tender documents should be downloaded first for an IT infrastructure
lead/full report. Rank by semantic likelihood that the document contains real
technical requirements or a specification.

Do not decide whether this tender is a lead. Do not summarize the tender, assess
business value, write risks, or propose manager questions. You only route
document metadata into a safe download order.

Treat these as high-value technical targets when metadata suggests them:
TZ, technical assignment, technical requirements, object/procurement subject
description, specification, appendix to technical assignment, appendix #1 with
characteristics, procurement documentation where a TZ may be embedded,
"notice and documentation" when it is the only/general document, and KD/materials
likely to contain a technical section.

Prefer this order:
1. technical requirements / object description / specification
2. KD or procurement documentation that may embed the technical section
3. NMCK/NMCD or price calculation
4. draft contract
5. application forms, protocols, notices and other low-value files

Return one strict JSON object only:
{{
  "download_first": [
    {{
      "document_id": "id",
      "expected_role": "technical_requirements|specification|object_description|documentation|contract|nmck|application_form|protocol|other|unknown",
      "priority": 0,
      "reason": "short reason"
    }}
  ],
  "download_if_budget_allows": [],
  "likely_irrelevant": [],
  "missing_expected_documents": ["short note"],
  "confidence": "high|medium|low"
}}

Tender:
{json.dumps(_tender_payload(tender), ensure_ascii=False, indent=2, default=str)}

Document metadata:
{json.dumps(metadata, ensure_ascii=False, indent=2, default=str)}
""".strip()


def _extract_json(text: str | None) -> dict[str, Any]:
    raw = _text(text).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end <= start:
            raise
        payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("planner response is not a JSON object")
    return payload


def _normalize_confidence(value: Any) -> str:
    confidence = _text(value).strip().lower()
    return confidence if confidence in PLANNER_CONFIDENCES else "low"


def _normalize_plan_items(
    value: Any,
    *,
    known_ids: set[str],
    default_priority: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_item in enumerate(value):
        if not isinstance(raw_item, dict):
            continue
        doc_id = _text(
            raw_item.get("document_id")
            or raw_item.get("id")
            or raw_item.get("documentId")
        ).strip()
        if not doc_id or doc_id not in known_ids or doc_id in seen:
            continue
        role = _text(raw_item.get("expected_role") or "unknown").strip()
        if role not in PLANNER_ROLES:
            role = "unknown"
        try:
            priority = int(raw_item.get("priority"))
        except (TypeError, ValueError):
            priority = default_priority + index
        priority = max(0, min(99, priority))
        items.append(
            {
                "document_id": doc_id,
                "expected_role": role,
                "priority": priority,
                "reason": _compact(raw_item.get("reason"), limit=300),
            }
        )
        seen.add(doc_id)

    return sorted(items, key=lambda item: item["priority"])


def normalize_document_download_plan(
    payload: dict[str, Any],
    docs: list[dict[str, Any]],
) -> dict[str, Any]:
    for key in (
        "download_first",
        "download_if_budget_allows",
        "likely_irrelevant",
        "missing_expected_documents",
        "confidence",
    ):
        if key not in payload:
            raise ValueError(f"planner response is missing {key}")

    known_ids = {document_id(doc) for doc in docs if document_id(doc)}
    download_first = _normalize_plan_items(
        payload.get("download_first"),
        known_ids=known_ids,
        default_priority=0,
    )
    download_if_budget_allows = _normalize_plan_items(
        payload.get("download_if_budget_allows"),
        known_ids=known_ids,
        default_priority=20,
    )
    likely_irrelevant = _normalize_plan_items(
        payload.get("likely_irrelevant"),
        known_ids=known_ids,
        default_priority=80,
    )
    if not download_first and not download_if_budget_allows:
        raise ValueError("planner response did not select any known document")

    missing_expected_documents = payload.get("missing_expected_documents") or []
    if not isinstance(missing_expected_documents, list):
        missing_expected_documents = []

    return {
        "download_first": download_first,
        "download_if_budget_allows": download_if_budget_allows,
        "likely_irrelevant": likely_irrelevant,
        "missing_expected_documents": [
            _compact(item, limit=220)
            for item in missing_expected_documents
            if _text(item).strip()
        ][:10],
        "confidence": _normalize_confidence(payload.get("confidence")),
        "planner_used": True,
        "planner_failed": False,
        "source": "llm",
    }


def failed_document_download_plan(error: Exception | str) -> dict[str, Any]:
    return {
        "download_first": [],
        "download_if_budget_allows": [],
        "likely_irrelevant": [],
        "missing_expected_documents": [],
        "confidence": "low",
        "planner_used": False,
        "planner_failed": True,
        "planner_error": _compact(error, limit=500),
        "source": "deterministic_fallback",
    }


def plan_document_download(
    tender: dict[str, Any] | None,
    docs: list[dict[str, Any]],
) -> dict[str, Any]:
    if not docs:
        return failed_document_download_plan("no documents")

    try:
        client = create_llm_client()
        json_mode = bool(
            getattr(settings, "llm_json_mode", False)
            and client.provider == "routerai"
        )
        response = client.generate_chat_completion(
            system_prompt=(
                "You are a tender document download planner. Return strict JSON only."
            ),
            user_prompt=build_document_download_planner_prompt(tender, docs),
            temperature=0.0,
            max_tokens=1600,
            json_mode=json_mode,
        )
        plan = normalize_document_download_plan(_extract_json(response.text), docs)
        if response.response_id:
            plan["response_id"] = response.response_id
        if response.usage:
            plan["usage"] = response.usage
        return plan
    except Exception as exc:
        return failed_document_download_plan(exc)


def plan_item_annotations(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    annotations: dict[str, dict[str, Any]] = {}
    for bucket in ("download_first", "download_if_budget_allows", "likely_irrelevant"):
        for item in plan.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            doc_id = _text(item.get("document_id")).strip()
            if not doc_id:
                continue
            annotations[doc_id] = {
                "planner_bucket": bucket,
                "expected_role": item.get("expected_role") or "unknown",
                "planner_priority": item.get("priority"),
                "planner_reason": item.get("reason") or "",
            }
    return annotations


def document_annotation(
    doc: dict[str, Any],
    *,
    all_docs: list[dict[str, Any]],
    plan_annotations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    doc_id = document_id(doc)
    annotation = dict((plan_annotations or {}).get(doc_id) or {})
    inferred_role = infer_expected_role(doc, all_docs=all_docs)
    if (
        annotation.get("expected_role") in (None, "", "unknown")
        and inferred_role != "unknown"
    ):
        annotation["expected_role"] = inferred_role
    annotation.setdefault("expected_role", inferred_role)
    annotation["high_value_technical"] = is_high_value_technical_metadata(
        doc,
        all_docs=all_docs,
    )
    return annotation


def _append_doc(
    ordered: list[dict[str, Any]],
    seen: set[str],
    doc: dict[str, Any],
) -> None:
    doc_id = document_id(doc)
    if not doc_id or doc_id in seen:
        return
    ordered.append(doc)
    seen.add(doc_id)


def merge_planner_document_order(
    docs: list[dict[str, Any]],
    plan: dict[str, Any],
    deterministic_order: list[dict[str, Any]],
    *,
    limit_docs: int,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id = {document_id(doc): doc for doc in docs if document_id(doc)}
    annotations = plan_item_annotations(plan)
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    explicit_high_value = [
        doc for doc in deterministic_order if _has_explicit_technical_metadata(doc)
    ]
    for doc in explicit_high_value:
        _append_doc(ordered, seen, doc)

    planned_ids: list[str] = []
    for bucket in ("download_first", "download_if_budget_allows"):
        for item in plan.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            doc_id = _text(item.get("document_id")).strip()
            if doc_id and doc_id in by_id and doc_id not in planned_ids:
                planned_ids.append(doc_id)

    for doc_id in planned_ids:
        _append_doc(ordered, seen, by_id[doc_id])

    high_value = [
        doc
        for doc in deterministic_order
        if is_high_value_technical_metadata(doc, all_docs=docs)
    ]
    for doc in high_value:
        _append_doc(ordered, seen, doc)

    for doc in deterministic_order:
        _append_doc(ordered, seen, doc)

    for doc in docs:
        _append_doc(ordered, seen, doc)

    for doc in docs:
        doc_id = document_id(doc)
        if doc_id:
            annotations[doc_id] = document_annotation(
                doc,
                all_docs=docs,
                plan_annotations=annotations,
            )

    if limit_docs > 0 and len(ordered) < min(limit_docs, len(docs)):
        missing = [doc for doc in docs if document_id(doc) not in seen]
        ordered.extend(missing[: max(0, limit_docs - len(ordered))])

    return ordered, annotations
