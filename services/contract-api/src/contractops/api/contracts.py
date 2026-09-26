from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field, StringConstraints

from contractops.api.dependencies import authenticated_actor, get_contract_service
from contractops.application.contracts import (
    AddContractVersionCommand,
    ContractService,
    CreateContractCommand,
)
from contractops.context import ActorContext
from contractops.domain.contract import Contract, ContractVersion

router = APIRouter(prefix="/contracts", tags=["contracts"])
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
Currency = Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Z]{3}$")]
ContentHash = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[a-f0-9]{64}$")]


class CreateContractRequest(BaseModel):
    contract_number: str | None = Field(default=None, min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    contract_type: str = Field(min_length=1, max_length=100)
    counterparty_name: str | None = Field(default=None, max_length=300)
    department_id: UUID
    amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: Currency | None = None
    valid_from: date | None = None
    valid_until: date | None = None


class AddContractVersionRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=150)
    size_bytes: int = Field(ge=0, le=5_000_000_000)
    content_hash: ContentHash


class ContractVersionResponse(BaseModel):
    id: UUID
    contract_id: UUID
    version_number: int
    status: str
    object_key: str
    file_name: str
    media_type: str
    size_bytes: int
    content_hash: str
    created_by: UUID
    created_at: datetime

    @classmethod
    def from_domain(cls, value: ContractVersion) -> ContractVersionResponse:
        return cls(
            id=value.id,
            contract_id=value.contract_id,
            version_number=value.version_number,
            status=value.status.value,
            object_key=value.object_key,
            file_name=value.file_name,
            media_type=value.media_type,
            size_bytes=value.size_bytes,
            content_hash=value.content_hash,
            created_by=value.created_by,
            created_at=value.created_at,
        )


class ContractResponse(BaseModel):
    id: UUID
    contract_number: str | None
    title: str
    contract_type: str
    counterparty_name: str | None
    department_id: UUID
    amount: Decimal | None
    currency: str | None
    valid_from: date | None
    valid_until: date | None
    status: str
    current_version_id: UUID | None
    state_version: int
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    versions: list[ContractVersionResponse]

    @classmethod
    def from_domain(cls, value: Contract) -> ContractResponse:
        return cls(
            id=value.id,
            contract_number=value.contract_number,
            title=value.title,
            contract_type=value.contract_type,
            counterparty_name=value.counterparty_name,
            department_id=value.department_id,
            amount=value.amount,
            currency=value.currency,
            valid_from=value.valid_from,
            valid_until=value.valid_until,
            status=value.status.value,
            current_version_id=value.current_version_id,
            state_version=value.state_version,
            created_by=value.created_by,
            created_at=value.created_at,
            updated_at=value.updated_at,
            versions=[ContractVersionResponse.from_domain(item) for item in value.versions],
        )


@router.post("", response_model=ContractResponse, status_code=status.HTTP_201_CREATED)
def create_contract(
    payload: CreateContractRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ContractService, Depends(get_contract_service)],
) -> ContractResponse:
    contract, replayed = service.create_contract(
        actor,
        CreateContractCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ContractResponse.from_domain(contract)


@router.get("/{contract_id}", response_model=ContractResponse)
def get_contract(
    contract_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ContractService, Depends(get_contract_service)],
) -> ContractResponse:
    return ContractResponse.from_domain(service.get_contract(actor, contract_id))


@router.post(
    "/{contract_id}/versions",
    response_model=ContractVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_contract_version(
    contract_id: UUID,
    payload: AddContractVersionRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[ContractService, Depends(get_contract_service)],
) -> ContractVersionResponse:
    version, replayed = service.add_version(
        actor,
        contract_id,
        AddContractVersionCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return ContractVersionResponse.from_domain(version)
