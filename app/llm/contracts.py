"""Strict structured-output contracts for released AI product decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


LEAD_TRIAGE_PROMPT_VERSION = "lead-triage-v1"
LEAD_TRIAGE_SCHEMA_VERSION = "lead-triage-output-v1"


class LeadTriageOutput(BaseModel):
    """Exact live output required from the paid lead-triage call."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    lead_decision: Literal["go", "maybe", "reject"]
    lead_priority: Literal["high", "medium", "low"]
    confidence: Literal["high", "medium", "low"]
    lead_summary: str = Field(max_length=1_200)
    likely_customer_story: str = Field(max_length=1_200)
    possible_needs: list[str] = Field(max_length=8)
    target_roles: list[str] = Field(max_length=8)
    reject_reason: str = Field(max_length=1_000)
    requires_full_lead_report: bool

    @field_validator("lead_summary", "likely_customer_story", "reject_reason")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("possible_needs", "target_roles")
    @classmethod
    def validate_short_string_list(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            text = value.strip()
            if not text:
                raise ValueError("list items must not be empty")
            if len(text) > 240:
                raise ValueError("list items must not exceed 240 characters")
            normalized = text.casefold()
            if normalized not in seen:
                seen.add(normalized)
                result.append(text)
        return result

    @model_validator(mode="after")
    def validate_decision_invariants(self) -> "LeadTriageOutput":
        if self.lead_decision == "go":
            if not self.requires_full_lead_report:
                raise ValueError("go decision requires a full lead report")
            if self.reject_reason:
                raise ValueError("go decision must not include reject_reason")
        if self.lead_decision == "reject":
            if self.requires_full_lead_report:
                raise ValueError("reject decision cannot require a full lead report")
            if not self.reject_reason:
                raise ValueError("reject decision requires reject_reason")
        return self


def validate_lead_triage_output(value: Any) -> dict[str, Any]:
    """Validate exact model output; callers decide the safe fallback on failure."""

    model = LeadTriageOutput.model_validate(value, strict=True)
    return model.model_dump(mode="json")


def inspect_lead_triage_output(value: Any) -> dict[str, Any]:
    """Return sanitized strict-contract diagnostics without changing live behavior."""

    try:
        validate_lead_triage_output(value)
    except ValidationError as exc:
        errors = exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
        return {
            "mode": "shadow",
            "status": "failed",
            "error_count": len(errors),
            "error_fields": sorted(
                {
                    ".".join(str(item) for item in error.get("loc", ())) or "root"
                    for error in errors
                }
            ),
            "error_types": sorted(
                {str(error.get("type") or "validation_error") for error in errors}
            ),
        }

    return {
        "mode": "shadow",
        "status": "passed",
        "error_count": 0,
        "error_fields": [],
        "error_types": [],
    }
