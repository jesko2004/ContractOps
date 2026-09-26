from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field, StringConstraints

from contractops.api.dependencies import authenticated_actor, get_approval_service
from contractops.application.approvals import (
    ApprovalService,
    CreateApprovalPolicyCommand,
    StepActionCommand,
    TransferStepCommand,
)
from contractops.context import ActorContext, Role
from contractops.domain.approval import (
    ApprovalDecision,
    ApprovalInstance,
    ApprovalPolicy,
    ApprovalStep,
    ApprovalStepTemplate,
    PolicyCondition,
)

router = APIRouter(tags=["approvals"])
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
Currency = Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Z]{3}$")]


class PolicyConditionRequest(BaseModel):
    contract_types: list[str] = Field(default_factory=list, max_length=50)
    department_ids: list[UUID] = Field(default_factory=list, max_length=100)
    minimum_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    maximum_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    currency: Currency | None = None


class ApprovalStepTemplateRequest(BaseModel):
    step_order: int = Field(ge=1, le=100)
    name: str = Field(min_length=1, max_length=150)
    required_role: Role
    minimum_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)


class CreateApprovalPolicyRequest(BaseModel):
    policy_key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=0, ge=-1000, le=1000)
    condition: PolicyConditionRequest = Field(default_factory=PolicyConditionRequest)
    steps: list[ApprovalStepTemplateRequest] = Field(min_length=1, max_length=100)


class StepActionRequest(BaseModel):
    expected_version: int = Field(ge=0)
    comment: str | None = Field(default=None, max_length=2000)


class DecideStepRequest(StepActionRequest):
    decision: ApprovalDecision


class TransferStepRequest(StepActionRequest):
    target_user_id: UUID


class ApprovalPolicyResponse(BaseModel):
    id: UUID
    policy_key: str
    version_number: int
    name: str
    priority: int
    status: str
    condition: dict[str, Any]
    steps: list[dict[str, Any]]
    created_by: UUID
    created_at: datetime
    published_by: UUID | None
    published_at: datetime | None

    @classmethod
    def from_domain(cls, value: ApprovalPolicy) -> ApprovalPolicyResponse:
        return cls(
            id=value.id,
            policy_key=value.policy_key,
            version_number=value.version_number,
            name=value.name,
            priority=value.priority,
            status=value.status.value,
            condition=value.condition.snapshot(),
            steps=[step.snapshot() for step in value.steps],
            created_by=value.created_by,
            created_at=value.created_at,
            published_by=value.published_by,
            published_at=value.published_at,
        )


class ApprovalStepResponse(BaseModel):
    id: UUID
    instance_id: UUID
    step_order: int
    name: str
    required_role: str
    status: str
    assigned_to: UUID | None
    state_version: int
    decided_by: UUID | None
    decision_comment: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, value: ApprovalStep) -> ApprovalStepResponse:
        return cls(
            id=value.id,
            instance_id=value.instance_id,
            step_order=value.step_order,
            name=value.name,
            required_role=value.required_role.value,
            status=value.status.value,
            assigned_to=value.assigned_to,
            state_version=value.state_version,
            decided_by=value.decided_by,
            decision_comment=value.decision_comment,
            decided_at=value.decided_at,
            created_at=value.created_at,
            updated_at=value.updated_at,
        )


class ApprovalInstanceResponse(BaseModel):
    id: UUID
    contract_id: UUID
    contract_version_id: UUID
    policy_id: UUID
    policy_snapshot: dict[str, Any]
    status: str
    state_version: int
    requested_by: UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    steps: list[ApprovalStepResponse]

    @classmethod
    def from_domain(cls, value: ApprovalInstance) -> ApprovalInstanceResponse:
        return cls(
            id=value.id,
            contract_id=value.contract_id,
            contract_version_id=value.contract_version_id,
            policy_id=value.policy_id,
            policy_snapshot=value.policy_snapshot,
            status=value.status.value,
            state_version=value.state_version,
            requested_by=value.requested_by,
            created_at=value.created_at,
            updated_at=value.updated_at,
            completed_at=value.completed_at,
            steps=[ApprovalStepResponse.from_domain(item) for item in value.steps],
        )


@router.post(
    "/approval-policies",
    response_model=ApprovalPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_policy(
    payload: CreateApprovalPolicyRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalPolicyResponse:
    condition = PolicyCondition(
        contract_types=tuple(sorted(set(payload.condition.contract_types))),
        department_ids=tuple(sorted(set(payload.condition.department_ids), key=str)),
        minimum_amount=payload.condition.minimum_amount,
        maximum_amount=payload.condition.maximum_amount,
        currency=payload.condition.currency,
    )
    steps = tuple(
        ApprovalStepTemplate(
            step_order=item.step_order,
            name=item.name,
            required_role=item.required_role,
            minimum_amount=item.minimum_amount,
        )
        for item in payload.steps
    )
    policy, replayed = service.create_policy(
        actor,
        CreateApprovalPolicyCommand(
            policy_key=payload.policy_key,
            name=payload.name,
            priority=payload.priority,
            condition=condition,
            steps=steps,
        ),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ApprovalPolicyResponse.from_domain(policy)


@router.post(
    "/approval-policies/{policy_id}:publish",
    response_model=ApprovalPolicyResponse,
)
def publish_policy(
    policy_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalPolicyResponse:
    return ApprovalPolicyResponse.from_domain(service.publish_policy(actor, policy_id))


@router.post(
    "/contracts/{contract_id}:submit",
    response_model=ApprovalInstanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_contract(
    contract_id: UUID,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalInstanceResponse:
    instance, replayed = service.submit_contract(
        actor, contract_id, idempotency_key=idempotency_key
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ApprovalInstanceResponse.from_domain(instance)


@router.get("/approval-tasks", response_model=list[ApprovalStepResponse])
def list_tasks(
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> list[ApprovalStepResponse]:
    return [ApprovalStepResponse.from_domain(item) for item in service.list_tasks(actor)]


@router.get(
    "/approval-instances/{instance_id}", response_model=ApprovalInstanceResponse
)
def get_instance(
    instance_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalInstanceResponse:
    return ApprovalInstanceResponse.from_domain(service.get_instance(actor, instance_id))


@router.post("/approval-steps/{step_id}:claim", response_model=ApprovalStepResponse)
def claim_step(
    step_id: UUID,
    payload: StepActionRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalStepResponse:
    step, replayed = service.claim_step(
        actor,
        step_id,
        StepActionCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ApprovalStepResponse.from_domain(step)


@router.post("/approval-steps/{step_id}:decide", response_model=ApprovalStepResponse)
def decide_step(
    step_id: UUID,
    payload: DecideStepRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalStepResponse:
    step, replayed = service.decide_step(
        actor,
        step_id,
        payload.decision,
        StepActionCommand(expected_version=payload.expected_version, comment=payload.comment),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ApprovalStepResponse.from_domain(step)


@router.post("/approval-steps/{step_id}:transfer", response_model=ApprovalStepResponse)
def transfer_step(
    step_id: UUID,
    payload: TransferStepRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ApprovalService, Depends(get_approval_service)],
) -> ApprovalStepResponse:
    step, replayed = service.transfer_step(
        actor,
        step_id,
        TransferStepCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ApprovalStepResponse.from_domain(step)
