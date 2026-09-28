from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Protocol
from uuid import UUID

from contractops.application.contracts import AddContractVersionCommand, ContractService
from contractops.context import ActorContext, Role
from contractops.domain.contract import ContractVersion
from contractops.domain.document import DiffEntry, DocumentChunk, DocumentFinding, VersionDiff
from contractops.errors import ContractOpsError

SUPPORTED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    size_bytes: int
    etag: str | None


@dataclass(frozen=True, slots=True)
class UploadTicket:
    version: ContractVersion
    upload_url: str
    expires_in_seconds: int


class ObjectStore(Protocol):
    def presign_put(self, object_key: str, *, expires: timedelta) -> str: ...

    def stat(self, object_key: str) -> ObjectMetadata: ...


class DocumentRepository(Protocol):
    def load_version(self, actor: ActorContext, version_id: UUID) -> ContractVersion | None: ...

    def mark_uploaded_and_enqueue(
        self,
        actor: ActorContext,
        version_id: UUID,
        *,
        expected_size: int,
        expected_hash: str,
        etag: str | None,
    ) -> bool: ...

    def list_chunks(self, actor: ActorContext, version_id: UUID) -> tuple[DocumentChunk, ...]: ...

    def list_findings(
        self, actor: ActorContext, version_id: UUID
    ) -> tuple[DocumentFinding, ...]: ...

    def version_belongs_to_contract(
        self, actor: ActorContext, contract_id: UUID, version_id: UUID
    ) -> bool: ...


class DocumentService:
    _WRITE_ROLES = (Role.CONTRACT_OWNER, Role.TENANT_ADMIN)
    _READ_ROLES = (
        Role.CONTRACT_OWNER,
        Role.APPROVER,
        Role.LEGAL_ADMIN,
        Role.FINANCE_APPROVER,
        Role.BUSINESS_APPROVER,
        Role.AUDITOR,
        Role.TENANT_ADMIN,
    )

    def __init__(
        self,
        repository: DocumentRepository,
        object_store: ObjectStore,
        contract_service: ContractService,
        *,
        max_size_bytes: int,
        upload_expiry_seconds: int,
    ) -> None:
        self._repository = repository
        self._object_store = object_store
        self._contract_service = contract_service
        self._max_size_bytes = max_size_bytes
        self._upload_expiry_seconds = upload_expiry_seconds

    def initiate_upload(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
    ) -> UploadTicket:
        if command.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ContractOpsError(
                code="document_media_type_unsupported",
                message="only PDF and DOCX documents are supported",
                status_code=415,
            )
        if command.size_bytes <= 0 or command.size_bytes > self._max_size_bytes:
            raise ContractOpsError(
                code="document_size_invalid",
                message=f"document size must be between 1 and {self._max_size_bytes} bytes",
                status_code=413,
            )
        version, _ = self._contract_service.add_version(
            actor,
            contract_id,
            command,
            idempotency_key=idempotency_key,
        )
        url = self._object_store.presign_put(
            version.object_key,
            expires=timedelta(seconds=self._upload_expiry_seconds),
        )
        return UploadTicket(version, url, self._upload_expiry_seconds)

    def complete_upload(self, actor: ActorContext, version_id: UUID) -> bool:
        if not actor.has_any_role(*self._WRITE_ROLES):
            raise ContractOpsError(
                code="authorization_denied",
                message="the caller cannot complete document uploads",
                status_code=403,
            )
        version = self._find_version(actor, version_id)
        chunks = self._repository.list_chunks(actor, version_id)
        if chunks:
            return True
        metadata = self._object_store.stat(version.object_key)
        if metadata.size_bytes != version.size_bytes:
            raise ContractOpsError(
                code="document_size_mismatch",
                message="uploaded object size does not match the declared size",
                status_code=409,
            )
        return self._repository.mark_uploaded_and_enqueue(
            actor,
            version_id,
            expected_size=version.size_bytes,
            expected_hash=version.content_hash,
            etag=metadata.etag,
        )

    def list_chunks(self, actor: ActorContext, version_id: UUID) -> tuple[DocumentChunk, ...]:
        self._require_reader(actor)
        self._find_version(actor, version_id)
        return self._repository.list_chunks(actor, version_id)

    def list_findings(self, actor: ActorContext, version_id: UUID) -> tuple[DocumentFinding, ...]:
        self._require_reader(actor)
        self._find_version(actor, version_id)
        return self._repository.list_findings(actor, version_id)

    def diff(
        self,
        actor: ActorContext,
        contract_id: UUID,
        from_version_id: UUID,
        to_version_id: UUID,
    ) -> VersionDiff:
        self._require_reader(actor)
        self._contract_service.get_contract(actor, contract_id)
        for version_id in (from_version_id, to_version_id):
            if not self._repository.version_belongs_to_contract(actor, contract_id, version_id):
                raise ContractOpsError(
                    code="contract_version_not_found",
                    message="contract version was not found for this contract",
                    status_code=404,
                )
        old = self._repository.list_chunks(actor, from_version_id)
        new = self._repository.list_chunks(actor, to_version_id)
        matcher = SequenceMatcher(
            a=[chunk.content_hash for chunk in old],
            b=[chunk.content_hash for chunk in new],
            autojunk=False,
        )
        entries: list[DiffEntry] = []
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "replace":
                width = max(old_end - old_start, new_end - new_start)
                for offset in range(width):
                    entries.append(
                        DiffEntry(
                            "REPLACED",
                            old[old_start + offset] if old_start + offset < old_end else None,
                            new[new_start + offset] if new_start + offset < new_end else None,
                        )
                    )
            elif tag == "delete":
                entries.extend(DiffEntry("REMOVED", item, None) for item in old[old_start:old_end])
            else:
                entries.extend(DiffEntry("ADDED", None, item) for item in new[new_start:new_end])
        return VersionDiff(contract_id, from_version_id, to_version_id, tuple(entries))

    def _find_version(self, actor: ActorContext, version_id: UUID) -> ContractVersion:
        self._require_reader(actor)
        version = self._repository.load_version(actor, version_id)
        if version is not None:
            self._contract_service.get_contract(actor, version.contract_id)
            return version
        raise ContractOpsError(
            code="contract_version_not_found",
            message="contract version was not found",
            status_code=404,
        )

    def _require_reader(self, actor: ActorContext) -> None:
        if not actor.has_any_role(*self._READ_ROLES):
            raise ContractOpsError(
                code="authorization_denied",
                message="the caller cannot read document evidence",
                status_code=403,
            )
