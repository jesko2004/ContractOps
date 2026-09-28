from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from contractops.application.documents import DocumentRepository
from contractops.context import ActorContext
from contractops.domain.contract import ContractVersion, ContractVersionStatus
from contractops.domain.document import (
    DocumentChunk,
    DocumentFinding,
    FindingKind,
    FindingOrigin,
)
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database


class PostgresDocumentRepository(DocumentRepository):
    def __init__(self, database: Database) -> None:
        self._database = database

    def load_version(self, actor: ActorContext, version_id: UUID) -> ContractVersion | None:
        with self._database.transaction(actor) as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT id, contract_id, version_number, status, object_key,
                               file_name, media_type, size_bytes, content_hash,
                               created_by, created_at
                        FROM contract_versions WHERE id = :id
                        """
                    ),
                    {"id": version_id},
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _version_from_row(row)

    def mark_uploaded_and_enqueue(
        self,
        actor: ActorContext,
        version_id: UUID,
        *,
        expected_size: int,
        expected_hash: str,
        etag: str | None,
    ) -> bool:
        with self._database.transaction(actor) as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT status, size_bytes, content_hash
                        FROM contract_versions WHERE id = :id FOR UPDATE
                        """
                    ),
                    {"id": version_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ContractOpsError(
                    code="contract_version_not_found",
                    message="contract version was not found",
                    status_code=404,
                )
            if row["status"] in {"UPLOADED", "PARSING", "READY"}:
                return True
            if row["status"] != "UPLOADING":
                raise ContractOpsError(
                    code="document_upload_not_completable",
                    message="document upload cannot be completed from its current state",
                    status_code=409,
                )
            if row["size_bytes"] != expected_size or row["content_hash"] != expected_hash:
                raise ContractOpsError(
                    code="document_upload_declaration_changed",
                    message="document upload declaration changed before completion",
                    status_code=409,
                )
            connection.execute(
                text(
                    """
                    UPDATE contract_versions
                    SET status = 'UPLOADED', uploaded_at = now(), object_etag = :etag
                    WHERE id = :id
                    """
                ),
                {"id": version_id, "etag": etag},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ingestion_jobs (tenant_id, contract_version_id, next_attempt_at)
                    VALUES (:tenant_id, :version_id, now())
                    ON CONFLICT (tenant_id, contract_version_id) DO NOTHING
                    """
                ),
                {"tenant_id": actor.tenant_id, "version_id": version_id},
            )
            return False

    def list_chunks(self, actor: ActorContext, version_id: UUID) -> tuple[DocumentChunk, ...]:
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT id, contract_version_id, sequence, page_number,
                               heading_path, content, content_hash
                        FROM contract_chunks
                        WHERE contract_version_id = :version_id
                        ORDER BY sequence
                        """
                    ),
                    {"version_id": version_id},
                )
                .mappings()
                .all()
            )
        return tuple(_chunk_from_row(row) for row in rows)

    def list_findings(self, actor: ActorContext, version_id: UUID) -> tuple[DocumentFinding, ...]:
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT id, contract_version_id, source_chunk_id, kind, origin,
                               title, normalized_value, source_page, source_text,
                               confidence, model_name, model_output_hash, created_at
                        FROM document_findings
                        WHERE contract_version_id = :version_id
                        ORDER BY created_at, id
                        """
                    ),
                    {"version_id": version_id},
                )
                .mappings()
                .all()
            )
        return tuple(_finding_from_row(row) for row in rows)

    def version_belongs_to_contract(
        self, actor: ActorContext, contract_id: UUID, version_id: UUID
    ) -> bool:
        with self._database.transaction(actor) as connection:
            return (
                connection.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM contract_versions
                            WHERE id = :version_id AND contract_id = :contract_id
                        )
                        """
                    ),
                    {"version_id": version_id, "contract_id": contract_id},
                ).scalar_one()
                is True
            )


def _version_from_row(row: RowMapping) -> ContractVersion:
    return ContractVersion(
        id=cast(UUID, row["id"]),
        contract_id=cast(UUID, row["contract_id"]),
        version_number=cast(int, row["version_number"]),
        status=ContractVersionStatus(cast(str, row["status"])),
        object_key=cast(str, row["object_key"]),
        file_name=cast(str, row["file_name"]),
        media_type=cast(str, row["media_type"]),
        size_bytes=cast(int, row["size_bytes"]),
        content_hash=cast(str, row["content_hash"]),
        created_by=cast(UUID, row["created_by"]),
        created_at=cast(datetime, row["created_at"]),
    )


def _chunk_from_row(row: RowMapping) -> DocumentChunk:
    return DocumentChunk(
        id=cast(UUID, row["id"]),
        contract_version_id=cast(UUID, row["contract_version_id"]),
        sequence=cast(int, row["sequence"]),
        page_number=cast(int | None, row["page_number"]),
        heading_path=tuple(cast(list[str] | None, row["heading_path"]) or ()),
        content=cast(str, row["content"]),
        content_hash=cast(str, row["content_hash"]),
    )


def _finding_from_row(row: RowMapping) -> DocumentFinding:
    return DocumentFinding(
        id=cast(UUID, row["id"]),
        contract_version_id=cast(UUID, row["contract_version_id"]),
        source_chunk_id=cast(UUID, row["source_chunk_id"]),
        kind=FindingKind(cast(str, row["kind"])),
        origin=FindingOrigin(cast(str, row["origin"])),
        title=cast(str, row["title"]),
        normalized_value=cast(str | None, row["normalized_value"]),
        source_page=cast(int | None, row["source_page"]),
        source_text=cast(str, row["source_text"]),
        confidence=float(row["confidence"]),
        model_name=cast(str | None, row["model_name"]),
        model_output_hash=cast(str | None, row["model_output_hash"]),
        created_at=cast(datetime, row["created_at"]),
    )
