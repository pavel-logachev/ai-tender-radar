from __future__ import annotations


def resolve_document_rate_limit_seconds(
    document_rate_limit_seconds: float | None,
    fallback_rate_limit_seconds: float,
) -> float:
    if document_rate_limit_seconds is None or document_rate_limit_seconds <= 0:
        return fallback_rate_limit_seconds

    return document_rate_limit_seconds
