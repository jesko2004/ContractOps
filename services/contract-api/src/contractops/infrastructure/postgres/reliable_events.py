from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from contractops.context import ActorContext
from contractops.domain.events import (
    DeadLetter,
    DeadLetterStage,
    DeliveryReservation,
    OutboxEvent,
)
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database, WorkerDatabase


def _safe_error(error: Exception) -> tuple[str, str]:
    return type(error).__name__[:100], str(error)[:2000] or "operation failed"


def _event_from_row(row: Mapping[str, Any] | RowMapping) -> OutboxEvent:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return OutboxEvent(
        id=cast(UUID, row["id"]),
        tenant_id=cast(UUID, row["tenant_id"]),
        event_type=cast(str, row["event_type"]),
        schema_version=cast(int, row["schema_version"]),
        aggregate_type=cast(str, row["aggregate_type"]),
        aggregate_id=cast(UUID, row["aggregate_id"]),
        payload=cast(dict[str, Any], payload),
        request_id=cast(str | None, row["request_id"]),
        trace_id=cast(str | None, row["trace_id"]),
        occurred_at=row["occurred_at"],
        attempt_count=cast(int, row["attempt_count"]),
    )


def _dead_letter_from_row(row: Mapping[str, Any] | RowMapping) -> DeadLetter:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return DeadLetter(
        id=cast(UUID, row["id"]),
        tenant_id=cast(UUID, row["tenant_id"]),
        event_id=cast(UUID, row["event_id"]),
        delivery_id=cast(UUID | None, row["delivery_id"]),
        stage=DeadLetterStage(row["stage"]),
        channel=cast(str | None, row["channel"]),
        destination=cast(str | None, row["destination"]),
        error_code=cast(str, row["error_code"]),
        error_message=cast(str, row["error_message"]),
        payload=cast(dict[str, Any], payload),
        failed_at=row["failed_at"],
        replay_count=cast(int, row["replay_count"]),
        last_replayed_at=row["last_replayed_at"],
        resolved_at=row["resolved_at"],
    )


