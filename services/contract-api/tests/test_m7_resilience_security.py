from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from contractops.application.events import NotificationConsumer, OutboxPublisher
from contractops.domain.events import DeliveryReservation, OutboxEvent, StreamMessage
from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import parse_document
from contractops.infrastructure.notifications import WebhookNotificationAdapter
from contractops.observability import JsonFormatter


def _event() -> OutboxEvent:
    return OutboxEvent(
        id=uuid4(),
        tenant_id=uuid4(),
        event_type="approval.step.approved",
        schema_version=1,
        aggregate_type="approval_step",
        aggregate_id=uuid4(),
        payload={"contract_id": str(uuid4())},
        request_id="req-m7",
        trace_id="0123456789abcdef0123456789abcdef",
        occurred_at=datetime.now(UTC),
        attempt_count=1,
    )


class ResilienceStore:
    def __init__(self, event: OutboxEvent) -> None:
        self.event = event
        self.published: list[tuple[UUID, str]] = []
        self.publish_failures = 0
        self.delivery_failures = 0
        self.delivered = False

    def claim_outbox(
        self, worker_id: str, *, batch_size: int, lease_seconds: int
    ) -> Sequence[OutboxEvent]:
        del worker_id, batch_size, lease_seconds
        return (self.event,)

    def mark_published(self, event_id: UUID, stream_message_id: str) -> None:
        self.published.append((event_id, stream_message_id))

    def mark_publish_failed(
        self, event: OutboxEvent, *, error: Exception, retry_delay: timedelta
    ) -> bool:
        del event, error, retry_delay
        self.publish_failures += 1
        return False

    def get_event(self, event_id: UUID) -> OutboxEvent | None:
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
        return DeliveryReservation.DELIVERED if self.delivered else DeliveryReservation.ACQUIRED

    def mark_delivery_succeeded(
        self, event: OutboxEvent, *, channel: str, destination: str
    ) -> None:
        del event, channel, destination
        self.delivered = True

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
        return False


class RecoveringStream:
    def __init__(self, event: OutboxEvent, *, fail_publish: bool = False) -> None:
        self.event = event
        self.fail_publish = fail_publish
        self.stale = [StreamMessage("1-0", event.id)]
        self.acknowledged: list[str] = []

    def ensure_group(self) -> None:
        return None

    def publish(self, event: OutboxEvent) -> str:
        del event
        if self.fail_publish:
            raise ConnectionError("redis paused")
        return "2-0"

    def read(self, consumer_name: str, *, count: int, block_ms: int) -> Sequence[StreamMessage]:
        del consumer_name, count, block_ms
        return ()

    def claim_stale(
        self, consumer_name: str, *, min_idle_ms: int, count: int
    ) -> Sequence[StreamMessage]:
        del consumer_name, min_idle_ms
        values = self.stale[:count]
        self.stale = self.stale[count:]
        return values

    def acknowledge(self, message_id: str) -> None:
        self.acknowledged.append(message_id)


class RecoveringAdapter:
    channel = "TEST"
    destination = "m7-fault-injection"

    def __init__(self) -> None:
        self.available = False
        self.attempts = 0

    def send(self, event: OutboxEvent, *, idempotency_key: str) -> None:
        del event, idempotency_key
        self.attempts += 1
        if not self.available:
            raise ConnectionError("notification endpoint unavailable")


def test_redis_pause_leaves_outbox_retriable_then_publishes_once() -> None:
    event = _event()
    store = ResilienceStore(event)
    stream = RecoveringStream(event, fail_publish=True)
    publisher = OutboxPublisher(store, stream, worker_id="publisher-m7")

    assert publisher.run_once() == 0
    assert store.publish_failures == 1
    assert store.published == []

    stream.fail_publish = False
    assert publisher.run_once() == 1
    assert store.published == [(event.id, "2-0")]


def test_worker_reclaims_stale_message_and_recovers_notification_failure() -> None:
    event = _event()
    store = ResilienceStore(event)
    stream = RecoveringStream(event)
    adapter = RecoveringAdapter()
    consumer = NotificationConsumer(
        store,
        stream,
        (adapter,),
        consumer_name="worker-after-crash",
        claim_idle_ms=1,
    )

    assert consumer.run_once(block_ms=1) == 0
    assert store.delivery_failures == 1
    assert stream.acknowledged == []

    adapter.available = True
    stream.stale.append(StreamMessage("1-0", event.id))
    assert consumer.run_once(block_ms=1) == 1
    assert adapter.attempts == 2
    assert stream.acknowledged == ["1-0"]


def test_docx_zip_bomb_is_rejected_before_xml_parsing() -> None:
    package = io.BytesIO()
    xml = b"<w:document>" + (b"A" * (2 * 1024 * 1024)) + b"</w:document>"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)

    with pytest.raises(ContractOpsError) as captured:
        parse_document(
            package.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    assert captured.value.code == "document_parse_failed"


def test_webhook_blocks_private_network_destination() -> None:
    adapter = WebhookNotificationAdapter("http://127.0.0.1:8080/internal")

    with pytest.raises(ValueError, match="public network"):
        adapter.send(_event(), idempotency_key="m7-private-network")


def test_structured_logs_ignore_token_and_payload_extra_fields() -> None:
    record = logging.LogRecord(
        name="contractops.security-test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="request rejected",
        args=(),
        exc_info=None,
    )
    record.authorization = "Bearer secret-token"  # type: ignore[attr-defined]
    record.payload = {"contract_text": "secret clause"}  # type: ignore[attr-defined]

    rendered = JsonFormatter().format(record)
    value = json.loads(rendered)

    assert value["message"] == "request rejected"
    assert "secret-token" not in rendered
    assert "secret clause" not in rendered
