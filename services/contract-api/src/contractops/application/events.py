from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from contractops.context import ActorContext, Role
from contractops.domain.events import (
    DeadLetter,
    DeliveryReservation,
    OutboxEvent,
    StreamMessage,
)
from contractops.errors import ContractOpsError


class EventStream(Protocol):
    def ensure_group(self) -> None: ...

    def publish(self, event: OutboxEvent) -> str: ...

    def read(self, consumer_name: str, *, count: int, block_ms: int) -> Sequence[StreamMessage]: ...

    def claim_stale(
        self, consumer_name: str, *, min_idle_ms: int, count: int
    ) -> Sequence[StreamMessage]: ...

    def acknowledge(self, message_id: str) -> None: ...


class NotificationAdapter(Protocol):
    @property
    def channel(self) -> str: ...

    @property
    def destination(self) -> str: ...

    def send(self, event: OutboxEvent, *, idempotency_key: str) -> None: ...


class WorkerEventStore(Protocol):
    def claim_outbox(
        self, worker_id: str, *, batch_size: int, lease_seconds: int
    ) -> Sequence[OutboxEvent]: ...

    def mark_published(self, event_id: UUID, stream_message_id: str) -> None: ...

    def mark_publish_failed(
        self, event: OutboxEvent, *, error: Exception, retry_delay: timedelta
    ) -> bool: ...

    def get_event(self, event_id: UUID) -> OutboxEvent | None: ...

    def reserve_delivery(
        self,
        event: OutboxEvent,
        *,
        channel: str,
        destination: str,
        worker_id: str,
        lease_seconds: int,
    ) -> DeliveryReservation: ...

    def mark_delivery_succeeded(
        self, event: OutboxEvent, *, channel: str, destination: str
    ) -> None: ...

    def mark_delivery_failed(
        self,
        event: OutboxEvent,
        *,
        channel: str,
        destination: str,
        error: Exception,
        retry_delay: timedelta,
    ) -> bool: ...


class EventAdminRepository(Protocol):
    def list_dead_letters(
        self, actor: ActorContext, *, limit: int = 100
    ) -> Sequence[DeadLetter]: ...

    def replay_dead_letter(self, actor: ActorContext, dead_letter_id: UUID) -> DeadLetter: ...


class OutboxPublisher:
    def __init__(
        self,
        store: WorkerEventStore,
        stream: EventStream,
        *,
        worker_id: str,
        batch_size: int = 50,
        lease_seconds: int = 30,
    ) -> None:
        self._store = store
        self._stream = stream
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds

    def run_once(self) -> int:
        published = 0
        for event in self._store.claim_outbox(
            self._worker_id,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
        ):
            try:
                message_id = self._stream.publish(event)
            except Exception as exc:
                delay = timedelta(seconds=min(300, 2 ** min(event.attempt_count, 8)))
                self._store.mark_publish_failed(event, error=exc, retry_delay=delay)
            else:
                self._store.mark_published(event.id, message_id)
                published += 1
        return published


class NotificationConsumer:
    def __init__(
        self,
        store: WorkerEventStore,
        stream: EventStream,
        adapters: Sequence[NotificationAdapter],
        *,
        consumer_name: str,
        batch_size: int = 20,
        lease_seconds: int = 30,
        claim_idle_ms: int = 30_000,
    ) -> None:
        self._store = store
        self._stream = stream
        self._adapters = tuple(adapters)
        self._consumer_name = consumer_name
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._claim_idle_ms = claim_idle_ms

    def run_once(self, *, block_ms: int = 1_000) -> int:
        self._stream.ensure_group()
        messages = list(
            self._stream.claim_stale(
                self._consumer_name,
                min_idle_ms=self._claim_idle_ms,
                count=self._batch_size,
            )
        )
        if len(messages) < self._batch_size:
            messages.extend(
                self._stream.read(
                    self._consumer_name,
                    count=self._batch_size - len(messages),
                    block_ms=block_ms,
                )
            )
        acknowledged = 0
        seen_message_ids: set[str] = set()
        for message in messages:
            if message.message_id in seen_message_ids:
                continue
            seen_message_ids.add(message.message_id)
            if self._process(message):
                self._stream.acknowledge(message.message_id)
                acknowledged += 1
        return acknowledged

    def _process(self, message: StreamMessage) -> bool:
        event = self._store.get_event(message.event_id)
        if event is None:
            return True
        all_terminal = True
        for adapter in self._adapters:
            reservation = self._store.reserve_delivery(
                event,
                channel=adapter.channel,
                destination=adapter.destination,
                worker_id=self._consumer_name,
                lease_seconds=self._lease_seconds,
            )
            if reservation in {DeliveryReservation.DELIVERED, DeliveryReservation.DEAD}:
                continue
            if reservation is DeliveryReservation.BUSY:
                all_terminal = False
                continue
            try:
                adapter.send(event, idempotency_key=event.idempotency_key)
            except Exception as exc:
                dead = self._store.mark_delivery_failed(
                    event,
                    channel=adapter.channel,
                    destination=adapter.destination,
                    error=exc,
                    retry_delay=timedelta(
                        seconds=min(300, 2 ** min(event.attempt_count + 1, 8))
                    ),
                )
                all_terminal = all_terminal and dead
            else:
                self._store.mark_delivery_succeeded(
                    event,
                    channel=adapter.channel,
                    destination=adapter.destination,
                )
        return all_terminal


class EventAdminService:
    def __init__(self, repository: EventAdminRepository) -> None:
        self._repository = repository

    @staticmethod
    def _authorize(actor: ActorContext) -> None:
        if not actor.has_any_role(Role.TENANT_ADMIN):
            raise ContractOpsError(
                code="event_admin_forbidden",
                message="tenant administrator role is required",
                status_code=403,
            )

    def list_dead_letters(self, actor: ActorContext, *, limit: int = 100) -> Sequence[DeadLetter]:
        self._authorize(actor)
        return self._repository.list_dead_letters(actor, limit=limit)

    def replay_dead_letter(self, actor: ActorContext, dead_letter_id: UUID) -> DeadLetter:
        self._authorize(actor)
        return self._repository.replay_dead_letter(actor, dead_letter_id)
