from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from contractops.api.dependencies import authenticated_actor, get_knowledge_management_service
from contractops.application.knowledge_management import (
    MAX_UPLOAD_BYTES,
    KnowledgeBase,
    KnowledgeManagementService,
    ManagedChunk,
    ManagedDocument,
)
from contractops.context import ActorContext
from contractops.errors import ContractOpsError

router = APIRouter(prefix="/knowledge", tags=["knowledge-management"])
Actor = Annotated[ActorContext, Depends(authenticated_actor)]
Service = Annotated[KnowledgeManagementService, Depends(get_knowledge_management_service)]
DocumentType = Literal["CONTRACT", "POLICY", "TEMPLATE"]
DocumentStatus = Literal["PARSING", "READY", "FAILED", "ARCHIVED"]


class BaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    department_id: UUID | None = None


class BaseResponse(BaseModel):
    id: UUID
    name: str
    description: str
    department_id: UUID
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    document_count: int

    @classmethod
    def from_domain(cls, value: KnowledgeBase) -> BaseResponse:
        return cls(**asdict(value))


class ManagedDocumentResponse(BaseModel):
    id: UUID
    base_id: UUID
    version_id: UUID
    file_name: str
    document_type: str
    media_type: str
    size_bytes: int
    file_hash: str
    status: str
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    state_version: int
    chunk_count: int
    error_code: str | None
    error_message: str | None
    lease_expires_at: datetime | None
    original_available: bool
    root_id: UUID | None
    version_number: int
    is_current: bool
    parse_details: dict[str, object] | None
    searchable: bool
    can_archive: bool
    can_retry: bool
    can_upload_version: bool

    @classmethod
    def from_domain(cls, value: ManagedDocument, actor: ActorContext) -> ManagedDocumentResponse:
        manageable = KnowledgeManagementService.can_manage(actor, value)
        expired = value.lease_expires_at is not None and value.lease_expires_at <= datetime.now(UTC)
        return cls(
            **asdict(value),
            searchable=value.is_current and value.status == "READY" and value.chunk_count > 0,
            can_archive=manageable and value.status in {"READY", "FAILED"},
            can_retry=manageable
            and value.original_available
            and (value.status == "FAILED" or (value.status == "PARSING" and expired)),
            can_upload_version=manageable
            and value.is_current
            and value.status in {"READY", "FAILED"},
        )


class StateRequest(BaseModel):
    expected_version: int = Field(ge=0)


class ChunkResponse(BaseModel):
    id: UUID
    sequence: int
    page_number: int | None
    heading_path: tuple[str, ...]
    content: str
    content_hash: str
    source_pages: tuple[int, ...]
    source_id: str | None
    kind: str

    @classmethod
    def from_domain(cls, value: ManagedChunk) -> ChunkResponse:
        return cls(**asdict(value))


