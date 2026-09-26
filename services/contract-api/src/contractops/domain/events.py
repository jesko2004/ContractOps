from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class DeadLetterStage(StrEnum):
    PUBLISH = "PUBLISH"
    DELIVERY = "DELIVERY"


class DeliveryReservation(StrEnum):
    ACQUIRED = "ACQUIRED"
    BUSY = "BUSY"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: UUID
    tenant_id: UUID
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: UUID
    payload: dict[str, Any]
    request_id: str | None
    trace_id: str | None
    occurred_at: datetime
    attempt_count: int = 0

    @property
    def idempotency_key(self) -> str:
        return f"event:{self.id}"


@dataclass(frozen=True, slots=True)
class StreamMessage:
    message_id: str
    event_id: UUID


@dataclass(frozen=True, slots=True)
class DeadLetter:
    id: UUID
    tenant_id: UUID
    event_id: UUID
    delivery_id: UUID | None
    stage: DeadLetterStage
    channel: str | None
    destination: str | None
    error_code: str
    error_message: str
    payload: dict[str, Any]
    failed_at: datetime
    replay_count: int
    last_replayed_at: datetime | None
    resolved_at: datetime | None
