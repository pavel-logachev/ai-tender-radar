"""Append-only persistence for version-bound AI analysis runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.platform.contracts import AnalysisRunDraft
from app.platform.versioning import sha256_json


ANALYSIS_RUN_INSERT_SQL = """
INSERT INTO analysis_runs (
    id,
    workspace_id,
    tender_id,
    signal_id,
    profile_version_id,
    correlation_id,
    task_type,
    status,
    prompt_version,
    prompt_sha256,
    model_provider,
    model_id,
    model_config_sha256,
    schema_version,
    schema_sha256,
    context_sha256,
    input_sha256,
    code_revision,
    dataset_sha256,
    evaluator_version,
    output,
    output_sha256,
    error_class,
    error_message,
    latency_ms,
    cost_units,
    started_at,
    finished_at
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
);
"""


def insert_analysis_run(
    connection: Any,
    draft: AnalysisRunDraft,
    *,
    run_id: UUID | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> UUID:
    """Insert one immutable run inside the caller-owned transaction."""

    identity = run_id or uuid4()
    started = started_at or datetime.now(timezone.utc)
    if started.tzinfo is None:
        raise ValueError("started_at must include a timezone")
    terminal = draft.status != "started"
    finished = finished_at or (datetime.now(timezone.utc) if terminal else None)
    if finished is not None and finished.tzinfo is None:
        raise ValueError("finished_at must include a timezone")
    if draft.status == "started" and finished is not None:
        raise ValueError("started analysis run cannot have finished_at")
    if finished is not None and finished < started:
        raise ValueError("finished_at cannot be earlier than started_at")
    if draft.output is not None and draft.output_sha256 != sha256_json(draft.output):
        raise ValueError("output_sha256 does not match output")
    binding = draft.binding
    params = (
        identity,
        draft.workspace_id,
        draft.tender_id,
        draft.signal_id,
        draft.profile_version_id,
        draft.correlation_id,
        draft.task_type,
        draft.status,
        binding.prompt_version,
        binding.prompt_sha256,
        binding.model_provider,
        binding.model_id,
        binding.model_config_sha256,
        binding.schema_version,
        binding.schema_sha256,
        binding.context_sha256,
        binding.input_sha256,
        binding.code_revision,
        binding.dataset_sha256,
        binding.evaluator_version,
        Jsonb(draft.output) if draft.output is not None else None,
        draft.output_sha256,
        draft.error_class,
        draft.error_message,
        draft.latency_ms,
        draft.cost_units,
        started,
        finished,
    )
    with connection.cursor() as cursor:
        cursor.execute(ANALYSIS_RUN_INSERT_SQL, params)
    return identity
