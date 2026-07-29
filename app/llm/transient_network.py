from __future__ import annotations

import re
from typing import Iterable


TRANSIENT_HTTP_STATUS_CODES = {502, 503, 504, 520, 522, 524}

ROUTERAI_READ_TIMEOUT_NOT_RETRIED_MESSAGE = (
    "RouterAI read timeout. Request may still be running or charged upstream; "
    "not retrying automatically to avoid duplicate paid requests."
)

TRANSIENT_NETWORK_MARKERS = (
    "httpx.connecterror",
    "httpx.connecttimeout",
    "temporary failure in name resolution",
    "name or service not known",
)

READ_TIMEOUT_MARKERS = (
    "httpx.readtimeout",
    "routerai read timeout",
    "read timeout",
)

NON_TRANSIENT_ERROR_MARKERS = (
    "documents_missing",
    "no_valid_documents",
    "marketplace_auth",
    "business_action=",
    "effective_recommendation=no_go",
    "non_json_response",
    "cannot parse json from llm response",
    "cannot parse routerai",
    "jsondecodeerror",
    "validationerror",
    "validation error",
    "schema validation",
    "model refusal",
    "malformed content",
)


def is_transient_http_status_code(status_code: int | None) -> bool:
    return status_code in TRANSIENT_HTTP_STATUS_CODES


def _status_code_markers(status_codes: Iterable[int]) -> tuple[str, ...]:
    return tuple(
        marker
        for code in status_codes
        for marker in (
            f"status={code}",
            f"status_code={code}",
            f"status code {code}",
            f"http {code}",
            f"http/{code}",
        )
    )


def _contains_transient_status(text: str) -> bool:
    if any(marker in text for marker in _status_code_markers(TRANSIENT_HTTP_STATUS_CODES)):
        return True

    return any(
        re.search(rf"\b{code}\b", text)
        and any(
            phrase in text
            for phrase in (
                "bad gateway",
                "service unavailable",
                "gateway timeout",
                "cloudflare",
                "connection timed out",
            )
        )
        for code in TRANSIENT_HTTP_STATUS_CODES
    )


def is_transient_network_error_text(text: str | None) -> bool:
    normalized = str(text or "").lower()
    if not normalized:
        return False

    if any(marker in normalized for marker in NON_TRANSIENT_ERROR_MARKERS):
        return False

    return (
        any(marker in normalized for marker in TRANSIENT_NETWORK_MARKERS)
        or _contains_transient_status(normalized)
    )


def is_read_timeout_error_text(text: str | None) -> bool:
    normalized = str(text or "").lower()
    if not normalized:
        return False

    return any(marker in normalized for marker in READ_TIMEOUT_MARKERS)


def transient_network_error_summary(text: str | None, *, max_chars: int = 160) -> str:
    raw = str(text or "")
    normalized = raw.lower()

    for marker in (
        "httpx.ConnectError",
        "httpx.ConnectTimeout",
        "Temporary failure in name resolution",
        "Name or service not known",
    ):
        if marker.lower() in normalized:
            return marker

    for code in sorted(TRANSIENT_HTTP_STATUS_CODES):
        if is_transient_network_error_text(f"status={code}") and re.search(
            rf"\b{code}\b",
            normalized,
        ):
            return f"http_status={code}"

    lines = [" ".join(line.split()) for line in raw.splitlines() if line.strip()]
    summary = lines[-1] if lines else "transient_network_error"
    if len(summary) <= max_chars:
        return summary
    return f"{summary[: max_chars - 3]}..."
