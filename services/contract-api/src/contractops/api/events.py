from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from contractops.api.dependencies import authenticated_actor, get_event_admin_service
from contractops.application.events import EventAdminService
from contractops.context import ActorContext
from contractops.domain.events import DeadLetter

router = APIRouter(tags=["event-operations"])


class DeadLetterResponse(BaseModel):
    id: UUID
    event_id: UUID
    delivery_id: UUID | None
    stage: str
    channel: str | None
    destination: str | None
    error_code: str
    error_message: str
    payload: dict[str, Any]
    failed_at: datetime
    replay_count: int
    last_replayed_at: datetime | None
    resolved_at: datetime | None

    @classmethod
    def from_domain(cls, value: DeadLetter) -> DeadLetterResponse:
        return cls(
            id=value.id,
            event_id=value.event_id,
            delivery_id=value.delivery_id,
            stage=value.stage.value,
            channel=value.channel,
            destination=value.destination,
            error_code=value.error_code,
            error_message=value.error_message,
            payload=value.payload,
            failed_at=value.failed_at,
            replay_count=value.replay_count,
            last_replayed_at=value.last_replayed_at,
            resolved_at=value.resolved_at,
        )


@router.get("/event-dead-letters", response_model=list[DeadLetterResponse])
def list_dead_letters(
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[EventAdminService, Depends(get_event_admin_service)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[DeadLetterResponse]:
    return [
        DeadLetterResponse.from_domain(item)
        for item in service.list_dead_letters(actor, limit=limit)
    ]


@router.post(
    "/event-dead-letters/{dead_letter_id}:replay",
    response_model=DeadLetterResponse,
)
def replay_dead_letter(
    dead_letter_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[EventAdminService, Depends(get_event_admin_service)],
) -> DeadLetterResponse:
    return DeadLetterResponse.from_domain(service.replay_dead_letter(actor, dead_letter_id))
