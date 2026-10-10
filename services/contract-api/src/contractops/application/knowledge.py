from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from contractops.context import ActorContext, Role
from contractops.errors import ContractOpsError

MAX_QUERY_LENGTH = 300
MAX_TERMS = 32
EXCERPT_LENGTH = 900
_TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+")
_QUESTION_WORDS = frozenset(
    {"什么", "时候", "怎么", "如何", "多少", "请问", "这个", "这份", "哪些", "是否"}
)


@dataclass(frozen=True, slots=True)
class SearchTerm:
    text: str
    weight: int


def search_terms(query: str) -> tuple[SearchTerm, ...]:
    """Bounded literal terms; Chinese bigrams need no tokenizer/model dependency."""
    query = query.strip().lower()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise ContractOpsError(
            code="knowledge_query_invalid",
            message=f"query must contain 1 to {MAX_QUERY_LENGTH} characters",
            status_code=422,
        )
    terms: dict[str, int] = {}
    for token in _TOKEN.findall(query):
        if token in _QUESTION_WORDS:
            continue
        terms[token] = max(terms.get(token, 0), 4)
        if "\u3400" <= token[0] <= "\u9fff" and len(token) > 2:
            for index in range(len(token) - 1):
                pair = token[index : index + 2]
                if pair not in _QUESTION_WORDS:
                    terms.setdefault(pair, 1)
    if not terms:
        raise ContractOpsError(
            code="knowledge_query_invalid",
            message="query must contain a searchable word or Chinese phrase",
            status_code=422,
        )
    # Keep complete words before bigrams; stable ordering makes scores reproducible.
    ranked = sorted(terms.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return tuple(SearchTerm(term, weight) for term, weight in ranked[:MAX_TERMS])


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    contract_id: UUID
    contract_title: str
    version_id: UUID
    version_number: int
    file_name: str
    status: str
    file_hash: str
    chunk_count: int
    document_id: UUID | None = None
    base_id: UUID | None = None
    document_type: str = "CONTRACT"
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    contract_id: UUID
    contract_title: str
    version_id: UUID
    version_number: int
    file_name: str
    file_hash: str
    chunk_id: UUID
    sequence: int
    page_number: int | None
    heading_path: tuple[str, ...]
    content: str
    content_hash: str
    score: int
    document_id: UUID | None = None
    base_id: UUID | None = None
    source_pages: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeCitation:
    source: KnowledgeHit
    excerpt: str
    excerpt_start: int
    excerpt_end: int


@dataclass(frozen=True, slots=True)
class KnowledgeAnswer:
    status: str
    answer: str
    citations: tuple[KnowledgeCitation, ...]
    mode: str = "EXTRACTIVE"
    fallback_reason: str | None = None
    search_query: str = ""


class AnswerModel(Protocol):
    def answer(
        self, question: str, citations: tuple[KnowledgeCitation, ...], history: tuple[str, ...]
    ) -> str | None: ...


class KnowledgeRepository(Protocol):
    def list_documents(
        self, actor: ActorContext, *, limit: int, offset: int
    ) -> tuple[KnowledgeDocument, ...]: ...

    def search(
        self,
        actor: ActorContext,
        terms: tuple[SearchTerm, ...],
        *,
        limit: int,
        contract_id: UUID | None,
        base_id: UUID | None = None,
    ) -> tuple[KnowledgeHit, ...]: ...


class KnowledgeService:
    _READ_ROLES = (
        Role.CONTRACT_OWNER,
        Role.APPROVER,
        Role.LEGAL_ADMIN,
        Role.FINANCE_APPROVER,
        Role.BUSINESS_APPROVER,
        Role.AUDITOR,
        Role.TENANT_ADMIN,
    )

    def __init__(self, repository: KnowledgeRepository, model: AnswerModel | None = None) -> None:
        self._repository = repository
        self.model = model

    @staticmethod
    def _require_reader(actor: ActorContext) -> None:
        if not actor.has_any_role(*KnowledgeService._READ_ROLES):
            raise ContractOpsError(
                code="authorization_denied",
                message="the caller cannot read contract knowledge",
                status_code=403,
            )

    def list_documents(
        self, actor: ActorContext, *, limit: int = 20, offset: int = 0
    ) -> tuple[KnowledgeDocument, ...]:
        self._require_reader(actor)
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
            raise ContractOpsError(
                code="knowledge_pagination_invalid",
                message="limit must be 1..100 and offset 0..10000",
                status_code=422,
            )
        return self._repository.list_documents(actor, limit=limit, offset=offset)

    def search(
        self,
        actor: ActorContext,
        query: str,
        *,
        limit: int = 5,
        contract_id: UUID | None = None,
        base_id: UUID | None = None,
    ) -> tuple[KnowledgeCitation, ...]:
        self._require_reader(actor)
        if not 1 <= limit <= 10:
            raise ContractOpsError(
                code="knowledge_limit_invalid",
                message="search limit must be between 1 and 10",
                status_code=422,
            )
        terms = search_terms(query)
        if base_id is None:
            hits = self._repository.search(actor, terms, limit=limit, contract_id=contract_id)
        else:
            hits = self._repository.search(
                actor, terms, limit=limit, contract_id=contract_id, base_id=base_id
            )
        citations = []
        for hit in hits:
            positions = [
                match.start()
                for term in terms
                if (match := re.search(re.escape(term.text), hit.content, re.IGNORECASE))
                is not None
            ]
            # Empty/nonmatching evidence is never labeled as a successful answer.
            if not positions:
                continue
            start = max(0, min(positions) - 150)
            end = min(len(hit.content), start + EXCERPT_LENGTH)
            citations.append(KnowledgeCitation(hit, hit.content[start:end], start, end))
        return tuple(citations)

    def ask(
        self,
        actor: ActorContext,
        question: str,
        *,
        limit: int = 3,
        contract_id: UUID | None = None,
        base_id: UUID | None = None,
        history: tuple[str, ...] = (),
    ) -> KnowledgeAnswer:
        if len(history) > 6 or any(not item.strip() or len(item) > 300 for item in history):
            raise ContractOpsError(
                code="knowledge_history_invalid",
                message="最多6条历史问题，每条最多300字符。",
                status_code=422,
            )
        search_terms(question)
        query = (question + " " + " ".join(history[-2:]))[:MAX_QUERY_LENGTH].strip()
        citations = self.search(actor, query, limit=limit, contract_id=contract_id, base_id=base_id)
        if not citations:
            return KnowledgeAnswer(
                status="INSUFFICIENT_EVIDENCE",
                answer="在当前可访问且解析完成的合同版本中，未找到匹配原文。请调整关键词，或确认文档解析状态。",
                citations=(),
                search_query=query,
            )
        generated = None if self.model is None else self.model.answer(question, citations, history)
        # Re-read after the model call: no stale archived/version-switched evidence is returned.
        fresh = self.search(actor, query, limit=limit, contract_id=contract_id, base_id=base_id)
        if {c.source.chunk_id for c in fresh} != {c.source.chunk_id for c in citations}:
            generated, citations = None, fresh
        if generated:
            return KnowledgeAnswer(
                status="INSUFFICIENT_EVIDENCE"
                if generated.startswith("依据不足")
                else "MODEL_ANSWER",
                answer=generated,
                citations=citations,
                mode="RAG",
                search_query=query,
            )
        if not citations:
            return KnowledgeAnswer(
                status="INSUFFICIENT_EVIDENCE",
                answer="资料状态已变化，请重新提问。",
                citations=(),
                search_query=query,
            )
        excerpts = "\n\n".join(
            f"[{index}] {citation.excerpt}" for index, citation in enumerate(citations, start=1)
        )
        return KnowledgeAnswer(
            status="EVIDENCE_FOUND",
            answer=(
                "以下为关键词命中的原文摘录，不是模型生成的结论；"
                "命中不代表已完整回答问题，请结合来源核对。\n\n" + excerpts
            ),
            citations=citations,
            search_query=query,
            fallback_reason="MODEL_NOT_CONFIGURED"
            if self.model is None
            else "MODEL_UNAVAILABLE_OR_INVALID",
        )
