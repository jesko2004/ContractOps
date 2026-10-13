from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, cast

from contractops.application.knowledge_management import ManagedDocument
from contractops.context import ActorContext
from contractops.errors import ContractOpsError
from contractops.infrastructure.knowledge_store import KnowledgeStore
from contractops.infrastructure.pdf_inspector import PDFInspectorClient, map_inspector_result


class KnowledgeInspector:
    """Durable per-version tasks, resumed by authorized sync requests, not by sleeps."""

    def __init__(self, store: KnowledgeStore, client: PDFInspectorClient) -> None:
        self.store, self.client = store, client

    def process(
        self, actor: ActorContext, document: ManagedDocument, content: bytes
    ) -> ManagedDocument:
        self.store.save_inspector_job(
            actor,
            document.id,
            {
                "endpoint": self.client.endpoint,
                "profile_id": self.client.profile,
                "request_key": hashlib.sha256(
                    f"{actor.tenant_id}:{document.version_id}".encode()
                ).hexdigest(),
                "task_id": None,
                "status": "SUBMITTING",
                "progress": 0,
            },
            create=True,
        )
        return self.sync(actor, document)

    def retry(
        self, actor: ActorContext, document: ManagedDocument, content: bytes
    ) -> ManagedDocument:
        job = self.store.inspector_job(actor, document.id)
        if job and job["status"] == "FAILED":
            self.store.save_inspector_job(
                actor,
                document.id,
                {
                    "task_id": None,
                    "status": "SUBMITTING",
                    "progress": 0,
                    "error_code": None,
                    "request_key": hashlib.sha256(
                        f"{actor.tenant_id}:{document.version_id}:{document.state_version}".encode()
                    ).hexdigest(),
                },
                expected_version=document.state_version,
            )
        return self.process(actor, document, content)

    def state(self, actor: ActorContext, document: ManagedDocument) -> dict[str, Any]:
        job = self.store.inspector_job(actor, document.id)
        if job is None:
            return {
                "engine": "NATIVE",
                "status": document.status,
                "progress": 0 if document.status == "PARSING" else 100,
            }
        return {
            key: value
            for key, value in job.items()
            if key in {"status", "progress", "error_code", "task_id", "profile_id", "updated_at"}
        } | {
            "engine": "PDF_INSPECTOR",
            "document_version_id": document.version_id,
        }

    def preview(self, actor: ActorContext, document: ManagedDocument, page: int) -> bytes:
        job = self.store.inspector_job(actor, document.id)
        if not job or not job["task_id"] or job["endpoint"] != self.client.endpoint:
            raise ContractOpsError(
                code="inspector_preview_unavailable",
                message="该版本尚无可预览的 Inspector 原件。",
                status_code=409,
            )
        return self.client.preview(job["task_id"], page)

    def sync(self, actor: ActorContext, document: ManagedDocument) -> ManagedDocument:
        if document.status != "PARSING":
            return document
        job = self.store.inspector_job(actor, document.id)
        if job is None:
            return document
        if job["endpoint"] != self.client.endpoint:
            raise ContractOpsError(
                code="inspector_endpoint_changed",
                message="Inspector 地址已变化，不能将旧任务交给另一服务。",
                status_code=409,
            )
        if document.lease_expires_at and document.lease_expires_at <= datetime.now(UTC):
            document = self.store.begin_retry(actor, document.id, document.state_version)
        try:
            if job["task_id"] is None:
                task = self.client.submit(
                    self.store.original(actor, document.id), job["request_key"], job["profile_id"]
                )
                self.store.save_inspector_job(
                    actor,
                    document.id,
                    {
                        "task_id": task["id"],
                        "status": "QUEUED",
                        "error_code": None,
                    },
                    expected_version=document.state_version,
                )
                job = cast(dict[str, Any], self.store.inspector_job(actor, document.id))
            task = self.client.task(job["task_id"])
            if task.get("id") != job["task_id"]:
                raise ContractOpsError(
                    code="inspector_invalid_result", message="Inspector 返回的任务身份不匹配。"
                )
            status = task.get("status")
            if status not in {"queued", "processing", "ready", "needs_review", "failed"}:
                raise ContractOpsError(
                    code="inspector_invalid_result", message="Inspector 状态不合法。"
                )
            self.store.save_inspector_job(
                actor,
                document.id,
                {
                    "status": status.upper(),
                    "progress": {"queued": 0, "processing": 50}.get(status, 100),
                    "error_code": None,
                },
                expected_version=document.state_version,
            )
            if status in {"queued", "processing"}:
                return self.store.get_document(actor, document.id)
            if status == "failed":
                raise ContractOpsError(
                    code="inspector_task_failed",
                    message="Inspector 解析失败，请检查原件或服务后重试。",
                )
            result = self.client.result(job["task_id"])
            if result.get("status") not in {"ready", "needs_review"}:
                raise ContractOpsError(
                    code="inspector_invalid_result", message="Inspector 结果尚未就绪。"
                )
            blocks = map_inspector_result(result)
            details: dict[str, object] = {
                "engine": "PDF_INSPECTOR",
                "quality_status": "NEEDS_REVIEW"
                if status == "needs_review" or result["status"] == "needs_review"
                else "READY",
                "profile_id": job["profile_id"],
                "task_id": job["task_id"],
                "document": result.get("document", {}),
                "ocr": result.get("ocr", {}),
                "tables": result.get("tables", {}),
                "chunk_quality": result.get("chunk_quality", {}),
            }
            if not blocks:
                details["quality_status"] = "NEEDS_REVIEW"
            return self.store.complete_parse(actor, document, blocks, None, details)
        except ContractOpsError as error:
            if error.code == "inspector_unavailable":
                self.store.save_inspector_job(
                    actor,
                    document.id,
                    {
                        "status": "SUBMISSION_UNKNOWN" if not job["task_id"] else job["status"],
                        "error_code": error.code,
                    },
                    expected_version=document.state_version,
                )
                return self.store.get_document(actor, document.id)
            if error.status_code == 409:
                raise
            self.store.save_inspector_job(
                actor,
                document.id,
                {
                    "status": "FAILED",
                    "progress": 100,
                    "error_code": error.code,
                },
                expected_version=document.state_version,
            )
            return self.store.complete_parse(
                actor, document, (), error, {"engine": "PDF_INSPECTOR", "quality_status": "FAILED"}
            )
