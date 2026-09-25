CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE contracts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_number text,
    title text NOT NULL,
    contract_type text NOT NULL,
    counterparty_name text,
    status text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN (
            'DRAFT', 'SUBMITTED', 'IN_APPROVAL', 'APPROVED', 'CHANGES_REQUESTED',
            'REJECTED', 'ACTIVE', 'SUSPENDED', 'TERMINATED', 'EXPIRED'
        )),
    current_version_id uuid,
    state_version integer NOT NULL DEFAULT 0,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, contract_number)
);

CREATE TABLE contract_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL,
    version_number integer NOT NULL CHECK (version_number > 0),
    status text NOT NULL DEFAULT 'UPLOADING'
        CHECK (status IN ('UPLOADING', 'UPLOADED', 'PARSING', 'READY', 'FAILED', 'ARCHIVED')),
    object_key text NOT NULL,
    file_name text NOT NULL,
    media_type text NOT NULL,
    size_bytes bigint CHECK (size_bytes >= 0),
    content_hash text,
    page_count integer CHECK (page_count >= 0),
    parser_version text,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, contract_id, version_number),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id)
);

CREATE UNIQUE INDEX contract_versions_content_hash_idx
    ON contract_versions (tenant_id, contract_id, content_hash)
    WHERE content_hash IS NOT NULL;

ALTER TABLE contracts
    ADD CONSTRAINT contracts_current_version_fk
    FOREIGN KEY (tenant_id, current_version_id)
    REFERENCES contract_versions(tenant_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE ingestion_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_version_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    current_step text,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    lease_owner text,
    lease_expires_at timestamptz,
    next_attempt_at timestamptz,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, contract_version_id),
    FOREIGN KEY (tenant_id, contract_version_id)
        REFERENCES contract_versions(tenant_id, id)
);

CREATE INDEX ingestion_jobs_claim_idx
    ON ingestion_jobs (next_attempt_at, created_at)
    WHERE status IN ('PENDING', 'RETRY_WAIT');

CREATE TABLE contract_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_version_id uuid NOT NULL,
    sequence integer NOT NULL CHECK (sequence >= 0),
    page_number integer CHECK (page_number > 0),
    clause_type text,
    heading_path text[],
    content text NOT NULL,
    content_hash text NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(content, ''))
    ) STORED,
    embedding vector(1024),
    embedding_model text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, contract_version_id, sequence),
    FOREIGN KEY (tenant_id, contract_version_id)
        REFERENCES contract_versions(tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX contract_chunks_search_idx ON contract_chunks USING gin (search_vector);
CREATE INDEX contract_chunks_embedding_idx ON contract_chunks
    USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

CREATE TABLE review_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_version_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN (
            'DRAFT', 'INGESTING', 'AI_REVIEW', 'HUMAN_REVIEW', 'APPROVED',
            'ACTIVE', 'REJECTED', 'FAILED', 'CANCELLED', 'EXPIRED'
        )),
    rule_set_version text,
    state_version integer NOT NULL DEFAULT 0,
    requested_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_version_id)
        REFERENCES contract_versions(tenant_id, id)
);

CREATE TABLE review_findings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    review_case_id uuid NOT NULL,
    contract_version_id uuid NOT NULL,
    category text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    title text NOT NULL,
    description text NOT NULL,
    recommendation text,
    source_chunk_id uuid,
    source_page integer CHECK (source_page > 0),
    source_text text,
    confidence numeric(5,4) CHECK (confidence >= 0 AND confidence <= 1),
    resolution_status text NOT NULL DEFAULT 'OPEN'
        CHECK (resolution_status IN ('OPEN', 'ACCEPTED', 'MITIGATED', 'DISMISSED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, review_case_id) REFERENCES review_cases(tenant_id, id),
    FOREIGN KEY (tenant_id, contract_version_id)
        REFERENCES contract_versions(tenant_id, id),
    FOREIGN KEY (tenant_id, source_chunk_id) REFERENCES contract_chunks(tenant_id, id)
);

CREATE TABLE approval_steps (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    review_case_id uuid NOT NULL,
    step_order integer NOT NULL CHECK (step_order > 0),
    role text NOT NULL CHECK (role IN ('LEGAL', 'FINANCE', 'BUSINESS')),
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'IN_PROGRESS', 'APPROVED', 'REJECTED', 'SKIPPED')),
    assigned_to uuid,
    decided_by uuid,
    decision_comment text,
    decided_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, review_case_id, step_order),
    FOREIGN KEY (tenant_id, review_case_id) REFERENCES review_cases(tenant_id, id)
);

CREATE TABLE obligations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL,
    source_version_id uuid NOT NULL,
    obligation_type text NOT NULL,
    title text NOT NULL,
    description text,
    responsible_user_id uuid,
    due_at timestamptz,
    next_action_at timestamptz,
    status text NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'CONFIRMED', 'DUE', 'COMPLETED', 'OVERDUE', 'CANCELLED')),
    source_page integer CHECK (source_page > 0),
    source_text text,
    state_version integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id),
    FOREIGN KEY (tenant_id, source_version_id)
        REFERENCES contract_versions(tenant_id, id)
);

CREATE INDEX obligations_due_idx ON obligations (tenant_id, next_action_at)
    WHERE status IN ('CONFIRMED', 'DUE', 'OVERDUE');

CREATE TABLE idempotency_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    response_status integer,
    response_body jsonb,
    resource_id uuid,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, operation, idempotency_key)
);

CREATE TABLE outbox_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    event_type text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    payload jsonb NOT NULL,
    request_id text,
    trace_id text,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0
);

CREATE INDEX outbox_unpublished_idx ON outbox_events (occurred_at)
    WHERE published_at IS NULL;

CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    actor_id uuid,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    outcome text NOT NULL CHECK (outcome IN ('SUCCEEDED', 'DENIED', 'FAILED')),
    request_id text,
    trace_id text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_events_lookup_idx
    ON audit_events (tenant_id, resource_type, resource_id, occurred_at DESC);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'contracts', 'contract_versions', 'ingestion_jobs', 'contract_chunks',
        'review_cases', 'review_findings', 'approval_steps', 'obligations',
        'idempotency_records', 'outbox_events', 'audit_events'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING '
            || '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid) '
            || 'WITH CHECK '
            || '(tenant_id = NULLIF(current_setting(''app.tenant_id'', true), '''')::uuid)',
            table_name
        );
    END LOOP;
END $$;

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants
    USING (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
