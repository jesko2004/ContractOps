from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from contractops.application.contracts import AddContractVersionCommand, ContractService
from contractops.application.documents import DocumentService, ObjectMetadata
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.contract import (
    Contract,
    ContractStatus,
    ContractVersion,
    ContractVersionStatus,
)
from contractops.domain.document import DocumentChunk
from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import (
    extract_rule_findings,
    parse_document,
    validate_model_candidates,
)

TENANT_ID = UUID("00000000-0000-0000-0000-000000000101")
USER_ID = UUID("00000000-0000-0000-0000-000000000102")
DEPARTMENT_ID = UUID("00000000-0000-0000-0000-000000000103")
CONTRACT_ID = UUID("00000000-0000-0000-0000-000000000104")
VERSION_1_ID = UUID("00000000-0000-0000-0000-000000000105")
VERSION_2_ID = UUID("00000000-0000-0000-0000-000000000106")
NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _actor() -> ActorContext:
    return ActorContext(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        roles=frozenset({Role.CONTRACT_OWNER}),
        department_ids=frozenset({DEPARTMENT_ID}),
        data_scope=DataScope.DEPARTMENT,
    )


def _version(version_id: UUID, number: int, *, content_hash: str = "a" * 64) -> ContractVersion:
    return ContractVersion(
        id=version_id,
        contract_id=CONTRACT_ID,
        version_number=number,
        status=ContractVersionStatus.UPLOADING,
        object_key=f"tenants/{TENANT_ID}/contracts/{CONTRACT_ID}/{version_id}.docx",
        file_name="contract.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=128,
        content_hash=content_hash,
        created_by=USER_ID,
        created_at=NOW,
    )


class Ledger:
    def __init__(self) -> None:
        self.versions = [
            _version(VERSION_1_ID, 1),
            _version(VERSION_2_ID, 2, content_hash="b" * 64),
        ]

    def create_contract(self, *args: object, **kwargs: object) -> tuple[Contract, bool]:
        raise AssertionError("not used")

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None:
        if contract_id != CONTRACT_ID:
            return None
        return Contract(
            id=CONTRACT_ID,
            tenant_id=TENANT_ID,
            contract_number="C-1",
            title="Office services",
            contract_type="SERVICE",
            counterparty_name="Supplier",
            department_id=DEPARTMENT_ID,
            amount=None,
            currency=None,
            valid_from=None,
            valid_until=None,
            status=ContractStatus.DRAFT,
            current_version_id=VERSION_2_ID,
            state_version=0,
            created_by=USER_ID,
            created_at=NOW,
            updated_at=NOW,
            versions=tuple(self.versions),
        )

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]:
        return self.versions[0], False


class Repository:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.enqueued = False
        self.chunks = {
            VERSION_1_ID: (
                DocumentChunk(VERSION_1_ID, VERSION_1_ID, 0, 1, (), "Payment in 30 days", "old"),
                DocumentChunk(UUID(int=201), VERSION_1_ID, 1, 2, (), "Legacy clause", "removed"),
            ),
            VERSION_2_ID: (
                DocumentChunk(VERSION_2_ID, VERSION_2_ID, 0, 1, (), "Payment in 45 days", "new"),
                DocumentChunk(UUID(int=202), VERSION_2_ID, 1, 3, (), "New audit clause", "added"),
            ),
        }

    def load_version(self, actor: ActorContext, version_id: UUID) -> ContractVersion | None:
        return next((item for item in self.ledger.versions if item.id == version_id), None)

    def mark_uploaded_and_enqueue(self, *args: object, **kwargs: object) -> bool:
        self.enqueued = True
        return False

    def list_chunks(self, actor: ActorContext, version_id: UUID) -> tuple[DocumentChunk, ...]:
        return self.chunks.get(version_id, ())

    def list_findings(self, actor: ActorContext, version_id: UUID) -> tuple[object, ...]:
        return ()

    def version_belongs_to_contract(
        self, actor: ActorContext, contract_id: UUID, version_id: UUID
    ) -> bool:
        return contract_id == CONTRACT_ID and self.load_version(actor, version_id) is not None


