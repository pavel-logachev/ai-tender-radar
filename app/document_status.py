from __future__ import annotations

from typing import Any

from app.document_quality import (
    DOCUMENT_CONFIDENCE_HIGH,
    DOCUMENT_CONFIDENCE_LOW,
    DOCUMENT_CONFIDENCE_MEDIUM,
    DOCUMENT_SOURCE_CARD_ONLY,
    DOCUMENT_SOURCE_CONTRACT_OR_NOTICE,
    DOCUMENT_SOURCE_MISSING,
    DOCUMENT_SOURCE_PRICING_OR_BOM,
    DOCUMENT_SOURCE_UNKNOWN,
    normalize_document_quality_fields,
)


TECHNICAL_SPEC_FOUND = "technical_spec_found"
DOCUMENTS_PRESENT_NO_PRIMARY_SPEC = "documents_present_no_primary_spec"
DOCUMENTS_MISSING_CARD_ONLY = "documents_missing_card_only"
DOCUMENTS_BLOCKED_CARD_ONLY = "documents_blocked_card_only"
DOCUMENTS_PARTIAL_TECHNICAL_SPEC_FOUND = "documents_partial_technical_spec_found"

TECHNICAL_DOCUMENT_BASIS = "technical_document"
DOCUMENTS_WITHOUT_PRIMARY_BASIS = "documents_without_primary"
LEAD_CARD_ONLY_BASIS = "lead_card_only"

DOCUMENT_STATUS_LABELS = {
    TECHNICAL_SPEC_FOUND: "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b: \u0422\u0417 \u043d\u0430\u0439\u0434\u0435\u043d\u043e",
    DOCUMENTS_PRESENT_NO_PRIMARY_SPEC: (
        "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b: \u0435\u0441\u0442\u044c, "
        "\u043d\u043e \u0446\u0435\u043b\u0435\u0432\u043e\u0435 \u0422\u0417 "
        "\u043d\u0435 \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0435\u043d\u043e"
    ),
    DOCUMENTS_MISSING_CARD_ONLY: (
        "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b: \u043d\u0435 "
        "\u043f\u043e\u043b\u0443\u0447\u0435\u043d\u044b, "
        "\u0430\u043d\u0430\u043b\u0438\u0437 \u043f\u043e "
        "\u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0435"
    ),
    DOCUMENTS_BLOCKED_CARD_ONLY: (
        "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b: \u043d\u0435 "
        "\u043f\u043e\u043b\u0443\u0447\u0435\u043d\u044b, "
        "\u0430\u043d\u0430\u043b\u0438\u0437 \u043f\u043e "
        "\u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0435"
    ),
    DOCUMENTS_PARTIAL_TECHNICAL_SPEC_FOUND: (
        "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b: "
        "\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b "
        "\u0447\u0430\u0441\u0442\u0438\u0447\u043d\u043e, \u0422\u0417 "
        "\u043d\u0430\u0439\u0434\u0435\u043d\u043e"
    ),
}

DOCUMENT_STATUS_BASIS = {
    TECHNICAL_SPEC_FOUND: TECHNICAL_DOCUMENT_BASIS,
    DOCUMENTS_PARTIAL_TECHNICAL_SPEC_FOUND: TECHNICAL_DOCUMENT_BASIS,
    DOCUMENTS_PRESENT_NO_PRIMARY_SPEC: DOCUMENTS_WITHOUT_PRIMARY_BASIS,
    DOCUMENTS_MISSING_CARD_ONLY: LEAD_CARD_ONLY_BASIS,
    DOCUMENTS_BLOCKED_CARD_ONLY: LEAD_CARD_ONLY_BASIS,
}

DOCUMENT_STATUS_CODES = tuple(DOCUMENT_STATUS_LABELS)

BLOCKED_STATUS_MARKERS = (
    "blocked_by_marketplace_auth",
    "marketplace_auth",
    "external_marketplace_auth_required",
    "no_valid_documents",
    "preparation_no_valid_documents",
)


def document_status_label(code: Any) -> str:
    return DOCUMENT_STATUS_LABELS.get(str(code or ""), DOCUMENT_STATUS_LABELS[DOCUMENTS_MISSING_CARD_ONLY])


