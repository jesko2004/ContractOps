"""Index bounded maintenance and approval pagination; grant runtime schema inspection."""

from alembic import op

revision = "20260929_0008"
down_revision = "20260928_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX idempotency_expiry_idx ON idempotency_records (expires_at, id)")
    op.execute(
        "CREATE INDEX approval_tasks_page_idx ON approval_workflow_steps "
        "(tenant_id, created_at, id) WHERE status IN ('READY', 'CLAIMED')"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
                GRANT SELECT ON alembic_version TO contractops_worker;
                GRANT SELECT, UPDATE, DELETE ON idempotency_records TO contractops_worker;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_app') THEN
                GRANT SELECT ON alembic_version TO contractops_app;
                GRANT DELETE ON idempotency_records TO contractops_app;
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX approval_tasks_page_idx")
    op.execute("DROP INDEX idempotency_expiry_idx")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
                REVOKE SELECT ON alembic_version FROM contractops_worker;
                REVOKE SELECT, UPDATE, DELETE ON idempotency_records FROM contractops_worker;
            END IF;
        END $$;
    """)
