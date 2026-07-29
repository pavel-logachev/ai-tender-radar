from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


ZAKUPKI_223_COMMON_INFO_URL_TEMPLATE = (
    "https://zakupki.gov.ru/epz/order/notice/notice223/common-info.html?regNumber={reg_number}"
)


def procedure_url_from_sources(
    row: Mapping[str, Any] | None,
    full: Mapping[str, Any] | None,
) -> str | None:
    row = row if isinstance(row, Mapping) else {}
    full = full if isinstance(full, Mapping) else {}
    candidates = [
        row.get("source_url"),
        row.get("procedure_url"),
        public_223_procedure_url(full),
        row.get("url"),
        full.get("tenderUrl"),
        full.get("procedureUrl"),
        full.get("purchaseUrl"),
        full.get("noticeUrl"),
        full.get("url"),
        full.get("href"),
        full.get("link"),
    ]

    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value

    return None


def public_223_procedure_url(full: Mapping[str, Any] | None) -> str | None:
    if not isinstance(full, Mapping) or not is_223_order(full):
        return None

    reg_number = first_text(
        full,
        "orderNumber",
        "purchaseNoticeNumber",
        "purchaseNumber",
        "regNumber",
        "registryNumber",
    )
    if not reg_number or not re.fullmatch(r"\d{8,14}", reg_number):
        return None

    return ZAKUPKI_223_COMMON_INFO_URL_TEMPLATE.format(reg_number=reg_number)


def is_223_order(full: Mapping[str, Any]) -> bool:
    for key in ("lawId", "lawName", "law", "tenderUrl", "procedureUrl", "purchaseUrl"):
        value = str(full.get(key) or "").lower()
        if "223" in value or "notice223" in value or "/223/" in value:
            return True
    return False


def first_text(source: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return None
