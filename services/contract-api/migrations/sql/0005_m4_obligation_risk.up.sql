CREATE TABLE obligations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL,
    obligation_type text NOT NULL CHECK (obligation_type IN (
        'PAYMENT', 'DELIVERY', 'ACCEPTANCE', 'RENEWAL', 'TERMINATION_NOTICE', 'OTHER'
    )),
    title text NOT NULL,
    owner_id uuid NOT NULL,
    due_at timestamptz NOT NULL,
    grace_period_seconds integer NOT NULL DEFAULT 86400 CHECK (grace_period_seconds > 0),
    status text NOT NULL DEFAULT 'PLANNED' CHECK (status IN (
        'PLANNED', 'ACTIVE', 'COMPLETED', 'OVERDUE', 'WAIVED', 'CANCELLED'
    )),
    state_version integer NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    evidence_reference text,
    next_action_at timestamptz,
    lease_owner text,
    lease_until timestamptz,
    completed_by uuid,
    completed_at timestamptz,
    cancel_reason text,
    created_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id),
    CHECK (
        (status = 'COMPLETED' AND completed_by IS NOT NULL
            AND completed_at IS NOT NULL AND evidence_reference IS NOT NULL)
        OR status <> 'COMPLETED'
    ),
    CHECK (
        (status IN ('ACTIVE', 'OVERDUE') AND next_action_at IS NOT NULL)
        OR status NOT IN ('ACTIVE', 'OVERDUE')
    )
);

CREATE INDEX obligations_schedule_idx
    ON obligations (next_action_at, due_at, id)
    WHERE status IN ('ACTIVE', 'OVERDUE');
CREATE INDEX obligations_contract_idx
    ON obligations (tenant_id, contract_id, status);

CREATE TABLE risk_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    contract_id uuid NOT NULL,
    obligation_id uuid NOT NULL,
    risk_type text NOT NULL CHECK (risk_type IN (
        'DUE_SOON', 'OVERDUE', 'MISSING_EVIDENCE'
    )),
    severity text NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    status text NOT NULL DEFAULT 'OPEN' CHECK (status IN (
        'OPEN', 'ACKNOWLEDGED', 'DEFERRED', 'RESOLVED'
    )),
    owner_id uuid NOT NULL,
    state_version integer NOT NULL DEFAULT 0 CHECK (state_version >= 0),
    occurrence_count integer NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    first_detected_at timestamptz NOT NULL,
    last_detected_at timestamptz NOT NULL,
    deferred_until timestamptz,
    resolution text,
    resolved_by uuid,
    resolved_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts(tenant_id, id),
    FOREIGN KEY (tenant_id, obligation_id) REFERENCES obligations(tenant_id, id),
    CHECK (
        (status = 'DEFERRED' AND deferred_until IS NOT NULL)
        OR status <> 'DEFERRED'
    ),
    CHECK (
        (status = 'RESOLVED' AND resolution IS NOT NULL AND resolved_at IS NOT NULL)
        OR status <> 'RESOLVED'
    )
);

CREATE UNIQUE INDEX risk_events_one_active_idx
    ON risk_events (tenant_id, obligation_id, risk_type)
    WHERE status <> 'RESOLVED';
CREATE INDEX risk_events_queue_idx
    ON risk_events (tenant_id, status, severity, last_detected_at DESC);

CREATE TABLE obligation_reminders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    obligation_id uuid NOT NULL,
    reminder_type text NOT NULL,
    scheduled_at timestamptz NOT NULL,
    outbox_event_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, obligation_id, reminder_type, scheduled_at),
    FOREIGN KEY (tenant_id, obligation_id)
        REFERENCES obligations(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, outbox_event_id)
        REFERENCES outbox_events(tenant_id, id)
);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['obligations', 'risk_events', 'obligation_reminders']
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

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
        GRANT SELECT ON contracts TO contractops_worker;
        GRANT SELECT, UPDATE ON obligations TO contractops_worker;
        GRANT SELECT, INSERT, UPDATE ON risk_events TO contractops_worker;
        GRANT SELECT, INSERT, UPDATE ON obligation_reminders TO contractops_worker;
        GRANT INSERT ON outbox_events TO contractops_worker;
    END IF;
END $$;
