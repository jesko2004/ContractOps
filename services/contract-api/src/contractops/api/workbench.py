from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field

from contractops.api.approvals import ApprovalInstanceResponse
from contractops.api.contracts import ContractResponse
from contractops.api.dependencies import authenticated_actor
from contractops.context import ActorContext
from contractops.errors import ContractOpsError
from contractops.infrastructure.contract_workbench import ContractWorkbench

router = APIRouter(prefix="/workspace", tags=["contract-workbench"])
Actor = Annotated[ActorContext, Depends(authenticated_actor)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def service(request: Request) -> ContractWorkbench:
    value = getattr(request.app.state, "contract_workbench", None)
    if not isinstance(value, ContractWorkbench):
        raise ContractOpsError(
            code="workspace_unavailable", message="当前未配置业务工作台。", status_code=503
        )
    return value


Service = Annotated[ContractWorkbench, Depends(service)]


class LinkRequest(BaseModel):
    document_id: UUID
    expected_version: int = Field(ge=0)


class ConfirmRequest(BaseModel):
    expected_version: int = Field(ge=0)
    candidate_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=50)


class ReviewRequest(BaseModel):
    policy_base_id: UUID


class SubmitRequest(BaseModel):
    expected_version: int = Field(ge=0)
    report_id: UUID


@router.get("/contracts", response_model=list[ContractResponse])
def list_contracts(
    actor: Actor,
    backend: Service,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[ContractResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [
        ContractResponse.from_domain(contract)
        for contract in backend.list_contracts(actor, limit, offset)
    ]


@router.get("/contracts/{contract_id}")
def details(
    contract_id: UUID, actor: Actor, backend: Service, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    contract = backend.contracts.get_contract(actor, contract_id)
    link = backend.link(actor, contract_id)
    return dict(
        contract=ContractResponse.from_domain(contract).model_dump(mode="json"),
        document_id=str(link["document_id"]) if link else None,
        confirmation=link["confirmation"] if link else {},
        candidates=backend.candidates(actor, contract_id) if link else [],
        approvals=[
            ApprovalInstanceResponse.from_domain(i).model_dump(mode="json")
            for i in backend.history(actor, contract_id=contract_id)
        ],
    )


@router.post("/contracts/{contract_id}/documents")
def bind(
    contract_id: UUID,
    payload: LinkRequest,
    actor: Actor,
    backend: Service,
    key: Key,
    response: Response,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return backend.bind_document(
        actor, contract_id, payload.document_id, payload.expected_version, key
    )


@router.post("/contracts/{contract_id}:confirm")
def confirm(
    contract_id: UUID, payload: ConfirmRequest, actor: Actor, backend: Service, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return backend.confirm(actor, contract_id, payload.expected_version, payload.candidate_ids)


@router.post("/contracts/{contract_id}/reviews", status_code=201)
def generate(
    contract_id: UUID, payload: ReviewRequest, actor: Actor, backend: Service, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return backend.generate_report(actor, contract_id, payload.policy_base_id)


@router.get("/contracts/{contract_id}/review")
def report(
    contract_id: UUID,
    actor: Actor,
    backend: Service,
    response: Response,
    report_id: UUID | None = None,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return backend.report(actor, contract_id, report_id)


@router.post("/contracts/{contract_id}:submit", response_model=ApprovalInstanceResponse)
def submit(
    contract_id: UUID,
    payload: SubmitRequest,
    actor: Actor,
    backend: Service,
    key: Key,
    response: Response,
) -> ApprovalInstanceResponse:
    contract = backend.require_writer(actor, contract_id)
    if contract.status.value == "DRAFT":
        current = backend.report(actor, contract_id, payload.report_id)
        if current["status"] != "CURRENT" or current["contract_version_id"] != str(
            contract.current_version_id
        ):
            raise ContractOpsError(
                code="review_report_stale", message="预审报告已失效，请重新生成。", status_code=409
            )
    if contract.status.value == "DRAFT" and contract.state_version != payload.expected_version:
        raise ContractOpsError(
            code="contract_state_conflict", message="合同已变化，请刷新。", status_code=409
        )
    value, replayed = backend.approvals.submit_contract(actor, contract_id, idempotency_key=key)
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    response.headers["Cache-Control"] = "no-store"
    return ApprovalInstanceResponse.from_domain(value)


@router.get("/contracts/{contract_id}/diff")
def diff(contract_id: UUID, actor: Actor, backend: Service, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return backend.diff(actor, contract_id)


@router.get("/approval-history", response_model=list[ApprovalInstanceResponse])
def history(
    actor: Actor, backend: Service, response: Response, handled: bool = False
) -> list[ApprovalInstanceResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [
        ApprovalInstanceResponse.from_domain(i) for i in backend.history(actor, handled=handled)
    ]
