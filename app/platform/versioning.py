"""Canonical hashing and version binding for AI and integration artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.platform.contracts import AIVersionBinding


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set):
        normalized = [_canonical_value(item) for item in value]
        return sorted(normalized, key=lambda item: canonical_json(item))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be hashed canonically")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def current_code_revision() -> str:
    return (
        os.getenv("APP_REVISION")
        or os.getenv("GIT_COMMIT")
        or os.getenv("SOURCE_VERSION")
        or "unknown"
    )


def build_ai_version_binding(
    *,
    prompt_version: str,
    prompt_template: str,
    model_provider: str,
    model_id: str,
    model_config: dict[str, Any],
    schema_version: str,
    schema: dict[str, Any],
    context: str,
    input_payload: Any,
    code_revision: str | None = None,
    dataset_sha256: str | None = None,
    evaluator_version: str | None = None,
) -> AIVersionBinding:
    return AIVersionBinding(
        prompt_version=prompt_version,
        prompt_sha256=sha256_text(prompt_template),
        model_provider=model_provider,
        model_id=model_id,
        model_config_sha256=sha256_json(model_config),
        schema_version=schema_version,
        schema_sha256=sha256_json(schema),
        context_sha256=sha256_text(context),
        input_sha256=sha256_json(input_payload),
        code_revision=code_revision or current_code_revision(),
        dataset_sha256=dataset_sha256,
        evaluator_version=evaluator_version,
    )
