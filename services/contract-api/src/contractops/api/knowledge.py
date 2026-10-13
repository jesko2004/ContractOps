from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field, model_validator

from contractops.api.dependencies import authenticated_actor, get_knowledge_service
from contractops.application.knowledge import KnowledgeCitation, KnowledgeDocument, KnowledgeService
from contractops.context import ActorContext

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
Actor = Annotated[ActorContext, Depends(authenticated_actor)]
Service = Annotated[KnowledgeService, Depends(get_knowledge_service)]


class KnowledgeDocumentResponse(BaseModel):
    contract_id: UUID
    contract_title: str
    version_id: UUID
    version_number: int
    file_name: str
    status: str
    file_hash: str
    chunk_count: int
    searchable: bool
    document_id: UUID | None = None
    base_id: UUID | None = None
    document_type: str = "CONTRACT"

    @classmethod
    def from_domain(cls, value: KnowledgeDocument) -> KnowledgeDocumentResponse:
        return cls(
            contract_id=value.contract_id,
            contract_title=value.contract_title,
            version_id=value.version_id,
            version_number=value.version_number,
            file_name=value.file_name,
            status=value.status,
            file_hash=value.file_hash,
            chunk_count=value.chunk_count,
            searchable=value.status == "READY" and value.chunk_count > 0,
            document_id=value.document_id,
            base_id=value.base_id,
            document_type=value.document_type,
        )


class CitationResponse(BaseModel):
    contract_id: UUID
    contract_title: str
    version_id: UUID
    version_number: int
    file_name: str
    file_hash: str
    chunk_id: UUID
    sequence: int
    page_number: int | None
    heading_path: list[str]
    content_hash: str
    score: int
    excerpt: str
    excerpt_start: int
    excerpt_end: int
    document_id: UUID | None = None
    base_id: UUID | None = None
    source_pages: list[int] = []

    @classmethod
    def from_domain(cls, value: KnowledgeCitation) -> CitationResponse:
        hit = value.source
        return cls(
            contract_id=hit.contract_id,
            contract_title=hit.contract_title,
            version_id=hit.version_id,
            version_number=hit.version_number,
            file_name=hit.file_name,
            file_hash=hit.file_hash,
            chunk_id=hit.chunk_id,
            sequence=hit.sequence,
            page_number=hit.page_number,
            heading_path=list(hit.heading_path),
            content_hash=hit.content_hash,
            score=hit.score,
            excerpt=value.excerpt,
            excerpt_start=value.excerpt_start,
            excerpt_end=value.excerpt_end,
            document_id=hit.document_id,
            base_id=hit.base_id,
            source_pages=list(hit.source_pages) or ([hit.page_number] if hit.page_number else []),
        )


class SearchResponse(BaseModel):
    query: str
    mode: Literal["LEXICAL"] = "LEXICAL"
    results: list[CitationResponse]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    limit: int = Field(default=3, ge=1, le=10)
    contract_id: UUID | None = None
    base_id: UUID | None = None
    document_id: UUID | None = None
    history: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=6
    )

    @model_validator(mode="after")
    def validate_scope(self) -> AskRequest:
        if self.document_id is not None and self.contract_id is not None:
            raise ValueError("document_id and contract_id cannot be selected together")
        return self


class AskResponse(BaseModel):
    mode: Literal["EXTRACTIVE", "RAG"] = "EXTRACTIVE"
    status: str
    answer: str
    citations: list[CitationResponse]
    fallback_reason: str | None = None
    search_query: str = ""
    scope: dict[str, str | None] = Field(default_factory=dict)


@router.get("/documents", response_model=list[KnowledgeDocumentResponse])
def list_documents(
    response: Response,
    actor: Actor,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> list[KnowledgeDocumentResponse]:
    response.headers["Cache-Control"] = "no-store"
    return [
        KnowledgeDocumentResponse.from_domain(item)
        for item in service.list_documents(actor, limit=limit, offset=offset)
    ]


@router.get("/search", response_model=SearchResponse)
def search(
    response: Response,
    actor: Actor,
    service: Service,
    query: Annotated[str, Query(min_length=1, max_length=300)],
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
    contract_id: UUID | None = None,
    base_id: UUID | None = None,
) -> SearchResponse:
    response.headers["Cache-Control"] = "no-store"
    return SearchResponse(
        query=query,
        results=[
            CitationResponse.from_domain(item)
            for item in service.search(
                actor, query, limit=limit, contract_id=contract_id, base_id=base_id
            )
        ],
    )


@router.post("/ask", response_model=AskResponse)
def ask(payload: AskRequest, response: Response, actor: Actor, service: Service) -> AskResponse:
    response.headers["Cache-Control"] = "no-store"
    answer = service.ask(
        actor,
        payload.question,
        limit=payload.limit,
        contract_id=payload.document_id or payload.contract_id,
        base_id=payload.base_id,
        history=tuple(payload.history),
    )
    return AskResponse(
        mode="RAG" if answer.mode == "RAG" else "EXTRACTIVE",
        status=answer.status,
        answer=answer.answer,
        citations=[CitationResponse.from_domain(item) for item in answer.citations],
        fallback_reason=answer.fallback_reason,
        search_query=answer.search_query,
        scope={
            "base_id": str(payload.base_id) if payload.base_id else None,
            "document_id": str(payload.document_id) if payload.document_id else None,
            "contract_id": str(payload.contract_id) if payload.contract_id else None,
        },
    )
