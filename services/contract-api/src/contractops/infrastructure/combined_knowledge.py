from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from contractops.application.knowledge import (
    KnowledgeDocument,
    KnowledgeHit,
    KnowledgeRepository,
    SearchTerm,
)
from contractops.context import ActorContext


class CombinedKnowledgeRepository:
    """Search original contract evidence and managed knowledge without copying contract text."""

    def __init__(self, contracts: KnowledgeRepository, managed: KnowledgeRepository) -> None:
        self.contracts = contracts
        self.managed = managed

    def list_documents(
        self, actor: ActorContext, *, limit: int, offset: int
    ) -> tuple[KnowledgeDocument, ...]:
        count = offset + limit
        items = self.contracts.list_documents(actor, limit=count, offset=0)
        items += self.managed.list_documents(actor, limit=count, offset=0)
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    item.updated_at or datetime.min.replace(tzinfo=UTC),
                    item.document_id or item.contract_id,
                ),
                reverse=True,
            )[offset : offset + limit]
        )

    def search(
        self,
        actor: ActorContext,
        terms: tuple[SearchTerm, ...],
        *,
        limit: int,
        contract_id: UUID | None,
        base_id: UUID | None = None,
    ) -> tuple[KnowledgeHit, ...]:
        if base_id is not None:
            return self.managed.search(
                actor, terms, limit=limit, contract_id=contract_id, base_id=base_id
            )
        items = self.contracts.search(actor, terms, limit=limit, contract_id=contract_id)
        items += self.managed.search(actor, terms, limit=limit, contract_id=contract_id)
        return tuple(
            sorted(
                items,
                key=lambda item: (-item.score, item.contract_id, item.sequence, item.chunk_id),
            )[:limit]
        )
