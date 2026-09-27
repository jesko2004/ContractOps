ALTER TABLE outbox_events
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN locked_by text,
    ADD COLUMN locked_until timestamptz,
    ADD COLUMN stream_message_id text,
    ADD COLUMN last_error text,
    ADD COLUMN dead_lettered_at timestamptz;

ALTER TABLE outbox_events
    ADD CONSTRAINT outbox_events_tenant_id_id_key UNIQUE (tenant_id, id);

DROP INDEX outbox_unpublished_idx;
CREATE INDEX outbox_publish_claim_idx
    ON outbox_events (next_attempt_at, occurred_at)
    WHERE published_at IS NULL AND dead_lettered_at IS NULL;

CREATE TABLE notification_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    event_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('LOG', 'WEBHOOK')),
    destination text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'DELIVERING', 'RETRY_WAIT', 'DELIVERED', 'DEAD')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    last_error text,
    delivered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id),
    UNIQUE (tenant_id, event_id, channel, destination),
    FOREIGN KEY (tenant_id, event_id)
        REFERENCES outbox_events(tenant_id, id) ON DELETE CASCADE,
    CHECK (
        (status = 'DELIVERING' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'DELIVERING')
    ),
    CHECK (
        (status = 'DELIVERED' AND delivered_at IS NOT NULL)
        OR (status <> 'DELIVERED')
    )
);

CREATE INDEX notification_deliveries_retry_idx
    ON notification_deliveries (next_attempt_at, created_at)
    WHERE status IN ('PENDING', 'RETRY_WAIT', 'DELIVERING');

CREATE TABLE event_dead_letters (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    event_id uuid NOT NULL,
    delivery_id uuid,
    stage text NOT NULL CHECK (stage IN ('PUBLISH', 'DELIVERY')),
    channel text,
    destination text,
    error_code text NOT NULL,
    error_message text NOT NULL,
    payload jsonb NOT NULL,
    failed_at timestamptz NOT NULL DEFAULT now(),
    replay_count integer NOT NULL DEFAULT 0 CHECK (replay_count >= 0),
    last_replayed_at timestamptz,
    resolved_at timestamptz,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, event_id)
        REFERENCES outbox_events(tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, delivery_id)
        REFERENCES notification_deliveries(tenant_id, id) ON DELETE CASCADE,
    CHECK (jsonb_typeof(payload) = 'object'),
    CHECK (
        (stage = 'PUBLISH' AND delivery_id IS NULL)
        OR (stage = 'DELIVERY' AND delivery_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX event_dead_letters_active_publish_idx
    ON event_dead_letters (tenant_id, event_id)
    WHERE stage = 'PUBLISH' AND resolved_at IS NULL;
CREATE UNIQUE INDEX event_dead_letters_active_delivery_idx
    ON event_dead_letters (tenant_id, delivery_id)
    WHERE stage = 'DELIVERY' AND resolved_at IS NULL;
CREATE INDEX event_dead_letters_lookup_idx
    ON event_dead_letters (tenant_id, resolved_at, failed_at DESC);

DO $$
DECLARE
    table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['notification_deliveries', 'event_dead_letters']
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
        GRANT SELECT, UPDATE ON outbox_events TO contractops_worker;
        GRANT SELECT, INSERT, UPDATE ON notification_deliveries TO contractops_worker;
        GRANT SELECT, INSERT, UPDATE ON event_dead_letters TO contractops_worker;
    END IF;
END $$;
