from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import (
    and_,
    case,
    create_engine,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from contractops.application.knowledge import KnowledgeDocument, KnowledgeHit, SearchTerm
from contractops.application.knowledge_management import (
    KnowledgeBase,
    KnowledgeManagementService,
    ManagedChunk,
    ManagedDocument,
)
from contractops.context import ActorContext, DataScope
from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import ParsedBlock
from contractops.infrastructure.knowledge_schema import (
    bases,
    chunks,
    documents,
    inspector_jobs,
    metadata,
)
from contractops.infrastructure.postgres.database import Database


def _utc(value: object) -> datetime:
    result = cast(datetime, value)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def _missing() -> ContractOpsError:
    return ContractOpsError(
        code="knowledge_not_found",
        message="知识库或文档不存在，或当前身份无权访问。",
        status_code=404,
    )


def _conflict() -> ContractOpsError:
    return ContractOpsError(
        code="knowledge_state_conflict", message="数据状态已变化，请刷新后重试。", status_code=409
    )


class KnowledgeStore:
    """The same management implementation runs on PostgreSQL and local SQLite."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @contextmanager
    def _transaction(self, actor: ActorContext) -> Iterator[Connection]:
        try:
            with self.database.transaction(actor) as connection:
                if connection.dialect.name == "postgresql":
                    connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
                yield connection
        except IntegrityError:
            raise ContractOpsError(
                code="knowledge_write_conflict",
                message="名称或文件重复，或数据已变化，请刷新后重试。",
                status_code=409,
            ) from None
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "57014":
                raise ContractOpsError(
                    code="knowledge_query_timeout",
                    message="知识库操作超时，请缩小范围后重试。",
                    status_code=503,
                ) from None
            raise

    @staticmethod
    def _base_scope(actor: ActorContext) -> ColumnElement[bool]:
        return and_(
            bases.c.tenant_id == actor.tenant_id,
            or_(
                literal(actor.data_scope is DataScope.TENANT),
                bases.c.created_by == actor.user_id,
                and_(
                    literal(actor.data_scope is DataScope.DEPARTMENT),
                    bases.c.department_id.in_(actor.department_ids),
                ),
            ),
        )

    @classmethod
    def _document_scope(cls, actor: ActorContext) -> ColumnElement[bool]:
        return and_(
            cls._base_scope(actor),
            documents.c.tenant_id == actor.tenant_id,
            or_(
                literal(actor.data_scope is DataScope.TENANT),
                documents.c.created_by == actor.user_id,
                and_(
                    literal(actor.data_scope is DataScope.DEPARTMENT),
                    bases.c.department_id.in_(actor.department_ids),
                ),
            ),
        )

    @staticmethod
    def _base(row: RowMapping) -> KnowledgeBase:
        return KnowledgeBase(
            cast(UUID, row["id"]),
            cast(str, row["name"]),
            cast(str, row["description"]),
            cast(UUID, row["department_id"]),
            cast(UUID, row["created_by"]),
            _utc(row["created_at"]),
            _utc(row["updated_at"]),
            cast(int, row.get("document_count", 0)),
        )

    @staticmethod
    def _document(row: RowMapping) -> ManagedDocument:
        return ManagedDocument(
            id=cast(UUID, row["id"]),
            base_id=cast(UUID, row["base_id"]),
            version_id=cast(UUID, row["version_id"]),
            file_name=cast(str, row["file_name"]),
            document_type=cast(str, row["document_type"]),
            media_type=cast(str, row["media_type"]),
            size_bytes=cast(int, row["size_bytes"]),
            file_hash=cast(str, row["file_hash"]),
            status=cast(str, row["status"]),
            created_by=cast(UUID, row["created_by"]),
            created_at=_utc(row["created_at"]),
            updated_at=_utc(row["updated_at"]),
            state_version=cast(int, row["state_version"]),
            chunk_count=cast(int, row["chunk_count"]),
            error_code=cast(str | None, row["error_code"]),
            error_message=cast(str | None, row["error_message"]),
            lease_expires_at=None
            if row["lease_expires_at"] is None
            else _utc(row["lease_expires_at"]),
            original_available=bool(row["original_available"]),
            root_id=cast(UUID | None, row["root_id"]) or cast(UUID, row["id"]),
            version_number=cast(int, row["version_number"]),
            is_current=bool(row["is_current"]),
            parse_details=cast(dict[str, object] | None, row["parse_details"]),
        )

    def _document_select(self, actor: ActorContext) -> Select[tuple[Any, ...]]:
        count = (
            select(func.count())
            .select_from(chunks)
            .where(
                chunks.c.tenant_id == documents.c.tenant_id,
                chunks.c.document_id == documents.c.id,
            )
            .correlate(documents)
            .scalar_subquery()
        )
        return (
            select(
                *[column for column in documents.c if column.name != "original"],
                count.label("chunk_count"),
                documents.c.original.is_not(None).label("original_available"),
            )
            .select_from(
                documents.join(
                    bases,
                    and_(
                        documents.c.base_id == bases.c.id,
                        documents.c.tenant_id == bases.c.tenant_id,
                    ),
                )
            )
            .where(self._document_scope(actor))
        )

    def create_base(
        self, actor: ActorContext, name: str, description: str, department_id: UUID
    ) -> KnowledgeBase:
        now = datetime.now(UTC)
        value = dict(
            id=uuid4(),
            tenant_id=actor.tenant_id,
            name=name,
            description=description,
            department_id=department_id,
            created_by=actor.user_id,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._transaction(actor) as connection:
                row = (
                    connection.execute(insert(bases).values(**value).returning(*bases.c))
                    .mappings()
                    .one()
                )
                return self._base(row)
        except IntegrityError:
            raise ContractOpsError(
                code="knowledge_base_conflict",
                message="你已创建同名知识库，请使用其他名称。",
                status_code=409,
            ) from None

    def list_bases(self, actor: ActorContext) -> tuple[KnowledgeBase, ...]:
        count = (
            select(func.count())
            .select_from(documents)
            .where(
                documents.c.base_id == bases.c.id,
                documents.c.tenant_id == bases.c.tenant_id,
                documents.c.is_current.is_(True),
                or_(
                    literal(actor.data_scope is DataScope.TENANT),
                    documents.c.created_by == actor.user_id,
                    and_(
                        literal(actor.data_scope is DataScope.DEPARTMENT),
                        bases.c.department_id.in_(actor.department_ids),
                    ),
                ),
            )
            .correlate(bases)
            .scalar_subquery()
        )
        with self._transaction(actor) as connection:
            rows = (
                connection.execute(
                    select(bases, count.label("document_count"))
                    .where(self._base_scope(actor))
                    .order_by(bases.c.updated_at.desc(), bases.c.id.desc())
                    .limit(500)
                )
                .mappings()
                .all()
            )
        return tuple(self._base(row) for row in rows)

    def get_base(self, actor: ActorContext, base_id: UUID) -> KnowledgeBase:
        with self._transaction(actor) as connection:
            row = (
                connection.execute(
                    select(bases).where(self._base_scope(actor), bases.c.id == base_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _missing()
        return self._base(row)

    def documents(
        self,
        actor: ActorContext,
        base_id: UUID,
        *,
        query: str,
        document_type: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[ManagedDocument, ...]:
        statement = self._document_select(actor).where(
            documents.c.base_id == base_id, documents.c.is_current.is_(True)
        )
        if query:
            statement = statement.where(
                func.lower(documents.c.file_name).contains(query.lower(), autoescape=True)
            )
        if document_type:
            statement = statement.where(documents.c.document_type == document_type)
        if status:
            statement = statement.where(documents.c.status == status)
        with self._transaction(actor) as connection:
            rows = (
                connection.execute(
                    statement.order_by(documents.c.updated_at.desc(), documents.c.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return tuple(self._document(row) for row in rows)

    def get_document(self, actor: ActorContext, document_id: UUID) -> ManagedDocument:
        with self._transaction(actor) as connection:
            row = (
                connection.execute(
                    self._document_select(actor).where(documents.c.id == document_id)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise _missing()
        return self._document(row)

    def document_chunks(
        self, actor: ActorContext, document_id: UUID, *, limit: int, offset: int
    ) -> tuple[ManagedChunk, ...]:
        with self._transaction(actor) as connection:
            visible = connection.execute(
                self._document_select(actor).where(documents.c.id == document_id)
            ).first()
            if visible is None:
                raise _missing()
            rows = (
                connection.execute(
                    select(chunks)
                    .select_from(
                        chunks.join(
                            documents,
                            and_(
                                chunks.c.document_id == documents.c.id,
                                chunks.c.tenant_id == documents.c.tenant_id,
                            ),
                        ).join(
                            bases,
                            and_(
                                documents.c.base_id == bases.c.id,
                                documents.c.tenant_id == bases.c.tenant_id,
                            ),
                        )
                    )
                    .where(
                        self._document_scope(actor),
                        chunks.c.tenant_id == actor.tenant_id,
                        chunks.c.document_id == document_id,
                    )
                    .order_by(chunks.c.sequence)
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return tuple(
            ManagedChunk(
                cast(UUID, row["id"]),
                cast(int, row["sequence"]),
                cast(int | None, row["page_number"]),
                tuple(cast(list[str], row["heading_path"])),
                cast(str, row["content"]),
                cast(str, row["content_hash"]),
                tuple(cast(list[int] | None, row["source_pages"]) or ()),
                cast(str | None, row["source_id"]),
                cast(str | None, row["kind"]) or "text",
            )
            for row in rows
        )

    def save_upload(
        self,
        actor: ActorContext,
        base: KnowledgeBase,
        file_name: str,
        document_type: str,
        media_type: str,
        content: bytes,
        file_hash: str,
    ) -> tuple[ManagedDocument, bool]:
        document_id = uuid4()
        now = datetime.now(UTC)
        with self._transaction(actor) as connection:
            # Serialize quota/dedup checks, not just the insert. SQLite ignores FOR UPDATE.
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            parent = connection.execute(
                select(bases.c.id)
                .where(self._base_scope(actor), bases.c.id == base.id)
                .with_for_update()
            ).first()
            if parent is None:
                raise _missing()
            existing = (
                connection.execute(
                    self._document_select(actor).where(
                        documents.c.base_id == base.id, documents.c.file_hash == file_hash
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                return self._document(existing), True
            count = connection.execute(
                select(func.count())
                .select_from(documents)
                .where(documents.c.tenant_id == actor.tenant_id, documents.c.base_id == base.id)
            ).scalar_one()
            if count >= 100:
                raise ContractOpsError(
                    code="knowledge_document_limit",
                    message="最小版本每个知识库最多 100 份文档。",
                    status_code=409,
                )
            connection.execute(
                insert(documents).values(
                    id=document_id,
                    tenant_id=actor.tenant_id,
                    base_id=base.id,
                    version_id=uuid4(),
                    root_id=document_id,
                    file_name=file_name,
                    document_type=document_type,
                    media_type=media_type,
                    size_bytes=len(content),
                    file_hash=file_hash,
                    original=content,
                    status="PARSING",
                    state_version=0,
                    created_by=actor.user_id,
                    created_at=now,
                    updated_at=now,
                    lease_expires_at=now + timedelta(minutes=5),
                )
            )
            connection.execute(
                update(bases)
                .where(bases.c.id == base.id, bases.c.tenant_id == actor.tenant_id)
                .values(updated_at=now)
            )
        return self.get_document(actor, document_id), False

    def original(self, actor: ActorContext, document_id: UUID) -> bytes:
        self.get_document(actor, document_id)
        with self._transaction(actor) as connection:
            value: bytes | None = connection.execute(
                select(documents.c.original).where(
                    documents.c.tenant_id == actor.tenant_id, documents.c.id == document_id
                )
            ).scalar_one()
        if value is None:
            raise ContractOpsError(
                code="document_original_missing",
                message="旧演示仅保存正文，没有原文件；请重新上传文件。",
                status_code=409,
            )
        return bytes(value)

    def versions(self, actor: ActorContext, document_id: UUID) -> tuple[ManagedDocument, ...]:
        document = self.get_document(actor, document_id)
        with self._transaction(actor) as connection:
            rows = (
                connection.execute(
                    self._document_select(actor)
                    .where(func.coalesce(documents.c.root_id, documents.c.id) == document.root_id)
                    .order_by(documents.c.version_number.desc())
                    .limit(100)
                )
                .mappings()
                .all()
            )
        return tuple(self._document(row) for row in rows)

    def save_version(
        self,
        actor: ActorContext,
        document: ManagedDocument,
        expected_version: int,
        file_name: str,
        media_type: str,
        content: bytes,
    ) -> tuple[ManagedDocument, bool]:
        document_id, now = uuid4(), datetime.now(UTC)
        file_hash = hashlib.sha256(content).hexdigest()
        with self._transaction(actor) as connection:
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            # Same lock order as save_upload: serialize the shared per-base quota.
            parent = connection.execute(
                select(bases.c.id)
                .where(self._base_scope(actor), bases.c.id == document.base_id)
                .with_for_update()
            ).first()
            if parent is None:
                raise _missing()
            current = (
                connection.execute(
                    self._document_select(actor)
                    .where(documents.c.id == document.id)
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                current is None
                or not current["is_current"]
                or current["state_version"] != expected_version
            ):
                raise _conflict()
            if current["status"] in {"PARSING", "ARCHIVED"}:
                raise ContractOpsError(
                    code="document_version_not_allowed",
                    message="处理中或已归档文档不可更新版本。",
                    status_code=409,
                )
            if current["file_hash"] == file_hash:
                return self._document(current), True
            count = connection.execute(
                select(func.count())
                .select_from(documents)
                .where(
                    documents.c.tenant_id == actor.tenant_id,
                    documents.c.base_id == document.base_id,
                )
            ).scalar_one()
            if count >= 100:
                raise ContractOpsError(
                    code="knowledge_document_limit",
                    message="每库最多100个版本记录。",
                    status_code=409,
                )
            connection.execute(
                update(documents)
                .where(documents.c.id == document.id, documents.c.tenant_id == actor.tenant_id)
                .values(is_current=False, state_version=expected_version + 1, updated_at=now)
            )
            connection.execute(
                insert(documents).values(
                    id=document_id,
                    tenant_id=actor.tenant_id,
                    base_id=document.base_id,
                    version_id=uuid4(),
                    root_id=document.root_id,
                    version_number=document.version_number + 1,
                    file_name=file_name,
                    document_type=document.document_type,
                    media_type=media_type,
                    size_bytes=len(content),
                    file_hash=file_hash,
                    original=content,
                    status="PARSING",
                    state_version=0,
                    created_by=document.created_by,
                    created_at=now,
                    updated_at=now,
                    lease_expires_at=now + timedelta(minutes=5),
                )
            )
        return self.get_document(actor, document_id), False

    def inspector_job(self, actor: ActorContext, document_id: UUID) -> dict[str, Any] | None:
        self.get_document(actor, document_id)
        with self._transaction(actor) as connection:
            row = (
                connection.execute(
                    select(inspector_jobs).where(
                        inspector_jobs.c.tenant_id == actor.tenant_id,
                        inspector_jobs.c.document_id == document_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else dict(row)

    def save_inspector_job(
        self,
        actor: ActorContext,
        document_id: UUID,
        values: dict[str, Any],
        *,
        create: bool = False,
        expected_version: int | None = None,
    ) -> None:
        self.get_document(actor, document_id)
        with self._transaction(actor) as connection:
            if create:
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                statement = (sqlite_insert if connection.dialect.name == "sqlite" else pg_insert)(
                    inspector_jobs
                )
                connection.execute(
                    statement.values(
                        document_id=document_id,
                        tenant_id=actor.tenant_id,
                        updated_at=datetime.now(UTC),
                        **values,
                    ).on_conflict_do_nothing(index_elements=["document_id"])
                )
            else:
                conditions = [
                    inspector_jobs.c.document_id == document_id,
                    inspector_jobs.c.tenant_id == actor.tenant_id,
                ]
                if expected_version is not None:
                    conditions.append(
                        select(documents.c.id)
                        .where(
                            documents.c.id == document_id,
                            documents.c.tenant_id == actor.tenant_id,
                            documents.c.status == "PARSING",
                            documents.c.state_version == expected_version,
                        )
                        .exists()
                    )
                if values.get("status") in {"QUEUED", "PROCESSING", "SUBMISSION_UNKNOWN"}:
                    conditions.append(
                        inspector_jobs.c.status.in_(
                            ["SUBMITTING", "SUBMISSION_UNKNOWN", "QUEUED", "PROCESSING"]
                        )
                    )
                connection.execute(
                    update(inspector_jobs)
                    .where(*conditions)
                    .values(updated_at=datetime.now(UTC), **values)
                )

    def begin_retry(
        self, actor: ActorContext, document_id: UUID, expected_version: int
    ) -> ManagedDocument:
        self.get_document(actor, document_id)
        now = datetime.now(UTC)
        with self._transaction(actor) as connection:
            result = connection.execute(
                update(documents)
                .where(
                    documents.c.tenant_id == actor.tenant_id,
                    documents.c.id == document_id,
                    documents.c.state_version == expected_version,
                    or_(
                        documents.c.status == "FAILED",
                        and_(documents.c.status == "PARSING", documents.c.lease_expires_at <= now),
                    ),
                )
                .values(
                    status="PARSING",
                    state_version=expected_version + 1,
                    error_code=None,
                    error_message=None,
                    updated_at=now,
                    lease_expires_at=now + timedelta(minutes=5),
                )
            )
            if result.rowcount != 1:
                raise _conflict()
        return self.get_document(actor, document_id)

    def complete_parse(
        self,
        actor: ActorContext,
        document: ManagedDocument,
        blocks: tuple[ParsedBlock, ...],
        error: ContractOpsError | None,
        parse_details: dict[str, object] | None = None,
    ) -> ManagedDocument:
        now = datetime.now(UTC)
        with self._transaction(actor) as connection:
            result = connection.execute(
                update(documents)
                .where(
                    documents.c.id == document.id,
                    documents.c.tenant_id == actor.tenant_id,
                    documents.c.state_version == document.state_version,
                    documents.c.status == "PARSING",
                    documents.c.lease_expires_at > now,
                )
                .values(
                    status="FAILED" if error else "READY",
                    state_version=document.state_version + 1,
                    error_code=None if error is None else error.code,
                    error_message=None if error is None else str(error),
                    updated_at=now,
                    lease_expires_at=None,
                    parse_details=parse_details,
                )
            )
            if result.rowcount != 1:
                raise _conflict()
            connection.execute(
                delete(chunks).where(
                    chunks.c.tenant_id == actor.tenant_id, chunks.c.document_id == document.id
                )
            )
            if blocks:
                connection.execute(
                    insert(chunks),
                    [
                        dict(
                            id=uuid4(),
                            tenant_id=actor.tenant_id,
                            document_id=document.id,
                            sequence=block.sequence,
                            page_number=block.page_number,
                            heading_path=list(block.heading_path),
                            content=block.content,
                            content_hash=hashlib.sha256(block.content.encode()).hexdigest(),
                            source_pages=list(block.source_pages),
                            source_id=block.source_id,
                            kind=block.kind,
                        )
                        for block in blocks
                    ],
                )
        return self.get_document(actor, document.id)

    def archive(
        self, actor: ActorContext, document_id: UUID, expected_version: int
    ) -> ManagedDocument:
        self.get_document(actor, document_id)
        with self._transaction(actor) as connection:
            result = connection.execute(
                update(documents)
                .where(
                    documents.c.id == document_id,
                    documents.c.tenant_id == actor.tenant_id,
                    documents.c.state_version == expected_version,
                    documents.c.status.in_(["READY", "FAILED", "ARCHIVED"]),
                )
                .values(
                    status="ARCHIVED",
                    state_version=expected_version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            if result.rowcount != 1:
                raise _conflict()
        return self.get_document(actor, document_id)

    @staticmethod
    def _legacy_document(document: ManagedDocument) -> KnowledgeDocument:
        return KnowledgeDocument(
            document.id,
            document.file_name,
            document.version_id,
            document.version_number,
            document.file_name,
            document.status,
            document.file_hash,
            document.chunk_count,
            document_id=document.id,
            base_id=document.base_id,
            document_type=document.document_type,
            updated_at=document.updated_at,
        )

    def list_documents(
        self, actor: ActorContext, *, limit: int, offset: int
    ) -> tuple[KnowledgeDocument, ...]:
        with self._transaction(actor) as connection:
            rows = (
                connection.execute(
                    self._document_select(actor)
                    .where(documents.c.is_current.is_(True))
                    .order_by(documents.c.updated_at.desc(), documents.c.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
                .mappings()
                .all()
            )
        return tuple(self._legacy_document(self._document(row)) for row in rows)

    def search(
        self,
        actor: ActorContext,
        terms: tuple[SearchTerm, ...],
        *,
        limit: int,
        contract_id: UUID | None,
        base_id: UUID | None = None,
    ) -> tuple[KnowledgeHit, ...]:
        if not terms:
            return ()
        score = sum(
            (
                case(
                    (
                        func.lower(chunks.c.content).contains(term.text, autoescape=True),
                        term.weight,
                    ),
                    else_=0,
                )
                for term in terms
            ),
            start=literal(0),
        )
        source = chunks.join(
            documents,
            and_(
                chunks.c.document_id == documents.c.id, chunks.c.tenant_id == documents.c.tenant_id
            ),
        )
        source = source.join(
            bases,
            and_(documents.c.base_id == bases.c.id, documents.c.tenant_id == bases.c.tenant_id),
        )
        statement = (
            select(
                documents.c.id,
                documents.c.base_id,
                documents.c.version_id,
                documents.c.version_number,
                documents.c.file_name,
                documents.c.file_hash,
                chunks.c.id.label("chunk_id"),
                chunks.c.sequence,
                chunks.c.page_number,
                chunks.c.heading_path,
                chunks.c.content,
                chunks.c.content_hash,
                chunks.c.source_pages,
                score.label("score"),
            )
            .select_from(source)
            .where(
                self._document_scope(actor),
                documents.c.status == "READY",
                documents.c.is_current.is_(True),
                score > 0,
            )
        )
        if base_id:
            statement = statement.where(documents.c.base_id == base_id)
        if contract_id:
            statement = statement.where(documents.c.id == contract_id)
        with self._transaction(actor) as connection:
            rows = (
                connection.execute(
                    statement.order_by(
                        score.desc(), documents.c.id, chunks.c.sequence, chunks.c.id
                    ).limit(limit)
                )
                .mappings()
                .all()
            )
        return tuple(
            KnowledgeHit(
                cast(UUID, row["id"]),
                cast(str, row["file_name"]),
                cast(UUID, row["version_id"]),
                cast(int, row["version_number"]),
                cast(str, row["file_name"]),
                cast(str, row["file_hash"]),
                cast(UUID, row["chunk_id"]),
                cast(int, row["sequence"]),
                cast(int | None, row["page_number"]),
                tuple(cast(list[str], row["heading_path"])),
                cast(str, row["content"]),
                cast(str, row["content_hash"]),
                cast(int, row["score"]),
                document_id=cast(UUID, row["id"]),
                base_id=cast(UUID, row["base_id"]),
                source_pages=tuple(cast(list[int] | None, row["source_pages"]) or ()),
            )
            for row in rows
        )


class SQLiteKnowledgeDatabase(Database):
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False, "timeout": 10}
        )
        metadata.create_all(self._engine)
        self._upgrade_details()
        self._import_legacy()

    def _upgrade_details(self) -> None:
        # Additive migration of local MVP databases; original rows/files stay intact.
        with self._engine.begin() as connection:
            for table, additions in {
                "knowledge_documents": {
                    "root_id": "CHAR(32)",
                    "version_number": "INTEGER NOT NULL DEFAULT 1",
                    "is_current": "BOOLEAN NOT NULL DEFAULT 1",
                    "parse_details": "JSON",
                },
                "knowledge_document_chunks": {
                    "source_pages": "JSON",
                    "source_id": "VARCHAR(100)",
                    "kind": "VARCHAR(30)",
                },
            }.items():
                present = {
                    row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")
                }
                for column, definition in additions.items():
                    if column not in present:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            connection.exec_driver_sql(
                "UPDATE knowledge_documents SET root_id = id WHERE root_id IS NULL"
            )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_current_idx "
                "ON knowledge_documents(tenant_id, root_id) WHERE is_current"
            )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_version_idx "
                "ON knowledge_documents(tenant_id, root_id, version_number)"
            )

    @contextmanager
    def transaction(self, actor: ActorContext) -> Iterator[Connection]:
        with self._engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            yield connection

    def _import_legacy(self) -> None:
        """Copy old demo text once; keep the old tables intact, never write them again."""
        import json

        with self._engine.begin() as connection:
            if (
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
                ).first()
                is None
            ):
                return
            rows = connection.exec_driver_sql("SELECT * FROM documents").mappings().all()
            for row in rows:
                doc_id, tenant, owner = (
                    UUID(row["contract_id"]),
                    UUID(row["tenant_id"]),
                    UUID(row["owner_id"]),
                )
                if connection.execute(
                    select(documents.c.id).where(documents.c.id == doc_id)
                ).first():
                    continue
                base_id = uuid5(NAMESPACE_URL, f"contractops:legacy:{tenant}:{owner}")
                now = datetime.now(UTC)
                if (
                    connection.execute(select(bases.c.id).where(bases.c.id == base_id)).first()
                    is None
                ):
                    connection.execute(
                        insert(bases).values(
                            id=base_id,
                            tenant_id=tenant,
                            name="旧演示导入",
                            description="保留之前上传的解析正文；原文件未保留。",
                            department_id=UUID(row["department_id"]),
                            created_by=owner,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                connection.execute(
                    insert(documents).values(
                        id=doc_id,
                        tenant_id=tenant,
                        base_id=base_id,
                        version_id=UUID(row["version_id"]),
                        file_name=row["file_name"],
                        document_type="CONTRACT",
                        media_type="application/pdf"
                        if row["file_name"].lower().endswith(".pdf")
                        else "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document",
                        size_bytes=0,
                        file_hash=row["file_hash"],
                        original=None,
                        status=row["status"],
                        state_version=0,
                        created_by=owner,
                        created_at=now,
                        updated_at=now,
                    )
                )
                old_chunks = (
                    connection.exec_driver_sql(
                        "SELECT * FROM chunks WHERE version_id=?", (row["version_id"],)
                    )
                    .mappings()
                    .all()
                )
                for chunk in old_chunks:
                    connection.execute(
                        insert(chunks).values(
                            id=UUID(chunk["chunk_id"]),
                            tenant_id=tenant,
                            document_id=doc_id,
                            sequence=chunk["sequence"],
                            page_number=chunk["page_number"],
                            heading_path=json.loads(chunk["heading_path"]),
                            content=chunk["content"],
                            content_hash=chunk["content_hash"],
                        )
                    )


class SQLiteKnowledgeRepository(KnowledgeStore):
    def __init__(self, path: Path) -> None:
        super().__init__(SQLiteKnowledgeDatabase(path))

    def upload(
        self, actor: ActorContext, file_name: str, media_type: str, content: bytes
    ) -> KnowledgeDocument:
        """Compatibility helper for old local tests; HTTP uses the shared base-scoped API."""
        service = KnowledgeManagementService(self)
        base = next(
            (
                base
                for base in self.list_bases(actor)
                if base.name == "默认知识库" and base.created_by == actor.user_id
            ),
            None,
        )
        if base is None:
            base = service.create_base(actor, "默认知识库", "")
        document, _ = service.upload(actor, base.id, file_name, "CONTRACT", media_type, content)
        if document.status == "FAILED":
            raise ContractOpsError(
                code=document.error_code or "document_parse_failed",
                message=document.error_message or "parse failed",
                status_code=422,
            )
        return self._legacy_document(document)
