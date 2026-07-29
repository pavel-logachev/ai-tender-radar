-- Additive commercial platform foundation.
-- Existing production tables and write paths remain unchanged until an explicit cutover.

CREATE OR REPLACE FUNCTION atr_jsonb_contains_secret_key(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
STRICT
AS $$
DECLARE
    item_key TEXT;
    item_value JSONB;
BEGIN
    IF jsonb_typeof(value) = 'object' THEN
        FOR item_key, item_value IN SELECT * FROM jsonb_each(value)
        LOOP
            IF item_key ~* '(^|[_-])(token|password|secret|api[_-]?key|authorization|credentials?)([_-]|$)'
                OR regexp_replace(lower(item_key), '[^a-z0-9]', '', 'g') = ANY (
                    ARRAY[
                        'token', 'accesstoken', 'refreshtoken', 'authtoken',
                        'password', 'clientpassword', 'secret', 'clientsecret',
                        'apikey', 'authorization', 'credential', 'credentials'
                    ]
                ) THEN
                RETURN TRUE;
            END IF;
            IF atr_jsonb_contains_secret_key(item_value) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(value) = 'array' THEN
        FOR item_value IN SELECT * FROM jsonb_array_elements(value)
        LOOP
            IF atr_jsonb_contains_secret_key(item_value) THEN
                RETURN TRUE;
            END IF;
        END LOOP;
    END IF;
    RETURN FALSE;
END;
$$;

CREATE TABLE IF NOT EXISTS workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_slug_lower
    ON workspaces (lower(slug));

CREATE TABLE IF NOT EXISTS workspace_memberships (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    principal_type TEXT NOT NULL
        CHECK (principal_type IN ('user', 'service')),
    principal_id TEXT NOT NULL,
    role TEXT NOT NULL
        CHECK (role IN ('owner', 'admin', 'analyst', 'viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, principal_type, principal_id),
    CHECK (char_length(principal_id) BETWEEN 1 AND 300)
);

CREATE TABLE IF NOT EXISTS company_profile_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    legacy_company_profile_id UUID REFERENCES company_profiles(id) ON DELETE SET NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'retired')),
    profile JSONB NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    UNIQUE (workspace_id, version),
    UNIQUE (workspace_id, content_sha256),
    UNIQUE (workspace_id, id),
    CHECK ((status = 'active' AND activated_at IS NOT NULL) OR status <> 'active')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_company_profile_versions_active
    ON company_profile_versions (workspace_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS workspace_tenders (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tender_id UUID NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'visible'
        CHECK (state IN ('visible', 'hidden', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, tender_id)
);

CREATE TABLE IF NOT EXISTS source_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    adapter_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'error', 'disabled')),
    schema_version TEXT NOT NULL DEFAULT 'procurement-source-v1',
    public_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    secret_reference TEXT,
    cursor JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_success_at TIMESTAMPTZ,
    last_error_class TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name),
    UNIQUE (workspace_id, id),
    CHECK (adapter_type ~ '^[a-z0-9][a-z0-9_.-]*$'),
    CHECK (secret_reference IS NULL OR char_length(secret_reference) BETWEEN 1 AND 500),
    CHECK (NOT atr_jsonb_contains_secret_key(public_config))
);

CREATE TABLE IF NOT EXISTS procurement_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_connection_id UUID,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'procurement-source-v1',
    payload JSONB NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    source_updated_at TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, source, external_id, payload_sha256),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, source_connection_id)
        REFERENCES source_connections(workspace_id, id),
    CHECK (NOT atr_jsonb_contains_secret_key(payload))
);

