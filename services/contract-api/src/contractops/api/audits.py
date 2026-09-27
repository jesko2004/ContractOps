from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from contractops.api.dependencies import authenticated_actor, get_audit_service
from contractops.application.audits import AuditService
from contractops.context import ActorContext
from contractops.domain.audit import AuditCategory, AuditEvent, RuntimeFailure

router = APIRouter(tags=["audit"])


class AuditEventResponse(BaseModel):
    id: UUID
    category: str
    action: str
    resource_type: str
    resource_id: UUID | None
    outcome: str
    request_id: str | None
    trace_id: str | None
    metadata: dict[str, Any]
    occurred_at: datetime

    @classmethod
    def from_domain(cls, value: AuditEvent) -> AuditEventResponse:
        return cls(
            id=value.id,
            category=value.category.value,
            action=value.action,
            resource_type=value.resource_type,
            resource_id=value.resource_id,
            outcome=value.outcome.value,
            request_id=value.request_id,
            trace_id=value.trace_id,
            metadata=value.metadata,
            occurred_at=value.occurred_at,
        )


class RuntimeFailureResponse(BaseModel):
    occurred_at: datetime
    failure_type: str
    resource_type: str
    resource_id: UUID | None
    request_id: str | None
    trace_id: str | None
    summary: str

    @classmethod
    def from_domain(cls, value: RuntimeFailure) -> RuntimeFailureResponse:
        return cls(
            occurred_at=value.occurred_at,
            failure_type=value.failure_type,
            resource_type=value.resource_type,
            resource_id=value.resource_id,
            request_id=value.request_id,
            trace_id=value.trace_id,
            summary=value.summary,
        )


@router.get("/audit-events", response_model=list[AuditEventResponse])
def list_audit_events(
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[AuditService, Depends(get_audit_service)],
    request_id: Annotated[str | None, Query(max_length=100)] = None,
    trace_id: Annotated[str | None, Query(min_length=32, max_length=32)] = None,
    category: AuditCategory | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AuditEventResponse]:
    return [
        AuditEventResponse.from_domain(value)
        for value in service.list_events(
            actor,
            request_id=request_id,
            trace_id=trace_id,
            category=category,
            limit=limit,
        )
    ]


@router.get("/admin/runtime/failures", response_model=list[RuntimeFailureResponse])
def list_runtime_failures(
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[AuditService, Depends(get_audit_service)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[RuntimeFailureResponse]:
    return [
        RuntimeFailureResponse.from_domain(value)
        for value in service.list_failures(actor, limit=limit)
    ]
