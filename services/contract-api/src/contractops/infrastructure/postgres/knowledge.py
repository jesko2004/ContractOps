from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DBAPIError

from contractops.application.knowledge import (
    KnowledgeDocument,
    KnowledgeHit,
    KnowledgeRepository,
    SearchTerm,
)
from contractops.context import ActorContext
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database

# This is applied BEFORE ranking/pagination, in addition to transaction-scoped RLS.
# It mirrors ContractService._can_read, including own records in department scope.
_SCOPE = """
    c.tenant_id = :tenant_id
    AND (
        :data_scope = 'TENANT' OR c.created_by = :user_id
        OR (:data_scope = 'DEPARTMENT'
            AND c.department_id = ANY(CAST(:department_ids AS uuid[])))
    )
"""


class PostgresKnowledgeRepository(KnowledgeRepository):
    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def _parameters(actor: ActorContext) -> dict[str, object]:
        return {
            "tenant_id": actor.tenant_id,
            "user_id": actor.user_id,
            "data_scope": actor.data_scope.value,
            "department_ids": list(actor.department_ids),
        }

    def _read(
        self, actor: ActorContext, query: str, parameters: dict[str, object]
    ) -> tuple[RowMapping, ...]:
        try:
            with self._database.transaction(actor) as connection:
                # Literal matching is deliberately a small-corpus MVP, not an unbounded scan.
                connection.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                return tuple(connection.execute(text(query), parameters).mappings().all())
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "57014":
                raise ContractOpsError(
                    code="knowledge_query_timeout",
                    message="knowledge retrieval timed out; narrow the query or contract scope",
                    status_code=503,
                ) from None
            raise

    def list_documents(
        self, actor: ActorContext, *, limit: int, offset: int
    ) -> tuple[KnowledgeDocument, ...]:
        parameters = self._parameters(actor)
        parameters.update(limit=limit, offset=offset)
        rows = self._read(
            actor,
            f"""
            SELECT c.id AS contract_id, c.title AS contract_title,
                   v.id AS version_id, v.version_number, v.file_name,
                   v.status, v.content_hash AS file_hash, c.updated_at,
                   (SELECT count(*) FROM contract_chunks ch
                    WHERE ch.tenant_id = c.tenant_id
                      AND ch.contract_version_id = v.id) AS chunk_count
            FROM contracts c
            JOIN contract_versions v ON v.id = c.current_version_id
                AND v.tenant_id = c.tenant_id AND v.contract_id = c.id
            WHERE {_SCOPE}
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT :limit OFFSET :offset
            """,
            parameters,
        )
        return tuple(
            KnowledgeDocument(
                contract_id=cast(UUID, row["contract_id"]),
                contract_title=cast(str, row["contract_title"]),
                version_id=cast(UUID, row["version_id"]),
                version_number=cast(int, row["version_number"]),
                file_name=cast(str, row["file_name"]),
                status=cast(str, row["status"]),
                file_hash=cast(str, row["file_hash"]),
                chunk_count=cast(int, row["chunk_count"]),
                updated_at=cast(datetime, row["updated_at"]),
            )
            for row in rows
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
        if not terms or base_id is not None:
            return ()
        parameters = self._parameters(actor)
        parameters["limit"] = limit
        rank_parts = []
        for index, term in enumerate(terms):
            parameters[f"term_{index}"] = term.text
            parameters[f"weight_{index}"] = term.weight
            # Only server-generated parameter names enter SQL, never the query text.
            rank_parts.append(
                f"CASE WHEN strpos(lower(content), :term_{index}) > 0 "
                f"THEN :weight_{index} ELSE 0 END"
            )
        contract_filter = ""
        if contract_id is not None:
            parameters["contract_id"] = contract_id
            contract_filter = "AND c.id = :contract_id"
        rows = self._read(
            actor,
            f"""
            WITH accessible AS MATERIALIZED (
                SELECT c.id AS contract_id, c.title AS contract_title,
                       v.id AS version_id, v.version_number, v.file_name,
                       v.content_hash AS file_hash, ch.id AS chunk_id,
                       ch.sequence, ch.page_number, ch.heading_path,
                       ch.content, ch.content_hash
                FROM contracts c
                JOIN contract_versions v ON v.id = c.current_version_id
                    AND v.tenant_id = c.tenant_id AND v.contract_id = c.id
                JOIN contract_chunks ch ON ch.contract_version_id = v.id
                    AND ch.tenant_id = c.tenant_id
                WHERE {_SCOPE} AND v.status = 'READY' {contract_filter}
            ), ranked AS (
                SELECT *, ({" + ".join(rank_parts)}) AS score FROM accessible
            )
            SELECT * FROM ranked WHERE score > 0
            ORDER BY score DESC, contract_id, sequence, chunk_id
            LIMIT :limit
            """,
            parameters,
        )
        return tuple(
            KnowledgeHit(
                contract_id=cast(UUID, row["contract_id"]),
                contract_title=cast(str, row["contract_title"]),
                version_id=cast(UUID, row["version_id"]),
                version_number=cast(int, row["version_number"]),
                file_name=cast(str, row["file_name"]),
                file_hash=cast(str, row["file_hash"]),
                chunk_id=cast(UUID, row["chunk_id"]),
                sequence=cast(int, row["sequence"]),
                page_number=cast(int | None, row["page_number"]),
                heading_path=tuple(cast(list[str] | None, row["heading_path"]) or ()),
                content=cast(str, row["content"]),
                content_hash=cast(str, row["content_hash"]),
                score=cast(int, row["score"]),
            )
            for row in rows
        )
