from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from contractops.application.documents import SUPPORTED_MEDIA_TYPES
from contractops.context import ActorContext, Role
from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import ParsedBlock, parse_document

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
DOCUMENT_TYPES = frozenset({"CONTRACT", "POLICY", "TEMPLATE"})
DOCUMENT_STATUSES = frozenset({"PARSING", "READY", "FAILED", "ARCHIVED"})


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    id: UUID
    name: str
    description: str
    department_id: UUID
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    document_count: int = 0


@dataclass(frozen=True, slots=True)
class ManagedDocument:
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
    error_code: str | None = None
    error_message: str | None = None
    lease_expires_at: datetime | None = None
    original_available: bool = True
    root_id: UUID | None = None
    version_number: int = 1
    is_current: bool = True
    parse_details: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ManagedChunk:
    id: UUID
    sequence: int
    page_number: int | None
    heading_path: tuple[str, ...]
    content: str
    content_hash: str
    source_pages: tuple[int, ...] = ()
    source_id: str | None = None
    kind: str = "text"


class KnowledgeManagementRepository(Protocol):
    def create_base(
        self, actor: ActorContext, name: str, description: str, department_id: UUID
    ) -> KnowledgeBase: ...

    def list_bases(self, actor: ActorContext) -> tuple[KnowledgeBase, ...]: ...

    def get_base(self, actor: ActorContext, base_id: UUID) -> KnowledgeBase: ...

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
    ) -> tuple[ManagedDocument, ...]: ...

    def get_document(self, actor: ActorContext, document_id: UUID) -> ManagedDocument: ...

    def document_chunks(
        self, actor: ActorContext, document_id: UUID, *, limit: int, offset: int
    ) -> tuple[ManagedChunk, ...]: ...

    def save_upload(
        self,
        actor: ActorContext,
        base: KnowledgeBase,
        file_name: str,
        document_type: str,
        media_type: str,
        content: bytes,
        file_hash: str,
    ) -> tuple[ManagedDocument, bool]: ...

    def original(self, actor: ActorContext, document_id: UUID) -> bytes: ...

    def versions(self, actor: ActorContext, document_id: UUID) -> tuple[ManagedDocument, ...]: ...

    def save_version(
        self,
        actor: ActorContext,
        document: ManagedDocument,
        expected_version: int,
        file_name: str,
        media_type: str,
        content: bytes,
    ) -> tuple[ManagedDocument, bool]: ...

    def begin_retry(
        self, actor: ActorContext, document_id: UUID, expected_version: int
    ) -> ManagedDocument: ...

    def complete_parse(
        self,
        actor: ActorContext,
        document: ManagedDocument,
        blocks: tuple[ParsedBlock, ...],
        error: ContractOpsError | None,
        parse_details: dict[str, object] | None = None,
    ) -> ManagedDocument: ...

    def archive(
        self, actor: ActorContext, document_id: UUID, expected_version: int
    ) -> ManagedDocument: ...


class InspectorParser(Protocol):
    def process(
        self, actor: ActorContext, document: ManagedDocument, content: bytes
    ) -> ManagedDocument: ...
    def retry(
        self, actor: ActorContext, document: ManagedDocument, content: bytes
    ) -> ManagedDocument: ...
    def sync(self, actor: ActorContext, document: ManagedDocument) -> ManagedDocument: ...
    def state(self, actor: ActorContext, document: ManagedDocument) -> dict[str, object]: ...
    def preview(self, actor: ActorContext, document: ManagedDocument, page: int) -> bytes: ...


