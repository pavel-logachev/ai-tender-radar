"""Strict versioned contracts for the commercial platform foundation."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "procurement-source-v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "authtoken",
    "authorization",
    "clientpassword",
    "clientsecret",
    "credential",
    "credentials",
    "password",
    "refreshtoken",
    "secret",
    "token",
}
STRICT_MODEL_CONFIG = ConfigDict(
    extra="forbid",
    frozen=True,
    strict=True,
    str_strip_whitespace=True,
)


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class EvidenceReference(BaseModel):
    """Pointer to evidence without copying an entire source document."""

    model_config = STRICT_MODEL_CONFIG

    source_id: str = Field(min_length=1, max_length=200)
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    locator: str = Field(min_length=1, max_length=500)
    excerpt: str | None = Field(default=None, max_length=2_000)


class CanonicalSourceRecord(BaseModel):
    """Source-neutral tender record used at the adapter boundary."""

    model_config = STRICT_MODEL_CONFIG

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    source: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    external_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=2_000)
    customer_name: str | None = Field(default=None, max_length=1_000)
    initial_price: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="RUB", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    law: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=300)
    published_at: datetime | None = None
    deadline_at: datetime | None = None
    url: str | None = Field(default=None, max_length=2_000)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("url must use http or https and include a host")
        if parsed.username or parsed.password:
            raise ValueError("url credentials are not allowed")
        return value

    @model_validator(mode="after")
    def validate_raw_size(self) -> "CanonicalSourceRecord":
        def contains_sensitive_key(value: Any) -> bool:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = "".join(
                        character
                        for character in str(key).casefold()
                        if character.isalnum()
                    )
                    if normalized in SENSITIVE_KEYS:
                        return True
                    if contains_sensitive_key(item):
                        return True
            elif isinstance(value, list):
                return any(contains_sensitive_key(item) for item in value)
            return False

        if contains_sensitive_key(self.raw):
            raise ValueError("raw payload contains a secret-like key")
        encoded = json.dumps(
            self.raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) > 1_000_000:
            raise ValueError("raw payload exceeds 1,000,000 bytes")
        return self


class AIVersionBinding(BaseModel):
    """Reproducibility identity attached to every new AI run."""

    model_config = STRICT_MODEL_CONFIG

    prompt_version: str = Field(min_length=1, max_length=100)
    prompt_sha256: str = Field(pattern=SHA256_PATTERN)
    model_provider: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    model_config_sha256: str = Field(pattern=SHA256_PATTERN)
    schema_version: str = Field(min_length=1, max_length=100)
    schema_sha256: str = Field(pattern=SHA256_PATTERN)
    context_sha256: str = Field(pattern=SHA256_PATTERN)
    input_sha256: str = Field(pattern=SHA256_PATTERN)
    code_revision: str = Field(min_length=1, max_length=200)
    dataset_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    evaluator_version: str | None = Field(default=None, max_length=100)


class AnalysisRunDraft(BaseModel):
    """Append-only analysis run ready for persistence."""

    model_config = STRICT_MODEL_CONFIG

    workspace_id: UUID
    task_type: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    status: Literal["started", "succeeded", "failed", "abstained", "review_required"]
    binding: AIVersionBinding
    tender_id: UUID | None = None
    signal_id: UUID | None = None
    profile_version_id: UUID | None = None
    correlation_id: UUID | None = None
    output: dict[str, Any] | None = None
    output_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    error_class: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=1_000)
    latency_ms: int | None = Field(default=None, ge=0)
    cost_units: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> "AnalysisRunDraft":
        if self.status == "succeeded" and (self.output is None or self.output_sha256 is None):
            raise ValueError("succeeded analysis run requires output and output_sha256")
        if self.status == "failed" and not self.error_class:
            raise ValueError("failed analysis run requires error_class")
        return self
