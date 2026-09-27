from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, Field

from contractops.api.dependencies import authenticated_actor, get_obligation_service
from contractops.application.obligations import (
    CompleteObligationCommand,
    CreateObligationCommand,
    ObligationService,
    RiskActionCommand,
)
from contractops.context import ActorContext
from contractops.domain.obligation import (
    Obligation,
    ObligationType,
    RiskAction,
    RiskEvent,
)

router = APIRouter(tags=["obligations"])
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]


class CreateObligationRequest(BaseModel):
    obligation_type: ObligationType
    title: str = Field(min_length=1, max_length=200)
    owner_id: UUID
    due_at: datetime
    grace_period_seconds: int = Field(default=86_400, ge=60, le=31_536_000)
    reminder_seconds_before: int = Field(default=604_800, ge=0, le=31_536_000)


class CompleteObligationRequest(BaseModel):
    expected_version: int = Field(ge=0)
    evidence_reference: str = Field(min_length=1, max_length=500)


class RiskActionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    deferred_until: datetime | None = None


class TerminateContractRequest(BaseModel):
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)


class ActivateContractRequest(BaseModel):
    expected_version: int = Field(ge=0)


class ObligationResponse(BaseModel):
    id: UUID
    contract_id: UUID
    obligation_type: str
    title: str
    owner_id: UUID
    due_at: datetime
    grace_period_seconds: int
    status: str
    state_version: int
    evidence_reference: str | None
    next_action_at: datetime | None
    completed_at: datetime | None
    cancel_reason: str | None

    @classmethod
    def from_domain(cls, value: Obligation) -> ObligationResponse:
        return cls(
            id=value.id,
            contract_id=value.contract_id,
            obligation_type=value.obligation_type.value,
            title=value.title,
            owner_id=value.owner_id,
            due_at=value.due_at,
            grace_period_seconds=value.grace_period_seconds,
            status=value.status.value,
            state_version=value.state_version,
            evidence_reference=value.evidence_reference,
            next_action_at=value.next_action_at,
            completed_at=value.completed_at,
            cancel_reason=value.cancel_reason,
        )


class RiskEventResponse(BaseModel):
    id: UUID
    contract_id: UUID
    obligation_id: UUID
    risk_type: str
    severity: str
    status: str
    owner_id: UUID
    state_version: int
    occurrence_count: int
    first_detected_at: datetime
    last_detected_at: datetime
    deferred_until: datetime | None
    resolution: str | None
    resolved_at: datetime | None

    @classmethod
    def from_domain(cls, value: RiskEvent) -> RiskEventResponse:
        return cls(
            id=value.id,
            contract_id=value.contract_id,
            obligation_id=value.obligation_id,
            risk_type=value.risk_type.value,
            severity=value.severity.value,
            status=value.status.value,
            owner_id=value.owner_id,
            state_version=value.state_version,
            occurrence_count=value.occurrence_count,
            first_detected_at=value.first_detected_at,
            last_detected_at=value.last_detected_at,
            deferred_until=value.deferred_until,
            resolution=value.resolution,
            resolved_at=value.resolved_at,
        )


class TerminateContractResponse(BaseModel):
    contract_id: UUID
    cancelled_obligations: int


class ActivateContractResponse(BaseModel):
    contract_id: UUID
    state_version: int


@router.post(
    "/contracts/{contract_id}/obligations",
    response_model=ObligationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_obligation(
    contract_id: UUID,
    payload: CreateObligationRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> ObligationResponse:
    obligation, replayed = service.create_obligation(
        actor,
        contract_id,
        CreateObligationCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ObligationResponse.from_domain(obligation)


@router.post("/contracts/{contract_id}:activate", response_model=ActivateContractResponse)
def activate_contract(
    contract_id: UUID,
    payload: ActivateContractRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> ActivateContractResponse:
    state_version, replayed = service.activate_contract(
        actor,
        contract_id,
        expected_version=payload.expected_version,
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ActivateContractResponse(contract_id=contract_id, state_version=state_version)


@router.post("/obligations/{obligation_id}:complete", response_model=ObligationResponse)
def complete_obligation(
    obligation_id: UUID,
    payload: CompleteObligationRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> ObligationResponse:
    obligation, replayed = service.complete_obligation(
        actor,
        obligation_id,
        CompleteObligationCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ObligationResponse.from_domain(obligation)


@router.get("/risk-events", response_model=list[RiskEventResponse])
def list_risk_events(
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[RiskEventResponse]:
    return [RiskEventResponse.from_domain(item) for item in service.list_risks(actor, limit=limit)]


def _act_on_risk(
    risk_id: UUID,
    action: RiskAction,
    payload: RiskActionRequest,
    response: Response,
    idempotency_key: str,
    actor: ActorContext,
    service: ObligationService,
) -> RiskEventResponse:
    risk, replayed = service.act_on_risk(
        actor,
        risk_id,
        RiskActionCommand(action=action, **payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return RiskEventResponse.from_domain(risk)


@router.post("/risk-events/{risk_id}:acknowledge", response_model=RiskEventResponse)
def acknowledge_risk(
    risk_id: UUID,
    payload: RiskActionRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> RiskEventResponse:
    return _act_on_risk(
        risk_id, RiskAction.ACKNOWLEDGE, payload, response, idempotency_key, actor, service
    )


@router.post("/risk-events/{risk_id}:defer", response_model=RiskEventResponse)
def defer_risk(
    risk_id: UUID,
    payload: RiskActionRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> RiskEventResponse:
    return _act_on_risk(
        risk_id, RiskAction.DEFER, payload, response, idempotency_key, actor, service
    )


@router.post("/risk-events/{risk_id}:resolve", response_model=RiskEventResponse)
def resolve_risk(
    risk_id: UUID,
    payload: RiskActionRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> RiskEventResponse:
    return _act_on_risk(
        risk_id, RiskAction.RESOLVE, payload, response, idempotency_key, actor, service
    )


@router.post("/contracts/{contract_id}:terminate", response_model=TerminateContractResponse)
def terminate_contract(
    contract_id: UUID,
    payload: TerminateContractRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ObligationService, Depends(get_obligation_service)],
) -> TerminateContractResponse:
    cancelled, replayed = service.terminate_contract(
        actor,
        contract_id,
        expected_version=payload.expected_version,
        reason=payload.reason,
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return TerminateContractResponse(
        contract_id=contract_id,
        cancelled_obligations=cancelled,
    )
