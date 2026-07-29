"""Deterministic job identity, replay, retry, and state-transition rules."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.platform.versioning import sha256_json


class JobState(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    RECONCILED = "reconciled"
    CANCELLED = "cancelled"


class ReplayDecision(StrEnum):
    NEW = "new"
    REUSE = "reuse"
    CONFLICT = "conflict"


TERMINAL_STATES = {
    JobState.SUCCEEDED,
    JobState.DEAD_LETTERED,
    JobState.RECONCILED,
    JobState.CANCELLED,
}

ALLOWED_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.RECEIVED: {JobState.VALIDATED, JobState.DEAD_LETTERED, JobState.CANCELLED},
    JobState.VALIDATED: {JobState.ACCEPTED, JobState.DEAD_LETTERED, JobState.CANCELLED},
    JobState.ACCEPTED: {JobState.PROCESSING, JobState.CANCELLED},
    JobState.PROCESSING: {
        JobState.SUCCEEDED,
        JobState.RETRY_SCHEDULED,
        JobState.DEAD_LETTERED,
    },
    JobState.RETRY_SCHEDULED: {
        JobState.PROCESSING,
        JobState.DEAD_LETTERED,
        JobState.CANCELLED,
    },
    JobState.SUCCEEDED: {JobState.RECONCILED},
    JobState.DEAD_LETTERED: {JobState.ACCEPTED, JobState.RECONCILED},
    JobState.RECONCILED: set(),
    JobState.CANCELLED: set(),
}


class JobSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: UUID
    workspace_id: UUID
    kind: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    operation_key: str = Field(min_length=1, max_length=300)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: JobState
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    available_at: datetime
    deadline_at: datetime | None = None
    lease_owner: str | None = Field(default=None, max_length=200)
    lease_until: datetime | None = None
    last_error_class: str | None = Field(default=None, max_length=120)
    last_error_message: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_timestamps_and_lease(self) -> "JobSnapshot":
        for field_name in ("available_at", "deadline_at", "lease_until"):
            value = getattr(self, field_name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{field_name} must include a timezone")
        if (self.lease_owner is None) != (self.lease_until is None):
            raise ValueError("lease_owner and lease_until must be set together")
        if self.state == JobState.PROCESSING and self.lease_owner is None:
            raise ValueError("processing job requires a lease")
        if self.attempts > self.max_attempts:
            raise ValueError("attempts cannot exceed max_attempts")
        return self


def new_job(
    *,
    workspace_id: UUID,
    kind: str,
    operation_key: str,
    payload: dict[str, Any],
    max_attempts: int = 3,
    now: datetime | None = None,
    deadline_at: datetime | None = None,
    job_id: UUID | None = None,
) -> JobSnapshot:
    current = now or datetime.now(timezone.utc)
    return JobSnapshot(
        id=job_id or uuid4(),
        workspace_id=workspace_id,
        kind=kind,
        operation_key=operation_key,
        payload_hash=sha256_json(payload),
        state=JobState.RECEIVED,
        max_attempts=max_attempts,
        available_at=current,
        deadline_at=deadline_at,
    )


def replay_decision(
    existing: JobSnapshot | None,
    *,
    incoming_payload: dict[str, Any],
) -> ReplayDecision:
    if existing is None:
        return ReplayDecision.NEW
    return (
        ReplayDecision.REUSE
        if existing.payload_hash == sha256_json(incoming_payload)
        else ReplayDecision.CONFLICT
    )


def retry_delay_seconds(
    *,
    job_id: UUID,
    attempt: int,
    base_seconds: int = 30,
    max_seconds: int = 3_600,
) -> int:
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    if base_seconds < 1 or max_seconds < base_seconds:
        raise ValueError("invalid retry delay bounds")
    exponential = min(max_seconds, base_seconds * (2 ** (attempt - 1)))
    digest = hashlib.sha256(f"{job_id}:{attempt}".encode("ascii")).digest()
    jitter_cap = max(1, exponential // 5)
    jitter = int.from_bytes(digest[:2], "big") % (jitter_cap + 1)
    return min(max_seconds, exponential + jitter)


def transition_job(
    job: JobSnapshot,
    target: JobState,
    *,
    now: datetime | None = None,
    lease_owner: str | None = None,
    lease_seconds: int = 300,
    error_class: str | None = None,
    error_message: str | None = None,
) -> JobSnapshot:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    if target not in ALLOWED_TRANSITIONS[job.state]:
        raise ValueError(f"invalid job transition: {job.state.value} -> {target.value}")
    if job.deadline_at is not None and current > job.deadline_at and target == JobState.PROCESSING:
        raise ValueError("job deadline has expired")

    updates: dict[str, Any] = {
        "state": target,
        "last_error_class": error_class,
        "last_error_message": error_message,
    }
    if target == JobState.PROCESSING:
        if not lease_owner:
            raise ValueError("lease_owner is required for processing")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if job.attempts >= job.max_attempts:
            raise ValueError("retry budget is exhausted")
        updates.update(
            attempts=job.attempts + 1,
            lease_owner=lease_owner,
            lease_until=current + timedelta(seconds=lease_seconds),
            available_at=current,
        )
    elif target == JobState.RETRY_SCHEDULED:
        if not error_class:
            raise ValueError("retry_scheduled requires error_class")
        if job.attempts >= job.max_attempts:
            raise ValueError("retry budget is exhausted; dead-letter the job")
        updates.update(
            lease_owner=None,
            lease_until=None,
            available_at=current
            + timedelta(seconds=retry_delay_seconds(job_id=job.id, attempt=job.attempts)),
        )
    else:
        if target == JobState.DEAD_LETTERED and not error_class:
            raise ValueError("dead_lettered requires error_class")
        updates.update(lease_owner=None, lease_until=None)
    values = job.model_dump(mode="python")
    values.update(updates)
    return JobSnapshot.model_validate(values, strict=True)


def redrive_dead_letter(
    job: JobSnapshot,
    *,
    authorized: bool,
    now: datetime | None = None,
) -> JobSnapshot:
    """Explicit operator redrive that preserves identity and resets the retry budget."""

    if not authorized:
        raise PermissionError("dead-letter redrive requires explicit operator authorization")
    if job.state != JobState.DEAD_LETTERED:
        raise ValueError("only dead-lettered jobs can be re-driven")
    current = now or datetime.now(timezone.utc)
    values = job.model_dump(mode="python")
    values.update(
        state=JobState.ACCEPTED,
        attempts=0,
        available_at=current,
        lease_owner=None,
        lease_until=None,
        last_error_class=None,
        last_error_message=None,
    )
    return JobSnapshot.model_validate(values, strict=True)