@router.get("/bases", response_model=list[BaseResponse])
def list_bases(response: Response, actor: Actor, service: Service) -> list[BaseResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [BaseResponse.from_domain(base) for base in service.list_bases(actor)]


@router.post("/bases", response_model=BaseResponse, status_code=201)
def create_base(payload: BaseRequest, actor: Actor, service: Service) -> BaseResponse:
    return BaseResponse.from_domain(
        service.create_base(actor, payload.name, payload.description, payload.department_id)
    )


@router.get("/bases/{base_id}/documents", response_model=list[ManagedDocumentResponse])
def list_documents(
    base_id: UUID,
    response: Response,
    actor: Actor,
    service: Service,
    query: Annotated[str, Query(max_length=200)] = "",
    document_type: DocumentType | None = None,
    status: DocumentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[ManagedDocumentResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [
        ManagedDocumentResponse.from_domain(doc, actor)
        for doc in service.documents(
            actor,
            base_id,
            query=query,
            document_type=document_type,
            status=status,
            limit=limit,
            offset=offset,
        )
    ]


@router.post("/bases/{base_id}/documents", response_model=ManagedDocumentResponse, status_code=201)
async def upload(
    base_id: UUID,
    request: Request,
    response: Response,
    actor: Actor,
    service: Service,
    file_name: Annotated[str, Query(min_length=1, max_length=255)],
    media_type: Annotated[str, Query(min_length=1, max_length=150)],
    document_type: DocumentType = "CONTRACT",
) -> ManagedDocumentResponse:
    service.require_writer(actor)
    await run_in_threadpool(service.repository.get_base, actor, base_id)
    content = bytearray()
    async for part in request.stream():
        if len(content) + len(part) > MAX_UPLOAD_BYTES:
            raise ContractOpsError(
                code="document_size_invalid", message="单文件最多 5 MiB。", status_code=413
            )
        content.extend(part)
    doc, replayed = await run_in_threadpool(
        service.upload, actor, base_id, file_name, document_type, media_type, bytes(content)
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    response.headers["Cache-Control"] = "no-store"
    return ManagedDocumentResponse.from_domain(doc, actor)


@router.get("/documents/{document_id}", response_model=ManagedDocumentResponse)
def get_document(
    document_id: UUID, response: Response, actor: Actor, service: Service
) -> ManagedDocumentResponse:
    service.require_reader(actor)
    response.headers["Cache-Control"] = "no-store"
    return ManagedDocumentResponse.from_domain(
        service.repository.get_document(actor, document_id), actor
    )


@router.get("/documents/{document_id}/chunks", response_model=list[ChunkResponse])
def document_chunks(
    document_id: UUID,
    response: Response,
    actor: Actor,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[ChunkResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [
        ChunkResponse.from_domain(chunk)
        for chunk in service.document_chunks(actor, document_id, limit=limit, offset=offset)
    ]


@router.post("/documents/{document_id}:retry", response_model=ManagedDocumentResponse)
def retry(
    document_id: UUID, payload: StateRequest, actor: Actor, service: Service
) -> ManagedDocumentResponse:
    return ManagedDocumentResponse.from_domain(
        service.retry(actor, document_id, payload.expected_version), actor
    )


@router.get("/documents/{document_id}/versions", response_model=list[ManagedDocumentResponse])
def versions(
    document_id: UUID, response: Response, actor: Actor, service: Service
) -> list[ManagedDocumentResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [
        ManagedDocumentResponse.from_domain(doc, actor)
        for doc in service.versions(actor, document_id)
    ]


@router.get("/documents/{document_id}/original")
def original(document_id: UUID, actor: Actor, service: Service) -> Response:
    document, content = service.original(actor, document_id)
    return Response(
        content,
        media_type=document.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(document.file_name, safe=""),
        },
    )


@router.get("/documents/{document_id}/text", response_class=PlainTextResponse)
def full_text(document_id: UUID, actor: Actor, service: Service) -> PlainTextResponse:
    blocks: list[str] = []
    for offset in range(0, 2000, 100):
        page = service.document_chunks(actor, document_id, limit=100, offset=offset)
        blocks.extend(chunk.content for chunk in page)
        if len(page) < 100:
            break
    return PlainTextResponse("\n\n".join(blocks), headers={"Cache-Control": "no-store"})


@router.get("/documents/{document_id}/processing")
def processing(
    document_id: UUID, response: Response, actor: Actor, service: Service
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return service.processing(actor, document_id)


@router.get("/documents/{document_id}/pages/{page}.png")
def preview(document_id: UUID, page: int, actor: Actor, service: Service) -> Response:
    return Response(
        service.preview(actor, document_id, page),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/documents/{document_id}:sync", response_model=ManagedDocumentResponse)
def sync(document_id: UUID, actor: Actor, service: Service) -> ManagedDocumentResponse:
    return ManagedDocumentResponse.from_domain(service.sync(actor, document_id), actor)


@router.post(
    "/documents/{document_id}/versions", response_model=ManagedDocumentResponse, status_code=201
)
async def upload_version(
    document_id: UUID,
    request: Request,
    response: Response,
    actor: Actor,
    service: Service,
    expected_version: Annotated[int, Query(ge=0)],
    file_name: Annotated[str, Query(min_length=1, max_length=255)],
    media_type: Annotated[str, Query(min_length=1, max_length=150)],
) -> ManagedDocumentResponse:
    await run_in_threadpool(service._manageable, actor, document_id)
    content = bytearray()
    async for part in request.stream():
        if len(content) + len(part) > MAX_UPLOAD_BYTES:
            raise ContractOpsError(
                code="document_size_invalid", message="单文件最多5 MiB。", status_code=413
            )
        content.extend(part)
    document, replayed = await run_in_threadpool(
        service.upload_version,
        actor,
        document_id,
        expected_version,
        file_name,
        media_type,
        bytes(content),
    )
    response.headers["X-Idempotent-Replay"] = str(replayed).lower()
    response.headers["Cache-Control"] = "no-store"
    return ManagedDocumentResponse.from_domain(document, actor)


@router.post("/documents/{document_id}:archive", response_model=ManagedDocumentResponse)
def archive(
    document_id: UUID, payload: StateRequest, actor: Actor, service: Service
) -> ManagedDocumentResponse:
    return ManagedDocumentResponse.from_domain(
        service.archive(actor, document_id, payload.expected_version), actor
    )