CREATE INDEX IF NOT EXISTS idx_procurement_signals_latest
    ON procurement_signals (workspace_id, source, external_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tender_id UUID,
    signal_id UUID,
    profile_version_id UUID,
    parent_run_id UUID,
    correlation_id UUID,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('started', 'succeeded', 'failed', 'abstained', 'review_required')),
    prompt_version TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    model_provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_config_sha256 TEXT NOT NULL CHECK (model_config_sha256 ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL,
    schema_sha256 TEXT NOT NULL CHECK (schema_sha256 ~ '^[0-9a-f]{64}$'),
    context_sha256 TEXT NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
    input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    code_revision TEXT NOT NULL,
    dataset_sha256 TEXT CHECK (dataset_sha256 IS NULL OR dataset_sha256 ~ '^[0-9a-f]{64}$'),
    evaluator_version TEXT,
    output JSONB,
    output_sha256 TEXT CHECK (output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$'),
    error_class TEXT,
    error_message TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    cost_units NUMERIC(20, 8) CHECK (cost_units IS NULL OR cost_units >= 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, tender_id)
        REFERENCES workspace_tenders(workspace_id, tender_id),
    FOREIGN KEY (workspace_id, signal_id)
        REFERENCES procurement_signals(workspace_id, id),
    FOREIGN KEY (workspace_id, profile_version_id)
        REFERENCES company_profile_versions(workspace_id, id),
    FOREIGN KEY (workspace_id, parent_run_id)
        REFERENCES analysis_runs(workspace_id, id),
    CHECK (status <> 'succeeded' OR (output IS NOT NULL AND output_sha256 IS NOT NULL)),
    CHECK (status <> 'failed' OR error_class IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_analysis_runs_workspace_task_created
    ON analysis_runs (workspace_id, task_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_tender_created
    ON analysis_runs (workspace_id, tender_id, created_at DESC)
    WHERE tender_id IS NOT NULL;

CREATE OR REPLACE FUNCTION reject_analysis_run_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'analysis_runs is append-only';
END;
$$;

DROP TRIGGER IF EXISTS analysis_runs_append_only ON analysis_runs;
CREATE TRIGGER analysis_runs_append_only
    BEFORE UPDATE OR DELETE ON analysis_runs
    FOR EACH ROW EXECUTE FUNCTION reject_analysis_run_mutation();

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'received'
        CHECK (state IN (
            'received', 'validated', 'accepted', 'processing', 'succeeded',
            'retry_scheduled', 'dead_lettered', 'reconciled', 'cancelled'
        )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deadline_at TIMESTAMPTZ,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    last_error_class TEXT,
    last_error_message TEXT,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (workspace_id, kind, operation_key),
    UNIQUE (workspace_id, id),
    CHECK (NOT atr_jsonb_contains_secret_key(payload)),
    CHECK ((lease_owner IS NULL) = (lease_until IS NULL)),
    CHECK (attempts <= max_attempts)
);

CREATE INDEX IF NOT EXISTS idx_jobs_claimable
    ON jobs (state, available_at, created_at)
    WHERE state IN ('accepted', 'retry_scheduled');
CREATE INDEX IF NOT EXISTS idx_jobs_workspace_state
    ON jobs (workspace_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    job_id UUID,
    channel TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL
        CHECK (state IN ('accepted', 'delivered', 'failed', 'unknown', 'reconciled')),
    provider_reference TEXT,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, channel, operation_key),
    FOREIGN KEY (workspace_id, job_id)
        REFERENCES jobs(workspace_id, id)
);

CREATE TABLE IF NOT EXISTS outcome_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    tender_id UUID,
    signal_id UUID,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'service', 'import')),
    actor_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, tender_id)
        REFERENCES workspace_tenders(workspace_id, tender_id),
    FOREIGN KEY (workspace_id, signal_id)
        REFERENCES procurement_signals(workspace_id, id),
    CHECK (NOT atr_jsonb_contains_secret_key(payload))
);

CREATE INDEX IF NOT EXISTS idx_outcome_events_workspace_tender
    ON outcome_events (workspace_id, tender_id, occurred_at DESC)
    WHERE tender_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS workspace_audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    correlation_id UUID,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'service', 'system')),
    actor_id TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (NOT atr_jsonb_contains_secret_key(metadata))
);

CREATE INDEX IF NOT EXISTS idx_workspace_audit_events_created
    ON workspace_audit_events (workspace_id, created_at DESC);
