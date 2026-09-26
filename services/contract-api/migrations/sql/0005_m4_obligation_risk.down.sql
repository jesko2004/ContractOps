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
DROP TABLE IF EXISTS obligations CASCADE;
