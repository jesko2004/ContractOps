from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from contractops.application.obligations import (
    CreateObligationCommand,
    ObligationScheduler,
    ObligationService,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.obligation import ObligationType, RiskSeverity
from contractops.infrastructure.postgres import (
    Database,
    PostgresObligationRepository,
    PostgresSchedulerObligationStore,
    WorkerDatabase,
)

RUNTIME_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_DATABASE_URL")
ADMIN_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")
WORKER_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_WORKER_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not RUNTIME_DATABASE_URL or not ADMIN_DATABASE_URL or not WORKER_DATABASE_URL,
    reason="PostgreSQL obligation integration URLs are not configured",
)


def _tenant() -> UUID:
    tenant_id = uuid4()
    assert ADMIN_DATABASE_URL is not None
    with psycopg.connect(ADMIN_DATABASE_URL) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s)",
            (tenant_id, f"Tenant {tenant_id}"),
        )
    return tenant_id


def _actor(tenant_id: UUID) -> ActorContext:
    return ActorContext(
        tenant_id=tenant_id,
        user_id=uuid4(),
        roles=frozenset({Role.CONTRACT_OWNER, Role.TENANT_ADMIN}),
        department_ids=frozenset(),
        data_scope=DataScope.TENANT,
    )


def _approved_contract(database: Database, actor: ActorContext) -> UUID:
    contract_id = uuid4()
    with database.transaction(actor) as connection:
        connection.execute(
            text(
                """
                INSERT INTO contracts (
                    id, tenant_id, contract_number, title, contract_type,
                    department_id, status, state_version, created_by
                ) VALUES (
                    :id, :tenant_id, :number, 'Office services agreement',
                    'SERVICE', :department_id, 'APPROVED', 0, :created_by
                )
                """
            ),
            {
                "id": contract_id,
                "tenant_id": actor.tenant_id,
                "number": f"CT-{uuid4().hex[:12]}",
                "department_id": uuid4(),
                "created_by": actor.user_id,
            },
        )
    return contract_id


def test_due_scan_is_deduplicated_and_termination_cancels_future_work() -> None:
    assert RUNTIME_DATABASE_URL is not None
    assert WORKER_DATABASE_URL is not None
    runtime = Database(RUNTIME_DATABASE_URL)
    worker = WorkerDatabase(WORKER_DATABASE_URL)
    actor = _actor(_tenant())
    repository = PostgresObligationRepository(runtime)
    service = ObligationService(repository)
    scheduler = ObligationScheduler(
        PostgresSchedulerObligationStore(worker), worker_id="scheduler-test"
    )
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        contract_id = _approved_contract(runtime, actor)
        version, replayed = service.activate_contract(
            actor,
            contract_id,
            expected_version=0,
            idempotency_key=f"activate-{uuid4()}",
        )
        assert (version, replayed) == (1, False)
        obligation, _ = service.create_obligation(
            actor,
            contract_id,
            CreateObligationCommand(
                obligation_type=ObligationType.PAYMENT,
                title="Pay supplier",
                owner_id=actor.user_id,
                due_at=now - timedelta(days=3),
                grace_period_seconds=86_400,
                reminder_seconds_before=0,
            ),
            idempotency_key=f"obligation-{uuid4()}",
        )
        assert scheduler.run_once(now) == 1

        with runtime.transaction(actor) as connection:
            risk = connection.execute(
                text(
                    "SELECT severity, occurrence_count FROM risk_events "
                    "WHERE obligation_id = :id AND status <> 'RESOLVED'"
                ),
                {"id": obligation.id},
            ).mappings().one()
            assert risk["severity"] == RiskSeverity.CRITICAL.value
            assert risk["occurrence_count"] == 1
            assert connection.execute(
                text("SELECT count(*) FROM obligation_reminders WHERE obligation_id = :id"),
                {"id": obligation.id},
            ).scalar_one() == 1

            original_schedule = obligation.next_action_at
            connection.execute(
                text(
                    "UPDATE obligations SET next_action_at = :scheduled_at, "
                    "lease_owner = NULL, lease_until = NULL WHERE id = :id"
                ),
                {"id": obligation.id, "scheduled_at": original_schedule},
            )
        assert scheduler.run_once(now) == 1
        with runtime.transaction(actor) as connection:
            assert connection.execute(
                text("SELECT count(*) FROM obligation_reminders WHERE obligation_id = :id"),
                {"id": obligation.id},
            ).scalar_one() == 1

        cancelled, replayed = service.terminate_contract(
            actor,
            contract_id,
            expected_version=1,
            reason="contract terminated by owner",
            idempotency_key=f"terminate-{uuid4()}",
        )
        assert replayed is False
        assert cancelled == 1
        assert scheduler.run_once(now + timedelta(days=10)) == 0
    finally:
        worker.dispose()
        runtime.dispose()