def document_status_quality_label(status: dict[str, Any] | None) -> str:
    status = status if isinstance(status, dict) else {}
    code = status.get("code")
    base = str(status.get("label") or document_status_label(code))
    confidence = str(status.get("primary_document_confidence") or "").strip()
    source_kind = str(status.get("primary_document_source_kind") or "").strip()

    high_confidence_technical = (
        confidence in {"", DOCUMENT_CONFIDENCE_HIGH}
        and code in {TECHNICAL_SPEC_FOUND, DOCUMENTS_PARTIAL_TECHNICAL_SPEC_FOUND}
    )
    if high_confidence_technical:
        return "Документы: ТЗ найдено, высокая уверенность"
    if confidence == DOCUMENT_CONFIDENCE_MEDIUM:
        if source_kind == DOCUMENT_SOURCE_PRICING_OR_BOM:
            return "Документы: техническая таблица/НМЦК, средняя уверенность"
        return "Документы: технический источник, средняя уверенность"
    if confidence == DOCUMENT_CONFIDENCE_LOW:
        if source_kind in {DOCUMENT_SOURCE_MISSING, DOCUMENT_SOURCE_CARD_ONLY}:
            return "Документы: только карточка или документы не найдены, низкая уверенность"
        if source_kind == DOCUMENT_SOURCE_CONTRACT_OR_NOTICE:
            return "Документы: извещение/договор без полного ТЗ, низкая уверенность"
        if code in {DOCUMENTS_MISSING_CARD_ONLY, DOCUMENTS_BLOCKED_CARD_ONLY}:
            return "Документы: только карточка или документы не найдены, низкая уверенность"
        if code == DOCUMENTS_PRESENT_NO_PRIMARY_SPEC:
            return "Документы: primary ТЗ не выбран, низкая уверенность"
    return base


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _non_negative_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(float(str(value).strip())))
    except (TypeError, ValueError):
        return None


def _documents_count_from_summary(documents_summary: Any) -> int:
    return len(_as_list(documents_summary))


def _documents_with_text_from_summary(documents_summary: Any) -> int:
    count = 0
    for doc in _as_list(documents_summary):
        if not isinstance(doc, dict):
            continue
        text_len = _non_negative_int_or_none(doc.get("text_len")) or 0
        if text_len > 0:
            count += 1
    return count


def _contains_blocked_marker(*values: Any) -> bool:
    text = " ".join(str(value or "").strip().lower() for value in values if value)
    return any(marker in text for marker in BLOCKED_STATUS_MARKERS)