class PostgresWorkerEventStore:
    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database

    def claim_outbox(
        self, worker_id: str, *, batch_size: int, lease_seconds: int
    ) -> Sequence[OutboxEvent]:
        with self._database.transaction() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        WITH candidates AS (
                            SELECT id
                            FROM outbox_events
                            WHERE published_at IS NULL
                              AND dead_lettered_at IS NULL
                              AND next_attempt_at <= now()
                              AND (locked_until IS NULL OR locked_until < now())
                            ORDER BY occurred_at, id
                            FOR UPDATE SKIP LOCKED
                            LIMIT :batch_size
                        )
                        UPDATE outbox_events AS event
                        SET locked_by = :worker_id,
                            locked_until = now() + make_interval(secs => :lease_seconds),
                            attempt_count = event.attempt_count + 1,
                            last_error = NULL
                        FROM candidates
                        WHERE event.id = candidates.id
                        RETURNING event.*
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "batch_size": batch_size,
                        "lease_seconds": lease_seconds,
                    },
                )
                .mappings()
                .all()
            )
            return tuple(_event_from_row(row) for row in rows)

    def mark_published(self, event_id: UUID, stream_message_id: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                text(
                    """
                    UPDATE outbox_events
                    SET published_at = now(), stream_message_id = :stream_message_id,
                        locked_by = NULL, locked_until = NULL, last_error = NULL
                    WHERE id = :event_id AND published_at IS NULL
                    """
                ),
                {"event_id": event_id, "stream_message_id": stream_message_id},
            )

    def mark_publish_failed(
        self, event: OutboxEvent, *, error: Exception, retry_delay: timedelta
    ) -> bool:
        error_code, error_message = _safe_error(error)
        with self._database.transaction() as connection:
            current = (
                connection.execute(
                    text(
                        """
                        SELECT attempt_count, max_attempts
                        FROM outbox_events WHERE id = :event_id FOR UPDATE
                        """
                    ),
                    {"event_id": event.id},
                )
                .mappings()
                .one()
            )
            dead = bool(current["attempt_count"] >= current["max_attempts"])
            if dead:
                connection.execute(
                    text(
                        """
                        UPDATE outbox_events
                        SET dead_lettered_at = now(), locked_by = NULL, locked_until = NULL,
                            last_error = :error_message
                        WHERE id = :event_id
                        """
                    ),
                    {"event_id": event.id, "error_message": error_message},
                )
                self._insert_dead_letter(
                    connection,
                    event,
                    stage=DeadLetterStage.PUBLISH,
                    error_code=error_code,
                    error_message=error_message,
                )
            else:
                connection.execute(
                    text(
                        """
                        UPDATE outbox_events
                        SET locked_by = NULL, locked_until = NULL,
                            next_attempt_at = now() + CAST(:retry_delay AS interval),
                            last_error = :error_message
                        WHERE id = :event_id
                        """
                    ),
                    {
                        "event_id": event.id,
                        "retry_delay": retry_delay,
                        "error_message": error_message,
                    },
                )
            return dead

    def get_event(self, event_id: UUID) -> OutboxEvent | None:
        with self._database.transaction() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM outbox_events WHERE id = :event_id"),
                    {"event_id": event_id},
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None else _event_from_row(row)

    def reserve_delivery(
        self,
        event: OutboxEvent,
        *,
        channel: str,
        destination: str,
        worker_id: str,
        lease_seconds: int,
    ) -> DeliveryReservation:
        with self._database.transaction() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO notification_deliveries (
                        tenant_id, event_id, channel, destination
                    ) VALUES (:tenant_id, :event_id, :channel, :destination)
                    ON CONFLICT (tenant_id, event_id, channel, destination) DO NOTHING
                    """
                ),
                {
                    "tenant_id": event.tenant_id,
                    "event_id": event.id,
                    "channel": channel,
                    "destination": destination,
                },
            )
            claimed = (
                connection.execute(
                    text(
                        """
                        UPDATE notification_deliveries
                        SET status = 'DELIVERING', lease_owner = :worker_id,
                            lease_until = now() + make_interval(secs => :lease_seconds),
                            attempt_count = attempt_count + 1, updated_at = now()
                        WHERE tenant_id = :tenant_id AND event_id = :event_id
                          AND channel = :channel AND destination = :destination
                          AND (
                              (status IN ('PENDING', 'RETRY_WAIT') AND next_attempt_at <= now())
                              OR (status = 'DELIVERING' AND lease_until < now())
                          )
                        RETURNING id
                        """
                    ),
                    {
                        "tenant_id": event.tenant_id,
                        "event_id": event.id,
                        "channel": channel,
                        "destination": destination,
                        "worker_id": worker_id,
                        "lease_seconds": lease_seconds,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if claimed is not None:
                return DeliveryReservation.ACQUIRED
            status: str = cast(
                str,
                connection.execute(
                    text(
                        """
                        SELECT status FROM notification_deliveries
                        WHERE tenant_id = :tenant_id AND event_id = :event_id
                          AND channel = :channel AND destination = :destination
                        """
                    ),
                    {
                        "tenant_id": event.tenant_id,
                        "event_id": event.id,
                        "channel": channel,
                        "destination": destination,
                    },
                ).scalar_one(),
            )
            if status == "DELIVERED":
                return DeliveryReservation.DELIVERED
            if status == "DEAD":
                return DeliveryReservation.DEAD
            return DeliveryReservation.BUSY

    def mark_delivery_succeeded(
        self, event: OutboxEvent, *, channel: str, destination: str
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                text(
                    """
                    UPDATE notification_deliveries
                    SET status = 'DELIVERED', delivered_at = now(),
                        lease_owner = NULL, lease_until = NULL, last_error = NULL,
                        updated_at = now()
                    WHERE tenant_id = :tenant_id AND event_id = :event_id
                      AND channel = :channel AND destination = :destination
                      AND status = 'DELIVERING'
                    """
                ),
                {
                    "tenant_id": event.tenant_id,
                    "event_id": event.id,
                    "channel": channel,
                    "destination": destination,
                },
            )

    def mark_delivery_failed(
        self,
        event: OutboxEvent,
        *,
        channel: str,
        destination: str,
        error: Exception,
        retry_delay: timedelta,
    ) -> bool:
        error_code, error_message = _safe_error(error)
        with self._database.transaction() as connection:
            delivery = (
                connection.execute(
                    text(
                        """
                        SELECT id, attempt_count, max_attempts
                        FROM notification_deliveries
                        WHERE tenant_id = :tenant_id AND event_id = :event_id
                          AND channel = :channel AND destination = :destination
                        FOR UPDATE
                        """
                    ),
                    {
                        "tenant_id": event.tenant_id,
                        "event_id": event.id,
                        "channel": channel,
                        "destination": destination,
                    },
                )
                .mappings()
                .one()
            )
            dead = bool(delivery["attempt_count"] >= delivery["max_attempts"])
            if dead:
                connection.execute(
                    text(
                        """
                        UPDATE notification_deliveries
                        SET status = 'DEAD', lease_owner = NULL, lease_until = NULL,
                            last_error = :error_message, updated_at = now()
                        WHERE id = :delivery_id
                        """
                    ),
                    {"delivery_id": delivery["id"], "error_message": error_message},
                )
                self._insert_dead_letter(
                    connection,
                    event,
                    stage=DeadLetterStage.DELIVERY,
                    error_code=error_code,
                    error_message=error_message,
                    delivery_id=delivery["id"],
                    channel=channel,
                    destination=destination,
                )
            else:
                connection.execute(
                    text(
                        """
                        UPDATE notification_deliveries
                        SET status = 'RETRY_WAIT', lease_owner = NULL, lease_until = NULL,
                            next_attempt_at = now() + CAST(:retry_delay AS interval),
                            last_error = :error_message, updated_at = now()
                        WHERE id = :delivery_id
                        """
                    ),
                    {
                        "delivery_id": delivery["id"],
                        "retry_delay": retry_delay,
                        "error_message": error_message,
                    },
                )
            return dead

    @staticmethod
    def _insert_dead_letter(
        connection: Any,
        event: OutboxEvent,
        *,
        stage: DeadLetterStage,
        error_code: str,
        error_message: str,
        delivery_id: UUID | None = None,
        channel: str | None = None,
        destination: str | None = None,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO event_dead_letters (
                    tenant_id, event_id, delivery_id, stage, channel, destination,
                    error_code, error_message, payload
                ) VALUES (
                    :tenant_id, :event_id, :delivery_id, :stage, :channel, :destination,
                    :error_code, :error_message, CAST(:payload AS jsonb)
                )
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "tenant_id": event.tenant_id,
                "event_id": event.id,
                "delivery_id": delivery_id,
                "stage": stage.value,
                "channel": channel,
                "destination": destination,
                "error_code": error_code,
                "error_message": error_message,
                "payload": json.dumps(
                    {
                        "event_type": event.event_type,
                        "schema_version": event.schema_version,
                        "aggregate_type": event.aggregate_type,
                        "aggregate_id": str(event.aggregate_id),
                        "payload": event.payload,
                        "request_id": event.request_id,
                        "trace_id": event.trace_id,
                    }
                ),
            },
        )


class PostgresEventAdminRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_dead_letters(
        self, actor: ActorContext, *, limit: int = 100
    ) -> Sequence[DeadLetter]:
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT * FROM event_dead_letters
                        WHERE resolved_at IS NULL
                        ORDER BY failed_at DESC, id DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
            return tuple(_dead_letter_from_row(row) for row in rows)

    def replay_dead_letter(self, actor: ActorContext, dead_letter_id: UUID) -> DeadLetter:
        with self._database.transaction(actor) as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT * FROM event_dead_letters
                        WHERE id = :dead_letter_id AND resolved_at IS NULL
                        FOR UPDATE
                        """
                    ),
                    {"dead_letter_id": dead_letter_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ContractOpsError(
                    code="dead_letter_not_found",
                    message="active dead letter was not found",
                    status_code=404,
                )
            stage = DeadLetterStage(row["stage"])
            if stage is DeadLetterStage.PUBLISH:
                connection.execute(
                    text(
                        """
                        UPDATE outbox_events
                        SET attempt_count = 0, next_attempt_at = now(),
                            locked_by = NULL, locked_until = NULL,
                            dead_lettered_at = NULL, last_error = NULL
                        WHERE id = :event_id
                        """
                    ),
                    {"event_id": row["event_id"]},
                )
            else:
                connection.execute(
                    text(
                        """
                        UPDATE notification_deliveries
                        SET status = 'RETRY_WAIT', attempt_count = 0,
                            next_attempt_at = now(), lease_owner = NULL,
                            lease_until = NULL, last_error = NULL, updated_at = now()
                        WHERE id = :delivery_id
                        """
                    ),
                    {"delivery_id": row["delivery_id"]},
                )
                connection.execute(
                    text(
                        """
                        UPDATE outbox_events
                        SET published_at = NULL, stream_message_id = NULL,
                            attempt_count = 0, next_attempt_at = now(),
                            locked_by = NULL, locked_until = NULL,
                            dead_lettered_at = NULL, last_error = NULL
                        WHERE id = :event_id
                        """
                    ),
                    {"event_id": row["event_id"]},
                )
            updated = (
                connection.execute(
                    text(
                        """
                        UPDATE event_dead_letters
                        SET replay_count = replay_count + 1,
                            last_replayed_at = now(), resolved_at = now()
                        WHERE id = :dead_letter_id
                        RETURNING *
                        """
                    ),
                    {"dead_letter_id": dead_letter_id},
                )
                .mappings()
                .one()
            )
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        tenant_id, actor_id, action, resource_type, resource_id,
                        outcome, metadata
                    ) VALUES (
                        :tenant_id, :actor_id, 'event.dead_letter.replayed',
                        'event_dead_letter', :resource_id, 'SUCCEEDED', CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "tenant_id": actor.tenant_id,
                    "actor_id": actor.user_id,
                    "resource_id": dead_letter_id,
                    "metadata": json.dumps(
                        {"stage": stage.value, "event_id": str(row["event_id"])}
                    ),
                },
            )
            return _dead_letter_from_row(updated)
