-- The initial schema already owns the obligations table. M4 evolves it in place so
-- an upgrade keeps existing contract/version references and never drops tenant data.
DROP INDEX IF EXISTS obligations_due_idx;

ALTER TABLE obligations
    DROP CONSTRAINT IF EXISTS obligations_status_check,
    DROP CONSTRAINT IF EXISTS obligations_obligation_type_check;

ALTER TABLE obligations RENAME COLUMN responsible_user_id TO owner_id;

ALTER TABLE obligations
    ALTER COLUMN source_version_id DROP NOT NULL,
    ADD COLUMN grace_period_seconds integer NOT NULL DEFAULT 86400,
    ADD COLUMN evidence_reference text,
    ADD COLUMN lease_owner text,
    ADD COLUMN lease_until timestamptz,
    ADD COLUMN completed_by uuid,
    ADD COLUMN completed_at timestamptz,
    ADD COLUMN cancel_reason text,
    ADD COLUMN created_by uuid;

UPDATE obligations
SET obligation_type = 'OTHER'
WHERE obligation_type NOT IN (
    'PAYMENT', 'DELIVERY', 'ACCEPTANCE', 'RENEWAL', 'TERMINATION_NOTICE', 'OTHER'
);

UPDATE obligations
SET owner_id = COALESCE(owner_id, gen_random_uuid()),
    due_at = COALESCE(due_at, now()),
    status = CASE status
        WHEN 'DRAFT' THEN 'PLANNED'
        WHEN 'CONFIRMED' THEN 'ACTIVE'
        WHEN 'DUE' THEN 'ACTIVE'
        ELSE status
    END;

UPDATE obligations
SET created_by = owner_id,
    next_action_at = CASE
        WHEN status IN ('ACTIVE', 'OVERDUE') THEN COALESCE(next_action_at, due_at)
        ELSE next_action_at
    END,
    evidence_reference = CASE
        WHEN status = 'COMPLETED' THEN COALESCE(evidence_reference, source_text, 'migrated-record')
        ELSE evidence_reference
    END,
    completed_by = CASE
        WHEN status = 'COMPLETED' THEN COALESCE(completed_by, owner_id)
        ELSE completed_by
    END,
    completed_at = CASE
        WHEN status = 'COMPLETED' THEN COALESCE(completed_at, updated_at, now())
        ELSE completed_at
    END;

ALTER TABLE obligations
    ALTER COLUMN owner_id SET NOT NULL,
    ALTER COLUMN due_at SET NOT NULL,
    ALTER COLUMN created_by SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'PLANNED',
    ADD CONSTRAINT obligations_type_m4_check CHECK (obligation_type IN (
        'PAYMENT', 'DELIVERY', 'ACCEPTANCE', 'RENEWAL', 'TERMINATION_NOTICE', 'OTHER'
    )),
    ADD CONSTRAINT obligations_grace_period_m4_check CHECK (grace_period_seconds > 0),
    ADD CONSTRAINT obligations_status_m4_check CHECK (status IN (
        'PLANNED', 'ACTIVE', 'COMPLETED', 'OVERDUE', 'WAIVED', 'CANCELLED'
    )),
    ADD CONSTRAINT obligations_state_version_m4_check CHECK (state_version >= 0),
    ADD CONSTRAINT obligations_completion_m4_check CHECK (
        (status = 'COMPLETED' AND completed_by IS NOT NULL
            AND completed_at IS NOT NULL AND evidence_reference IS NOT NULL)
        OR status <> 'COMPLETED'
    ),
    ADD CONSTRAINT obligations_next_action_m4_check CHECK (
        (status IN ('ACTIVE', 'OVERDUE') AND next_action_at IS NOT NULL)
        OR status NOT IN ('ACTIVE', 'OVERDUE')
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
