ALTER TABLE audit_events
    ADD COLUMN category text NOT NULL DEFAULT 'BUSINESS'
        CHECK (category IN ('OPERATION', 'BUSINESS', 'SECURITY'));

CREATE INDEX audit_events_request_idx
    ON audit_events (tenant_id, request_id, occurred_at DESC)
    WHERE request_id IS NOT NULL;
CREATE INDEX audit_events_trace_idx
    ON audit_events (tenant_id, trace_id, occurred_at DESC)
    WHERE trace_id IS NOT NULL;
CREATE INDEX audit_events_failure_idx
    ON audit_events (tenant_id, occurred_at DESC)
    WHERE outcome = 'FAILED';

CREATE OR REPLACE FUNCTION reject_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only' USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER audit_events_append_only
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
        GRANT INSERT ON audit_events TO contractops_worker;
    END IF;
END $$;
