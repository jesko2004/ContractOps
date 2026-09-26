from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from contractops.domain.events import OutboxEvent
from contractops.infrastructure.redis_streams import RedisEventStream

REDIS_URL = os.getenv("CONTRACTOPS_INTEGRATION_REDIS_URL")

pytestmark = pytest.mark.skipif(not REDIS_URL, reason="Redis integration URL is not configured")


def test_consumer_group_reads_claims_and_acknowledges_messages() -> None:
    assert REDIS_URL is not None
    suffix = uuid4().hex
    stream = RedisEventStream(
        REDIS_URL,
        stream_name=f"contractops.test.{suffix}",
        group_name=f"contractops.test.group.{suffix}",
    )
    event = OutboxEvent(
        id=uuid4(),
        tenant_id=uuid4(),
        event_type="approval.step.approved",
        schema_version=1,
        aggregate_type="approval_step",
        aggregate_id=uuid4(),
        payload={},
        request_id=None,
        trace_id=None,
        occurred_at=datetime.now(UTC),
    )
    try:
        stream.ensure_group()
        message_id = stream.publish(event)
        received = stream.read("worker-a", count=1, block_ms=10)
        assert [(item.message_id, item.event_id) for item in received] == [
            (message_id, event.id)
        ]

        recovered = stream.claim_stale("worker-b", min_idle_ms=0, count=1)
        assert [(item.message_id, item.event_id) for item in recovered] == [
            (message_id, event.id)
        ]
        stream.acknowledge(message_id)
        assert stream.claim_stale("worker-c", min_idle_ms=0, count=1) == ()
    finally:
        stream.close()