class Store:
    def presign_put(self, object_key: str, *, expires: timedelta) -> str:
        return f"https://objects.example/{object_key}?expires={int(expires.total_seconds())}"

    def stat(self, object_key: str) -> ObjectMetadata:
        return ObjectMetadata(size_bytes=128, etag="etag-1")


def _service() -> tuple[DocumentService, Repository]:
    ledger = Ledger()
    repository = Repository(ledger)
    service = DocumentService(
        repository,
        Store(),
        ContractService(ledger),
        max_size_bytes=1024,
        upload_expiry_seconds=900,
    )
    return service, repository


def test_upload_ticket_is_limited_to_supported_media_and_completion_is_enqueued() -> None:
    service, repository = _service()
    ticket = service.initiate_upload(
        _actor(),
        CONTRACT_ID,
        AddContractVersionCommand(
            file_name="contract.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=128,
            content_hash="a" * 64,
        ),
        idempotency_key="upload-001",
    )
    assert ticket.expires_in_seconds == 900
    assert ticket.version.id == VERSION_1_ID

    repository.chunks[VERSION_1_ID] = ()
    replayed = service.complete_upload(_actor(), VERSION_1_ID)

    assert replayed is False
    assert repository.enqueued is True


def test_unsupported_upload_is_rejected_before_ledger_mutation() -> None:
    service, _ = _service()
    with pytest.raises(ContractOpsError) as captured:
        service.initiate_upload(
            _actor(),
            CONTRACT_ID,
            AddContractVersionCommand("contract.txt", "text/plain", 10, "a" * 64),
            idempotency_key="upload-002",
        )
    assert captured.value.code == "document_media_type_unsupported"


def test_completed_upload_replay_still_enforces_contract_scope() -> None:
    service, _ = _service()
    outsider = ActorContext(
        tenant_id=TENANT_ID,
        user_id=UUID("00000000-0000-0000-0000-000000000998"),
        roles=frozenset({Role.CONTRACT_OWNER}),
        department_ids=frozenset({UUID("00000000-0000-0000-0000-000000000999")}),
        data_scope=DataScope.DEPARTMENT,
    )

    with pytest.raises(ContractOpsError) as captured:
        service.complete_upload(outsider, VERSION_1_ID)

    assert captured.value.code == "authorization_denied"


def test_docx_parser_preserves_heading_path_and_rule_evidence() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Payment</w:t></w:r></w:p>
        <w:p><w:r><w:t>Total CNY 12,500.00, due 2026-12-31.</w:t></w:r></w:p>
      </w:body>
    </w:document>"""
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("word/document.xml", xml)

    blocks = parse_document(
        package.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    findings = extract_rule_findings(blocks[1])

    assert blocks[1].heading_path == ("Payment",)
    assert {item.normalized_value for item in findings} >= {"12500.00", "2026-12-31"}
    assert all(item.source_text in blocks[1].content for item in findings)


def test_invalid_model_output_is_rejected_as_a_whole() -> None:
    raw = [
        {
            "kind": "RISK",
            "title": "Unlimited liability",
            "source_text": "not present in source",
            "confidence": 0.9,
        }
    ]
    candidates = validate_model_candidates(
        raw,
        source_text="Liability is capped.",
        model_name="qwen",
    )
    assert candidates == ()


def test_version_diff_keeps_both_versions_source_references() -> None:
    service, _ = _service()
    result = service.diff(_actor(), CONTRACT_ID, VERSION_1_ID, VERSION_2_ID)

    assert result.entries
    assert result.entries[0].old_chunk is not None
    assert result.entries[0].new_chunk is not None
    assert result.entries[0].old_chunk.contract_version_id == VERSION_1_ID
    assert result.entries[0].new_chunk.contract_version_id == VERSION_2_ID
