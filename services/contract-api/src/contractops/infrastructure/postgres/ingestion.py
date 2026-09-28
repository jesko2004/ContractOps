from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import text

from contractops.infrastructure.document_parsing import FindingCandidate, ParsedBlock
from contractops.infrastructure.postgres.database import WorkerDatabase


@dataclass(frozen=True, slots=True)
class ClaimedIngestionJob:
    id: UUID
    tenant_id: UUID
    version_id: UUID
    object_key: str
    media_type: str
    expected_hash: str
    attempt_count: int
    max_attempts: int


class PostgresIngestionStore:
    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database

    def claim(self, worker_id: str, *, lease_seconds: int) -> ClaimedIngestionJob | None:
        with self._database.transaction() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        WITH candidate AS (
                            SELECT job.id
                            FROM ingestion_jobs AS job
                            JOIN contract_versions AS version
                              ON version.tenant_id = job.tenant_id
                             AND version.id = job.contract_version_id
                            WHERE (
                                job.status IN ('PENDING', 'RETRY_WAIT')
                                AND COALESCE(job.next_attempt_at, now()) <= now()
                            ) OR (
                                job.status = 'RUNNING' AND job.lease_expires_at < now()
                            )
                            ORDER BY job.created_at
                            FOR UPDATE OF job SKIP LOCKED
                            LIMIT 1
                        )
                        UPDATE ingestion_jobs AS job
                        SET status = 'RUNNING', current_step = 'READ_OBJECT',
                            attempt_count = attempt_count + 1,
                            lease_owner = :worker_id,
                            lease_expires_at = now() + make_interval(secs => :lease_seconds),
                            error_code = NULL, error_message = NULL, updated_at = now()
                        FROM candidate, contract_versions AS version
                        WHERE job.id = candidate.id
                          AND version.tenant_id = job.tenant_id
                          AND version.id = job.contract_version_id
                        RETURNING job.id, job.tenant_id, job.contract_version_id,
                                  version.object_key, version.media_type,
                                  version.content_hash, job.attempt_count, job.max_attempts
                        """
                    ),
                    {"worker_id": worker_id, "lease_seconds": lease_seconds},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            connection.execute(
                text("UPDATE contract_versions SET status = 'PARSING' WHERE id = :id"),
                {"id": row["contract_version_id"]},
            )
            return ClaimedIngestionJob(
                id=row["id"],
                tenant_id=row["tenant_id"],
                version_id=row["contract_version_id"],
                object_key=row["object_key"],
                media_type=row["media_type"],
                expected_hash=row["content_hash"],
                attempt_count=row["attempt_count"],
                max_attempts=row["max_attempts"],
            )

    def succeed(
        self,
        job: ClaimedIngestionJob,
        blocks: tuple[ParsedBlock, ...],
        findings: tuple[tuple[int, FindingCandidate], ...],
    ) -> None:
        with self._database.transaction() as connection:
            locked = (
                connection.execute(
                    text(
                        """
                    SELECT status, lease_owner FROM ingestion_jobs
                    WHERE id = :id FOR UPDATE
                    """
                    ),
                    {"id": job.id},
                )
                .mappings()
                .one()
            )
            if locked["status"] != "RUNNING":
                return
            chunk_ids: dict[int, UUID] = {}
            for block in blocks:
                chunk_id = uuid4()
                chunk_ids[block.sequence] = chunk_id
                connection.execute(
                    text(
                        """
                        INSERT INTO contract_chunks (
                            id, tenant_id, contract_version_id, sequence,
                            page_number, heading_path, content, content_hash
                        ) VALUES (
                            :id, :tenant_id, :version_id, :sequence,
                            :page_number, :heading_path, :content, :content_hash
                        )
                        """
                    ),
                    {
                        "id": chunk_id,
                        "tenant_id": job.tenant_id,
                        "version_id": job.version_id,
                        "sequence": block.sequence,
                        "page_number": block.page_number,
                        "heading_path": list(block.heading_path),
                        "content": block.content,
                        "content_hash": hashlib.sha256(block.content.encode()).hexdigest(),
                    },
                )
            for sequence, finding in findings:
                connection.execute(
                    text(
                        """
                        INSERT INTO document_findings (
                            tenant_id, contract_version_id, source_chunk_id,
                            kind, origin, title, normalized_value, source_page,
                            source_text, confidence, model_name, model_output_hash
                        ) VALUES (
                            :tenant_id, :version_id, :chunk_id,
                            :kind, :origin, :title, :normalized_value, :source_page,
                            :source_text, :confidence, :model_name, :model_output_hash
                        )
                        """
                    ),
                    {
                        "tenant_id": job.tenant_id,
                        "version_id": job.version_id,
                        "chunk_id": chunk_ids[sequence],
                        "kind": finding.kind.value,
                        "origin": finding.origin.value,
                        "title": finding.title,
                        "normalized_value": finding.normalized_value,
                        "source_page": blocks[sequence].page_number,
                        "source_text": finding.source_text,
                        "confidence": finding.confidence,
                        "model_name": finding.model_name,
                        "model_output_hash": finding.model_output_hash,
                    },
                )
            page_numbers = [block.page_number for block in blocks if block.page_number is not None]
            connection.execute(
                text(
                    """
                    UPDATE contract_versions
                    SET status = 'READY', parser_version = 'contractops-parser/1',
                        page_count = :page_count, ready_at = now()
                    WHERE id = :version_id
                    """
                ),
                {"version_id": job.version_id, "page_count": max(page_numbers, default=None)},
            )
            connection.execute(
                text(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'SUCCEEDED', current_step = 'COMPLETE',
                        lease_owner = NULL, lease_expires_at = NULL,
                        completed_at = now(), updated_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": job.id},
            )
            payload = json.dumps(
                {
                    "version_id": str(job.version_id),
                    "chunk_count": len(blocks),
                    "finding_count": len(findings),
                },
                separators=(",", ":"),
            )
            connection.execute(
                text(
                    """
                    INSERT INTO outbox_events (
                        tenant_id, event_type, aggregate_type, aggregate_id, payload
                    ) VALUES (
                        :tenant_id, 'contract.document.ready', 'contract_version',
                        :version_id, CAST(:payload AS jsonb)
                    )
                    """
                ),
                {"tenant_id": job.tenant_id, "version_id": job.version_id, "payload": payload},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        tenant_id, action, resource_type, resource_id,
                        outcome, category, metadata
                    ) VALUES (
                        :tenant_id, 'document.ingestion.complete', 'contract_version',
                        :version_id, 'SUCCEEDED', 'OPERATION', CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {"tenant_id": job.tenant_id, "version_id": job.version_id, "metadata": payload},
            )

    def fail(self, job: ClaimedIngestionJob, *, code: str, message: str) -> None:
        terminal = job.attempt_count >= job.max_attempts or code in {
            "document_hash_mismatch",
            "document_parse_failed",
            "document_media_type_unsupported",
        }
        status = "FAILED" if terminal else "RETRY_WAIT"
        with self._database.transaction() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ingestion_jobs
                    SET status = :status, current_step = 'FAILED',
                        lease_owner = NULL, lease_expires_at = NULL,
                        next_attempt_at = CASE WHEN :terminal THEN NULL
                            ELSE now() + make_interval(secs => :delay_seconds) END,
                        error_code = :code, error_message = :message,
                        completed_at = CASE WHEN :terminal THEN now() ELSE NULL END,
                        updated_at = now()
                    WHERE id = :id
                    """
                ),
                {
                    "id": job.id,
                    "status": status,
                    "terminal": terminal,
                    "delay_seconds": min(300, 2**job.attempt_count),
                    "code": code[:100],
                    "message": message[:1000],
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE contract_versions SET status = :status
                    WHERE id = :version_id
                    """
                ),
                {"version_id": job.version_id, "status": "FAILED" if terminal else "UPLOADED"},
            )
            metadata = json.dumps(
                {"error_code": code[:100], "terminal": terminal},
                separators=(",", ":"),
            )
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        tenant_id, action, resource_type, resource_id,
                        outcome, category, metadata
                    ) VALUES (
                        :tenant_id, 'document.ingestion.failed', 'contract_version',
                        :version_id, 'FAILED', 'OPERATION', CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "tenant_id": job.tenant_id,
                    "version_id": job.version_id,
                    "metadata": metadata,
                },
            )