class KnowledgeManagementService:
    def __init__(
        self, repository: KnowledgeManagementRepository, inspector: InspectorParser | None = None
    ) -> None:
        self.repository = repository
        self.inspector = inspector

    @staticmethod
    def require_reader(actor: ActorContext) -> None:
        if not actor.roles:
            raise ContractOpsError(
                code="authorization_denied", message="knowledge access denied", status_code=403
            )

    @staticmethod
    def require_writer(actor: ActorContext) -> None:
        if not actor.has_any_role(Role.CONTRACT_OWNER, Role.TENANT_ADMIN):
            raise ContractOpsError(
                code="authorization_denied",
                message="knowledge write access denied",
                status_code=403,
            )

    @staticmethod
    def can_manage(actor: ActorContext, document: ManagedDocument) -> bool:
        return actor.has_any_role(Role.CONTRACT_OWNER, Role.TENANT_ADMIN) and (
            document.created_by == actor.user_id or Role.TENANT_ADMIN in actor.roles
        )

    def create_base(
        self, actor: ActorContext, name: str, description: str, department_id: UUID | None = None
    ) -> KnowledgeBase:
        self.require_writer(actor)
        name, description = name.strip(), description.strip()
        if not 1 <= len(name) <= 100 or len(description) > 2000:
            raise ContractOpsError(
                code="knowledge_base_invalid",
                message="invalid name or description",
                status_code=422,
            )
        department_id = department_id or next(iter(sorted(actor.department_ids)), None)
        if department_id is None or not actor.can_access_department(department_id):
            raise ContractOpsError(
                code="authorization_denied", message="department is not accessible", status_code=403
            )
        return self.repository.create_base(actor, name, description, department_id)

    def list_bases(self, actor: ActorContext) -> tuple[KnowledgeBase, ...]:
        self.require_reader(actor)
        return self.repository.list_bases(actor)

    def documents(
        self,
        actor: ActorContext,
        base_id: UUID,
        *,
        query: str = "",
        document_type: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[ManagedDocument, ...]:
        self.require_reader(actor)
        self.repository.get_base(actor, base_id)
        if (
            len(query) > 200
            or not 1 <= limit <= 100
            or not 0 <= offset <= 10000
            or (document_type is not None and document_type not in DOCUMENT_TYPES)
            or (status is not None and status not in DOCUMENT_STATUSES)
        ):
            raise ContractOpsError(
                code="knowledge_filter_invalid", message="invalid document filter", status_code=422
            )
        return self.repository.documents(
            actor,
            base_id,
            query=query.strip(),
            document_type=document_type,
            status=status,
            limit=limit,
            offset=offset,
        )

    def _parse(
        self, actor: ActorContext, document: ManagedDocument, content: bytes
    ) -> ManagedDocument:
        if document.media_type == "application/pdf" and self.inspector:
            return self.inspector.process(actor, document, content)
        try:
            if hashlib.sha256(content).hexdigest() != document.file_hash:
                raise ContractOpsError(
                    code="document_hash_mismatch",
                    message="stored original failed checksum verification",
                )
            blocks = parse_document(content, document.media_type)
            if not blocks:
                raise ContractOpsError(
                    code="document_text_missing",
                    message="未提取到文字；扫描 PDF 需要 OCR，当前尚未接入。",
                )
            if len(blocks) > 2000 or sum(len(item.content) for item in blocks) > 500_000:
                raise ContractOpsError(
                    code="document_text_limit", message="文档正文超过本版处理上限。"
                )
        except ContractOpsError as error:
            return self.repository.complete_parse(actor, document, (), error)
        except Exception:
            return self.repository.complete_parse(
                actor,
                document,
                (),
                ContractOpsError(
                    code="document_parse_failed",
                    message="解析失败，可重试；若持续失败请检查文件格式。",
                ),
            )
        return self.repository.complete_parse(
            actor,
            document,
            blocks,
            None,
            {
                "engine": "NATIVE",
                "quality_status": "UNASSESSED",
                "ocr": {"provider": None},
                "issues": ["使用基础解析；未执行 OCR 或完整质量评估。"],
            },
        )

    def document_chunks(
        self, actor: ActorContext, document_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> tuple[ManagedChunk, ...]:
        self.require_reader(actor)
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise ContractOpsError(
                code="knowledge_filter_invalid", message="invalid chunk pagination", status_code=422
            )
        return self.repository.document_chunks(actor, document_id, limit=limit, offset=offset)

    def upload(
        self,
        actor: ActorContext,
        base_id: UUID,
        file_name: str,
        document_type: str,
        media_type: str,
        content: bytes,
    ) -> tuple[ManagedDocument, bool]:
        self.require_writer(actor)
        base = self.repository.get_base(actor, base_id)
        if document_type not in DOCUMENT_TYPES:
            raise ContractOpsError(
                code="document_type_invalid", message="invalid document category", status_code=422
            )
        if media_type not in SUPPORTED_MEDIA_TYPES:
            raise ContractOpsError(
                code="document_media_type_unsupported",
                message="only PDF and DOCX are supported",
                status_code=415,
            )
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ContractOpsError(
                code="document_size_invalid",
                message="文件大小必须为 1 字节至 5 MiB。",
                status_code=413,
            )
        safe_name = file_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not safe_name or len(safe_name) > 255:
            raise ContractOpsError(
                code="document_name_invalid", message="invalid file name", status_code=422
            )
        document, replayed = self.repository.save_upload(
            actor,
            base,
            safe_name,
            document_type,
            media_type,
            content,
            hashlib.sha256(content).hexdigest(),
        )
        if replayed:
            return document, True
        return self._parse(actor, document, content), False

    def _manageable(self, actor: ActorContext, document_id: UUID) -> ManagedDocument:
        self.require_writer(actor)
        document = self.repository.get_document(actor, document_id)
        if not self.can_manage(actor, document):
            raise ContractOpsError(
                code="authorization_denied",
                message="only document owner or scoped tenant admin can manage it",
                status_code=403,
            )
        return document

    def versions(self, actor: ActorContext, document_id: UUID) -> tuple[ManagedDocument, ...]:
        self.require_reader(actor)
        return self.repository.versions(actor, document_id)

    def original(self, actor: ActorContext, document_id: UUID) -> tuple[ManagedDocument, bytes]:
        self.require_reader(actor)
        document = self.repository.get_document(actor, document_id)
        content = self.repository.original(actor, document_id)
        if hashlib.sha256(content).hexdigest() != document.file_hash:
            raise ContractOpsError(
                code="document_hash_mismatch", message="原件校验失败。", status_code=409
            )
        return document, content

    def upload_version(
        self,
        actor: ActorContext,
        document_id: UUID,
        expected_version: int,
        file_name: str,
        media_type: str,
        content: bytes,
    ) -> tuple[ManagedDocument, bool]:
        document = self._manageable(actor, document_id)
        if not content or len(content) > MAX_UPLOAD_BYTES:
            raise ContractOpsError(
                code="document_size_invalid", message="单文件最多5 MiB。", status_code=413
            )
        if media_type not in SUPPORTED_MEDIA_TYPES:
            raise ContractOpsError(
                code="document_media_type_unsupported", message="只支持PDF/DOCX。", status_code=415
            )
        safe_name = file_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not safe_name or len(safe_name) > 255:
            raise ContractOpsError(
                code="document_name_invalid", message="文件名不合法。", status_code=422
            )
        value, replayed = self.repository.save_version(
            actor, document, expected_version, safe_name, media_type, content
        )
        return (value, True) if replayed else (self._parse(actor, value, content), False)

    def processing(self, actor: ActorContext, document_id: UUID) -> dict[str, object]:
        self.require_reader(actor)
        document = self.repository.get_document(actor, document_id)
        if self.inspector:
            return self.inspector.state(actor, document)
        return {
            "engine": "NATIVE",
            "status": document.status,
            "progress": 0 if document.status == "PARSING" else 100,
        }

    def preview(self, actor: ActorContext, document_id: UUID, page: int) -> bytes:
        self.require_reader(actor)
        document = self.repository.get_document(actor, document_id)
        if not 1 <= page <= 2000:
            raise ContractOpsError(
                code="document_page_invalid", message="页码不合法。", status_code=422
            )
        if not self.inspector or document.media_type != "application/pdf":
            raise ContractOpsError(
                code="inspector_preview_unavailable",
                message="无 Inspector PDF 预览。",
                status_code=409,
            )
        return self.inspector.preview(actor, document, page)

    def sync(self, actor: ActorContext, document_id: UUID) -> ManagedDocument:
        document = self._manageable(actor, document_id)
        if not self.inspector:
            raise ContractOpsError(
                code="inspector_not_configured", message="Inspector 未配置。", status_code=503
            )
        return self.inspector.sync(actor, document)

    def retry(
        self, actor: ActorContext, document_id: UUID, expected_version: int
    ) -> ManagedDocument:
        document = self._manageable(actor, document_id)
        now = datetime.now(UTC)
        if document.status != "FAILED" and not (
            document.status == "PARSING"
            and document.lease_expires_at is not None
            and document.lease_expires_at <= now
        ):
            raise ContractOpsError(
                code="document_retry_not_allowed",
                message="仅失败或处理租约已过期的文档可重试。",
                status_code=409,
            )
        content = self.repository.original(actor, document_id)
        current = self.repository.begin_retry(actor, document_id, expected_version)
        if current.media_type == "application/pdf" and self.inspector:
            return self.inspector.retry(actor, current, content)
        return self._parse(actor, current, content)

    def archive(
        self, actor: ActorContext, document_id: UUID, expected_version: int
    ) -> ManagedDocument:
        document = self._manageable(actor, document_id)
        if document.status == "PARSING":
            raise ContractOpsError(
                code="document_busy",
                message="文档正在解析，请结束或超时后再操作。",
                status_code=409,
            )
        return self.repository.archive(actor, document_id, expected_version)
