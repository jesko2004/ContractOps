DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
        REVOKE INSERT ON audit_events FROM contractops_worker;
    END IF;
END $$;

DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events;
DROP FUNCTION IF EXISTS reject_audit_event_mutation();
DROP INDEX IF EXISTS audit_events_failure_idx;
DROP INDEX IF EXISTS audit_events_trace_idx;
DROP INDEX IF EXISTS audit_events_request_idx;
ALTER TABLE audit_events DROP COLUMN IF EXISTS category;
