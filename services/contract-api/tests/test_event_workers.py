from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from contractops.application.events import NotificationConsumer, OutboxPublisher
from contractops.domain.events import DeliveryReservation, OutboxEvent, StreamMessage


def _event(*, attempt_count: int = 1) -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        tenant_id=uuid4(),
        event_type="approval.step.approved",
        schema_version=1,
        aggregate_type="approval_step",
        aggregate_id=uuid4(),
        payload={"instance_id": str(uuid4())},
        request_id="req-test",
        trace_id="trace-test",
        occurred_at=datetime.now(UTC),
        attempt_count=attempt_count,
    )


class FakeStream:
    def __init__(self, messages: Sequence[StreamMessage] = ()) -> None:
        self.messages = list(messages)
        self.published: list[OutboxEvent] = []
        self.acknowledged: list[str] = []

    def ensure_group(self) -> None:
        return None

    def publish(self, event: OutboxEvent) -> str:
        self.published.append(event)
        return f"{len(self.published)}-0"

    def read(self, consumer_name: str, *, count: int, block_ms: int) -> Sequence[StreamMessage]:
        del consumer_name, block_ms
        values = self.messages[:count]
        self.messages = self.messages[count:]
        return values

    def claim_stale(
        self, consumer_name: str, *, min_idle_ms: int, count: int
    ) -> Sequence[StreamMessage]:
        del consumer_name, min_idle_ms, count
        return ()

    def acknowledge(self, message_id: str) -> None:
        self.acknowledged.append(message_id)


class FakeAdapter:
    channel = "LOG"
    destination = "test-log"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    def send(self, event: OutboxEvent, *, idempotency_key: str) -> None:
        if self.fail:
            raise RuntimeError("adapter unavailable")
        self.sent.append(idempotency_key)


class FakeStore:
    def __init__(self, event: OutboxEvent) -> None:
        self.event = event
        self.delivery_status = DeliveryReservation.ACQUIRED
        self.published: list[tuple[object, str]] = []
        self.publish_failures = 0
        self.delivery_failures = 0

    def claim_outbox(
        self, worker_id: str, *, batch_size: int, lease_seconds: int
    ) -> Sequence[OutboxEvent]:
        del worker_id, batch_size, lease_seconds
        return (self.event,)

    def mark_published(self, event_id: object, stream_message_id: str) -> None:
        self.published.append((event_id, stream_message_id))

    def mark_publish_failed(
        self, event: OutboxEvent, *, error: Exception, retry_delay: timedelta
    ) -> bool:
        del event, error, retry_delay
        self.publish_failures += 1
        return False

    def get_event(self, event_id: object) -> OutboxEvent | None:
        return self.event if event_id == self.event.id else None

    def reserve_delivery(
        self,
        event: OutboxEvent,
        *,
        channel: str,
        destination: str,
        worker_id: str,
        lease_seconds: int,
    ) -> DeliveryReservation:
        del event, channel, destination, worker_id, lease_seconds
        return self.delivery_status

    def mark_delivery_succeeded(
        self, event: OutboxEvent, *, channel: str, destination: str
    ) -> None:
        del event, channel, destination
        self.delivery_status = DeliveryReservation.DELIVERED

    def mark_delivery_failed(
        self,
        event: OutboxEvent,
        *,
        channel: str,
        destination: str,
        error: Exception,
        retry_delay: timedelta,
    ) -> bool:
        del event, channel, destination, error, retry_delay
        self.delivery_failures += 1
        return True


def test_publisher_marks_outbox_only_after_stream_accepts_event() -> None:
    event = _event()
    store = FakeStore(event)
    stream = FakeStream()

    assert OutboxPublisher(store, stream, worker_id="publisher").run_once() == 1

    assert stream.published == [event]
    assert store.published == [(event.id, "1-0")]


def test_duplicate_stream_delivery_does_not_send_duplicate_notification() -> None:
    event = _event()
    message = StreamMessage("1-0", event.id)
    store = FakeStore(event)
    adapter = FakeAdapter()
    first_stream = FakeStream((message,))
    first = NotificationConsumer(store, first_stream, (adapter,), consumer_name="worker-a")
    assert first.run_once(block_ms=1) == 1

    duplicate_stream = FakeStream((StreamMessage("2-0", event.id),))
    duplicate = NotificationConsumer(
        store, duplicate_stream, (adapter,), consumer_name="worker-b"
    )
    assert duplicate.run_once(block_ms=1) == 1

    assert adapter.sent == [event.idempotency_key]
    assert duplicate_stream.acknowledged == ["2-0"]


def test_busy_delivery_is_left_pending_for_worker_crash_recovery() -> None:
    event = _event()
    message = StreamMessage("1-0", event.id)
    store = FakeStore(event)
    store.delivery_status = DeliveryReservation.BUSY
    adapter = FakeAdapter()
    stream = FakeStream((message,))
    consumer = NotificationConsumer(store, stream, (adapter,), consumer_name="worker-b")

    assert consumer.run_once(block_ms=1) == 0
    assert stream.acknowledged == []

    store.delivery_status = DeliveryReservation.ACQUIRED
    stream.messages.append(message)
    assert consumer.run_once(block_ms=1) == 1
    assert adapter.sent == [event.idempotency_key]


def test_dead_delivery_is_acknowledged_after_retry_budget_is_exhausted() -> None:
    event = _event()
    stream = FakeStream((StreamMessage("1-0", event.id),))
    store = FakeStore(event)
    adapter = FakeAdapter(fail=True)
    consumer = NotificationConsumer(store, stream, (adapter,), consumer_name="worker-a")

    assert consumer.run_once(block_ms=1) == 1
    assert store.delivery_failures == 1
    assert stream.acknowledged == ["1-0"]
