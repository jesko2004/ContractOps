from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text

from contractops.application.events import EventAdminService
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.events import DeadLetterStage, DeliveryReservation
from contractops.infrastructure.postgres import (
    Database,
    PostgresEventAdminRepository,
    PostgresWorkerEventStore,
    WorkerDatabase,
)

RUNTIME_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_DATABASE_URL")
ADMIN_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")
WORKER_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_WORKER_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not RUNTIME_DATABASE_URL or not ADMIN_DATABASE_URL or not WORKER_DATABASE_URL,
    reason="PostgreSQL event integration URLs are not configured",
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
        roles=frozenset({Role.TENANT_ADMIN}),
        department_ids=frozenset(),
        data_scope=DataScope.TENANT,
    )


def _insert_event(database: Database, actor: ActorContext) -> UUID:
    event_id = uuid4()
    with database.transaction(actor) as connection:
        connection.execute(
            text(
                """
                INSERT INTO outbox_events (
                    id, tenant_id, event_type, aggregate_type, aggregate_id, payload
                ) VALUES (
                    :id, :tenant_id, 'approval.step.approved',
                    'approval_step', :aggregate_id, '{}'::jsonb
                )
                """
            ),
            {"id": event_id, "tenant_id": actor.tenant_id, "aggregate_id": uuid4()},
        )
    return event_id


def test_rolled_back_transaction_does_not_leave_outbox_event() -> None:
    assert RUNTIME_DATABASE_URL is not None
    tenant_id = _tenant()
    actor = _actor(tenant_id)
    database = Database(RUNTIME_DATABASE_URL)
    event_id = uuid4()
    try:
        with (
            pytest.raises(RuntimeError, match="force rollback"),
            database.transaction(actor) as connection,
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO outbox_events (
                        id, tenant_id, event_type, aggregate_type, aggregate_id, payload
                    ) VALUES (
                        :id, :tenant_id, 'test.rollback', 'test', :aggregate_id, '{}'::jsonb
                    )
                    """
                ),
                {"id": event_id, "tenant_id": tenant_id, "aggregate_id": uuid4()},
            )
            raise RuntimeError("force rollback")
        with database.transaction(actor) as connection:
            count = connection.execute(
                text("SELECT count(*) FROM outbox_events WHERE id = :id"), {"id": event_id}
            ).scalar_one()
        assert count == 0
    finally:
        database.dispose()


def test_delivery_deduplication_dead_letter_query_and_manual_replay() -> None:
    assert RUNTIME_DATABASE_URL is not None
    assert WORKER_DATABASE_URL is not None
    actor = _actor(_tenant())
    runtime_database = Database(RUNTIME_DATABASE_URL)
    worker_database = WorkerDatabase(WORKER_DATABASE_URL)
    store = PostgresWorkerEventStore(worker_database)
    admin = EventAdminService(PostgresEventAdminRepository(runtime_database))
    try:
        event_id = _insert_event(runtime_database, actor)
        claimed = store.claim_outbox("publisher-a", batch_size=500, lease_seconds=30)
        event = next(item for item in claimed if item.id == event_id)
        store.mark_published(event.id, "1-0")

        assert store.reserve_delivery(
            event,
            channel="LOG",
            destination="application-log",
            worker_id="consumer-a",
            lease_seconds=30,
        ) is DeliveryReservation.ACQUIRED
        store.mark_delivery_succeeded(
            event, channel="LOG", destination="application-log"
        )
        assert store.reserve_delivery(
            event,
            channel="LOG",
            destination="application-log",
            worker_id="consumer-b",
            lease_seconds=30,
        ) is DeliveryReservation.DELIVERED

        with worker_database.transaction() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO notification_deliveries (
                        tenant_id, event_id, channel, destination, max_attempts
                    ) VALUES (:tenant_id, :event_id, 'WEBHOOK', :destination, 1)
                    """
                ),
                {
                    "tenant_id": actor.tenant_id,
                    "event_id": event.id,
                    "destination": "https://example.invalid/hook",
                },
            )
        assert store.reserve_delivery(
            event,
            channel="WEBHOOK",
            destination="https://example.invalid/hook",
            worker_id="consumer-a",
            lease_seconds=30,
        ) is DeliveryReservation.ACQUIRED
        assert store.mark_delivery_failed(
            event,
            channel="WEBHOOK",
            destination="https://example.invalid/hook",
            error=RuntimeError("endpoint unavailable"),
            retry_delay=timedelta(seconds=1),
        ) is True

        letters = admin.list_dead_letters(actor)
        assert len(letters) == 1
        assert letters[0].stage is DeadLetterStage.DELIVERY
        replayed = admin.replay_dead_letter(actor, letters[0].id)
        assert replayed.resolved_at is not None
        assert replayed.replay_count == 1
        with runtime_database.transaction(actor) as connection:
            republish_state = connection.execute(
                text(
                    """
                    SELECT published_at, stream_message_id, attempt_count
                    FROM outbox_events WHERE id = :event_id
                    """
                ),
                {"event_id": event.id},
            ).one()
        assert republish_state.published_at is None
        assert republish_state.stream_message_id is None
        assert republish_state.attempt_count == 0
        assert store.reserve_delivery(
            event,
            channel="WEBHOOK",
            destination="https://example.invalid/hook",
            worker_id="consumer-b",
            lease_seconds=30,
        ) is DeliveryReservation.ACQUIRED
    finally:
        worker_database.dispose()
        runtime_database.dispose()
