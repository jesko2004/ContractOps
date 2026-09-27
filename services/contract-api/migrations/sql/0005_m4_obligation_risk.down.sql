DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
        REVOKE SELECT ON contracts FROM contractops_worker;
        REVOKE SELECT, UPDATE ON obligations FROM contractops_worker;
        REVOKE SELECT, INSERT, UPDATE ON risk_events FROM contractops_worker;
        REVOKE SELECT, INSERT, UPDATE ON obligation_reminders FROM contractops_worker;
        REVOKE INSERT ON outbox_events FROM contractops_worker;
    END IF;
END $$;

DROP TABLE IF EXISTS obligation_reminders CASCADE;
DROP TABLE IF EXISTS risk_events CASCADE;

DROP INDEX IF EXISTS obligations_schedule_idx;
DROP INDEX IF EXISTS obligations_contract_idx;

ALTER TABLE obligations
    DROP CONSTRAINT IF EXISTS obligations_type_m4_check,
    DROP CONSTRAINT IF EXISTS obligations_grace_period_m4_check,
    DROP CONSTRAINT IF EXISTS obligations_status_m4_check,
    DROP CONSTRAINT IF EXISTS obligations_state_version_m4_check,
    DROP CONSTRAINT IF EXISTS obligations_completion_m4_check,
    DROP CONSTRAINT IF EXISTS obligations_next_action_m4_check;

UPDATE obligations
SET status = CASE status
    WHEN 'PLANNED' THEN 'DRAFT'
    WHEN 'ACTIVE' THEN 'CONFIRMED'
    WHEN 'WAIVED' THEN 'CANCELLED'
    ELSE status
END;

UPDATE obligations AS obligation
SET source_version_id = (
    SELECT version.id
    FROM contract_versions AS version
    WHERE version.tenant_id = obligation.tenant_id
      AND version.contract_id = obligation.contract_id
    ORDER BY version.version_number DESC
    LIMIT 1
)
WHERE source_version_id IS NULL;

ALTER TABLE obligations
    ALTER COLUMN status SET DEFAULT 'DRAFT',
    ALTER COLUMN owner_id DROP NOT NULL,
    ALTER COLUMN due_at DROP NOT NULL,
    ALTER COLUMN source_version_id SET NOT NULL,
    DROP COLUMN grace_period_seconds,
    DROP COLUMN evidence_reference,
    DROP COLUMN lease_owner,
    DROP COLUMN lease_until,
    DROP COLUMN completed_by,
    DROP COLUMN completed_at,
    DROP COLUMN cancel_reason,
    DROP COLUMN created_by,
    ADD CONSTRAINT obligations_status_check CHECK (status IN (
        'DRAFT', 'CONFIRMED', 'DUE', 'COMPLETED', 'OVERDUE', 'CANCELLED'
    ));

ALTER TABLE obligations RENAME COLUMN owner_id TO responsible_user_id;

CREATE INDEX obligations_due_idx ON obligations (tenant_id, next_action_at)
    WHERE status IN ('CONFIRMED', 'DUE', 'OVERDUE');
