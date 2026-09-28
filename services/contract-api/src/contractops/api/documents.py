from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, Field, StringConstraints

from contractops.api.dependencies import authenticated_actor, get_document_service
from contractops.application.contracts import AddContractVersionCommand
from contractops.application.documents import DocumentService
from contractops.context import ActorContext
from contractops.domain.contract import ContractVersion
from contractops.domain.document import DiffEntry, DocumentChunk, DocumentFinding

router = APIRouter(tags=["documents"])
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=200),
]
ContentHash = Annotated[str, StringConstraints(to_lower=True, pattern=r"^[a-f0-9]{64}$")]


class InitiateUploadRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=150)
    size_bytes: int = Field(gt=0, le=5_000_000_000)
    content_hash: ContentHash


class VersionSummary(BaseModel):
    id: UUID
    contract_id: UUID
    version_number: int
    status: str
    object_key: str
    file_name: str
    media_type: str
    size_bytes: int
    content_hash: str
    created_at: datetime

    @classmethod
    def from_domain(cls, value: ContractVersion) -> VersionSummary:
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
            created_at=value.created_at,
        )


class UploadTicketResponse(BaseModel):
    version: VersionSummary
    upload_url: str
    expires_in_seconds: int


class CompleteUploadResponse(BaseModel):
    accepted: bool = True
    replayed: bool


class ChunkResponse(BaseModel):
    id: UUID
    sequence: int
    page_number: int | None
    heading_path: list[str]
    content: str
    content_hash: str

    @classmethod
    def from_domain(cls, value: DocumentChunk) -> ChunkResponse:
        return cls(
            id=value.id,
            sequence=value.sequence,
            page_number=value.page_number,
            heading_path=list(value.heading_path),
            content=value.content,
            content_hash=value.content_hash,
        )


class FindingResponse(BaseModel):
    id: UUID
    source_chunk_id: UUID
    kind: str
    origin: str
    title: str
    normalized_value: str | None
    source_page: int | None
    source_text: str
    confidence: float
    model_name: str | None
    model_output_hash: str | None

    @classmethod
    def from_domain(cls, value: DocumentFinding) -> FindingResponse:
        return cls(
            id=value.id,
            source_chunk_id=value.source_chunk_id,
            kind=value.kind.value,
            origin=value.origin.value,
            title=value.title,
            normalized_value=value.normalized_value,
            source_page=value.source_page,
            source_text=value.source_text,
            confidence=value.confidence,
            model_name=value.model_name,
            model_output_hash=value.model_output_hash,
        )


class DiffSide(BaseModel):
    chunk_id: UUID
    sequence: int
    page_number: int | None
    source_text: str
    content_hash: str

    @classmethod
    def from_chunk(cls, value: DocumentChunk) -> DiffSide:
        return cls(
            chunk_id=value.id,
            sequence=value.sequence,
            page_number=value.page_number,
            source_text=value.content,
            content_hash=value.content_hash,
        )


class DiffEntryResponse(BaseModel):
    change: str
    old: DiffSide | None
    new: DiffSide | None

    @classmethod
    def from_domain(cls, value: DiffEntry) -> DiffEntryResponse:
        return cls(
            change=value.change,
            old=None if value.old_chunk is None else DiffSide.from_chunk(value.old_chunk),
            new=None if value.new_chunk is None else DiffSide.from_chunk(value.new_chunk),
        )


class VersionDiffResponse(BaseModel):
    contract_id: UUID
    from_version_id: UUID
    to_version_id: UUID
    entries: list[DiffEntryResponse]


@router.post(
    "/contracts/{contract_id}/uploads",
    response_model=UploadTicketResponse,
    status_code=status.HTTP_201_CREATED,
)
def initiate_upload(
    contract_id: UUID,
    payload: InitiateUploadRequest,
    idempotency_key: IdempotencyKey,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> UploadTicketResponse:
    ticket = service.initiate_upload(
        actor,
        contract_id,
        AddContractVersionCommand(**payload.model_dump()),
        idempotency_key=idempotency_key,
    )
    return UploadTicketResponse(
        version=VersionSummary.from_domain(ticket.version),
        upload_url=ticket.upload_url,
        expires_in_seconds=ticket.expires_in_seconds,
    )


@router.post(
    "/contract-versions/{version_id}:complete-upload",
    response_model=CompleteUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def complete_upload(
    version_id: UUID,
    response: Response,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> CompleteUploadResponse:
    replayed = service.complete_upload(actor, version_id)
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    return CompleteUploadResponse(replayed=replayed)


@router.get("/contract-versions/{version_id}/chunks", response_model=list[ChunkResponse])
def list_chunks(
    version_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> list[ChunkResponse]:
    return [ChunkResponse.from_domain(item) for item in service.list_chunks(actor, version_id)]


@router.get("/contract-versions/{version_id}/findings", response_model=list[FindingResponse])
def list_findings(
    version_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> list[FindingResponse]:
    return [FindingResponse.from_domain(item) for item in service.list_findings(actor, version_id)]


@router.get("/contracts/{contract_id}/version-diff", response_model=VersionDiffResponse)
def compare_versions(
    contract_id: UUID,
    from_version_id: UUID,
    to_version_id: UUID,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> VersionDiffResponse:
    value = service.diff(actor, contract_id, from_version_id, to_version_id)
    return VersionDiffResponse(
        contract_id=value.contract_id,
        from_version_id=value.from_version_id,
        to_version_id=value.to_version_id,
        entries=[DiffEntryResponse.from_domain(item) for item in value.entries],
    )