def _preparation_value(preparation: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in preparation and preparation.get(key) is not None:
            return preparation.get(key)
    return None


def build_document_status(
    *,
    document_selection: dict[str, Any] | None = None,
    document_preparation: dict[str, Any] | None = None,
    documents_summary: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selection = _as_dict(document_selection)
    preparation = _as_dict(document_preparation)
    documents_summary = documents_summary or []

    docs_count = _non_negative_int_or_none(
        _preparation_value(preparation, "docs_after", "docs_count", "documents_count")
    )
    if docs_count is None:
        docs_count = _documents_count_from_summary(documents_summary)

    docs_with_text_count = _non_negative_int_or_none(
        _preparation_value(
            preparation,
            "docs_with_text_after",
            "docs_with_text_count",
            "documents_with_text_count",
            "documents_with_text",
        )
    )
    if docs_with_text_count is None:
        docs_with_text_count = _documents_with_text_from_summary(documents_summary)

    selector_status = str(
        selection.get("selector_status") or selection.get("status") or ""
    ).strip()
    primary_document_id = str(selection.get("primary_document_id") or "").strip()
    primary_found = bool(primary_document_id) and selector_status in {
        TECHNICAL_SPEC_FOUND,
        "selector_failed",
    }

    preparation_status = str(preparation.get("preparation_status") or "").strip()
    targeted_download_triggered = bool(preparation.get("targeted_download_triggered"))
    skipped_due_to_rate_limit_count = _non_negative_int_or_none(
        preparation.get("documents_skipped_due_to_rate_limit")
    ) or 0
    documents_skipped_due_to_rate_limit = bool(
        skipped_due_to_rate_limit_count
        or preparation.get("documents_skipped_due_to_rate_limit") is True
        or preparation.get("stopped_due_to_rate_limit")
    )
    missing_high_value_technical_document = bool(
        preparation.get("missing_high_value_technical_document")
    )
    missing_high_value_technical_document_title = str(
        preparation.get("missing_high_value_technical_document_title") or ""
    ).strip()
    document_download_planner_used = bool(
        preparation.get("document_download_planner_used")
    )
    document_download_planner_confidence = str(
        preparation.get("document_download_planner_confidence") or ""
    ).strip()
    blocked = _contains_blocked_marker(
        preparation_status,
        preparation.get("document_state"),
        preparation.get("warning_reason"),
        preparation.get("non_blocking_warning_reason"),
        preparation.get("preparation_summary"),
    )

    partial = primary_found and (
        docs_count > docs_with_text_count
        or "partial" in preparation_status.lower()
        or "429" in str(preparation.get("non_blocking_warning_reason") or "")
        or (
            missing_high_value_technical_document
            and documents_skipped_due_to_rate_limit
        )
    )

    if primary_found:
        code = DOCUMENTS_PARTIAL_TECHNICAL_SPEC_FOUND if partial else TECHNICAL_SPEC_FOUND
    elif docs_with_text_count > 0:
        code = DOCUMENTS_PRESENT_NO_PRIMARY_SPEC
    elif blocked:
        code = DOCUMENTS_BLOCKED_CARD_ONLY
    else:
        code = DOCUMENTS_MISSING_CARD_ONLY

    warning = ""
    if code in {DOCUMENTS_MISSING_CARD_ONLY, DOCUMENTS_BLOCKED_CARD_ONLY}:
        warning = (
            "\u0410\u043d\u0430\u043b\u0438\u0437 "
            "\u043f\u0440\u0435\u0434\u0432\u0430\u0440\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439: "
            "\u0442\u0435\u043a\u0441\u0442 \u0422\u0417/\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u043e\u0439 "
            "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u0430\u0446\u0438\u0438 "
            "\u043d\u0435 \u043f\u043e\u043f\u0430\u043b \u0432 "
            "\u043a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 LLM."
        )
    elif code == DOCUMENTS_PRESENT_NO_PRIMARY_SPEC:
        warning = (
            "\u0414\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u044b "
            "\u0435\u0441\u0442\u044c, \u043d\u043e "
            "\u0446\u0435\u043b\u0435\u0432\u043e\u0439 "
            "\u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 "
            "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442 "
            "\u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d."
        )
    if missing_high_value_technical_document and documents_skipped_due_to_rate_limit:
        warning = (
            "В API есть вероятный технический документ, но он не был скачан "
            "из-за 429/rate limit. Анализ предварительный."
        )

    fallback_quality = {
        "primary_document_confidence": (
            DOCUMENT_CONFIDENCE_HIGH if primary_found else DOCUMENT_CONFIDENCE_LOW
        ),
        "primary_document_source_kind": (
            DOCUMENT_SOURCE_UNKNOWN
            if primary_found
            else (
                DOCUMENT_SOURCE_MISSING
                if docs_count <= 0
                else DOCUMENT_SOURCE_CARD_ONLY
                if docs_with_text_count <= 0
                else DOCUMENT_SOURCE_UNKNOWN
            )
        ),
        "primary_document_quality_reason": "",
        "primary_document_is_full_technical_spec": bool(primary_found),
        "primary_document_suspicious": False,
    }
    selection_quality_values = {
        key: value
        for key, value in selection.items()
        if key.startswith("primary_document_") and value not in {None, ""}
    }
    quality = normalize_document_quality_fields(
        {**fallback_quality, **selection_quality_values}
    )
    if primary_found and quality["primary_document_confidence"] == DOCUMENT_CONFIDENCE_MEDIUM:
        warning = (
            "Primary source is medium confidence: useful technical/pricing source, "
            "but not a full technical specification. Distinguish confirmed document "
            "facts from card-based assumptions."
        )
    elif primary_found and quality["primary_document_confidence"] == DOCUMENT_CONFIDENCE_LOW:
        warning = (
            "Primary source is low confidence: do not treat it as a full technical "
            "specification. Use the lead report as preliminary account-development context."
        )

    return {
        "code": code,
        "label": document_status_label(code),
        "analysis_basis": DOCUMENT_STATUS_BASIS[code],
        "primary_document_title": selection.get("primary_document_title") or "",
        "primary_section_hint": selection.get("primary_section_hint") or "",
        "primary_document_confidence": quality["primary_document_confidence"],
        "primary_document_source_kind": quality["primary_document_source_kind"],
        "primary_document_quality_reason": quality["primary_document_quality_reason"],
        "primary_document_is_full_technical_spec": quality[
            "primary_document_is_full_technical_spec"
        ],
        "primary_document_suspicious": quality["primary_document_suspicious"],
        "docs_count": docs_count,
        "docs_with_text_count": docs_with_text_count,
        "targeted_download_triggered": targeted_download_triggered,
        "preparation_status": preparation_status,
        "selector_status": selector_status or (
            "documents_missing" if docs_with_text_count <= 0 else DOCUMENTS_PRESENT_NO_PRIMARY_SPEC
        ),
        "missing_high_value_technical_document": missing_high_value_technical_document,
        "missing_high_value_technical_document_title": (
            missing_high_value_technical_document_title
        ),
        "documents_skipped_due_to_rate_limit": documents_skipped_due_to_rate_limit,
        "document_download_planner_used": document_download_planner_used,
        "document_download_planner_confidence": document_download_planner_confidence,
        "warning": warning,
    }
