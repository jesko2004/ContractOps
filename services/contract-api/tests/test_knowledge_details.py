from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_knowledge_management import DOCX, _actor, _base, _docx, _upload

from contractops.application.knowledge import KnowledgeService
from contractops.application.knowledge_management import KnowledgeManagementService
from contractops.config import Settings
from contractops.errors import ContractOpsError
from contractops.infrastructure.knowledge_inspector import KnowledgeInspector
from contractops.infrastructure.knowledge_schema import documents
from contractops.infrastructure.knowledge_store import SQLiteKnowledgeRepository
from contractops.infrastructure.pdf_inspector import PDFInspectorClient, map_inspector_result
from contractops.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(environment="test"),
        local_knowledge_path=tmp_path / "api.sqlite3",
        local_token="details-test-only",
    )
    with TestClient(app, headers={"Authorization": "Bearer details-test-only"}) as value:
        yield value


def result(status: str = "needs_review") -> dict[str, Any]:
    text = "| Payment | Amount |\n|---|---|\n| Due | 300 |"
    return {
        "status": status,
        "chunks": [
            {
                "id": "source-block",
                "markdown": text,
                "pages": [1, 3],
                "section_path": ["Payment"],
                "kind": "table",
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        ],
        "tables": {"items": [{"id": "table-1"}]},
        "document": {"issues": [{"reason": "ocr_required", "pages": [2]}]},
        "ocr": {"provider": None, "unresolved_pages": [2]},
    }


class FakeInspector(PDFInspectorClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:8090", None, "manual_query", 1)
        self.status = "queued"
        self.lost_response = False
        self.keys: list[str] = []

    def submit(self, content: bytes, request_key: str, profile: str) -> dict[str, Any]:
        self.keys.append(request_key)
        if self.lost_response:
            self.lost_response = False
            raise ContractOpsError(code="inspector_unavailable", message="lost", status_code=503)
        return {"id": "durable-task", "status": "queued"}

    def task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "status": self.status}

    def result(self, task_id: str) -> dict[str, Any]:
        return result()


def test_versions_full_text_original_and_history_exclusion(client) -> None:
    base_id = _base(client)
    old = _upload(client, base_id, content=_docx("old-token Payment")).json()
    url = f"/v1/knowledge/documents/{old['id']}"
    assert client.get(url + "/text").text == "old-token Payment"
    original = client.get(url + "/original")
    assert original.headers["Cache-Control"] == "no-store"
    assert hashlib.sha256(original.content).hexdigest() == old["file_hash"]
    response = client.post(
        url + "/versions",
        params={
            "file_name": "v2.docx",
            "media_type": DOCX,
            "expected_version": old["state_version"],
        },
        content=_docx("new-token Delivery"),
    )
    assert response.status_code == 201, response.text
    current = response.json()
    assert current["version_number"] == 2 and current["is_current"]
    assert current["root_id"] == old["id"] and current["id"] != old["id"]
    versions = client.get(url + "/versions").json()
    assert [doc["version_number"] for doc in versions] == [2, 1]
    assert not versions[1]["is_current"] and not versions[1]["searchable"]
    assert client.get(url + "/original").content == original.content
    assert client.get(url + "/text").text == "old-token Payment"
    assert client.get(f"/v1/knowledge/bases/{base_id}/documents").json()[0]["id"] == current["id"]
    assert (
        client.post("/v1/knowledge/ask", json={"question": "old-token", "base_id": base_id}).json()[
            "citations"
        ]
        == []
    )
    assert (
        client.post(
            url + "/versions",
            params={
                "file_name": "stale.docx",
                "media_type": DOCX,
                "expected_version": old["state_version"],
            },
            content=_docx("stale"),
        ).status_code
        == 409
    )


def test_inspector_lost_submit_recovery_after_reopen_and_multi_page_sources(tmp_path: Path) -> None:
    actor, engine = _actor(), FakeInspector()
    path = tmp_path / "jobs.sqlite3"
    store = SQLiteKnowledgeRepository(path)
    service = KnowledgeManagementService(store, KnowledgeInspector(store, engine))
    base = service.create_base(actor, "Inspector", "")
    engine.lost_response = True
    document, _ = service.upload(
        actor, base.id, "source.pdf", "CONTRACT", "application/pdf", b"%PDF-test"
    )
    assert document.status == "PARSING"
    assert service.processing(actor, document.id)["status"] == "SUBMISSION_UNKNOWN"
    store.database.dispose()
    store = SQLiteKnowledgeRepository(path)
    service = KnowledgeManagementService(store, KnowledgeInspector(store, engine))
    assert service.sync(actor, document.id).status == "PARSING"
    assert len(engine.keys) == 2 and engine.keys[0] == engine.keys[1]
    engine.status = "needs_review"
    ready = service.sync(actor, document.id)
    assert ready.status == "READY" and ready.parse_details
    assert ready.parse_details["quality_status"] == "NEEDS_REVIEW"
    assert ready.parse_details["ocr"] == {"provider": None, "unresolved_pages": [2]}
    chunk = service.document_chunks(actor, document.id)[0]
    assert chunk.source_pages == (1, 3) and chunk.kind == "table"
    count, chunk_id = ready.chunk_count, chunk.id
    assert service.sync(actor, document.id).chunk_count == count
    assert service.document_chunks(actor, document.id)[0].id == chunk_id
    citation = KnowledgeService(store).ask(actor, "Payment").citations[0]
    assert citation.source.source_pages == (1, 3)
    outsider = replace(actor, user_id=uuid4(), department_ids=frozenset())
    for operation in (
        lambda: service.original(outsider, document.id),
        lambda: service.versions(outsider, document.id),
        lambda: service.processing(outsider, document.id),
        lambda: service.sync(outsider, document.id),
    ):
        with pytest.raises(ContractOpsError) as error:
            operation()
        assert error.value.status_code == 404


def test_pending_current_version_does_not_fall_back_and_expired_sync_recovers(
    tmp_path: Path,
) -> None:
    actor, engine = _actor(), FakeInspector()
    store = SQLiteKnowledgeRepository(tmp_path / "current.sqlite3")
    service = KnowledgeManagementService(store, KnowledgeInspector(store, engine))
    base = service.create_base(actor, "versions", "")
    first, _ = service.upload(actor, base.id, "v1.docx", "CONTRACT", DOCX, _docx("Payment old"))
    second, _ = service.upload_version(
        actor, first.id, first.state_version, "v2.pdf", "application/pdf", b"%PDF-v2"
    )
    assert second.status == "PARSING" and KnowledgeService(store).search(actor, "Payment") == ()
    with store.database.transaction(actor) as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == second.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    engine.status = "needs_review"
    ready = service.sync(actor, second.id)
    assert ready.status == "READY" and ready.state_version > second.state_version
    assert KnowledgeService(store).search(actor, "Payment")[0].source.version_number == 2


@pytest.mark.parametrize("change", ["hash", "page", "type"])
def test_invalid_inspector_evidence_is_rejected(change: str) -> None:
    value = result()
    if change == "hash":
        value["chunks"][0]["content_hash"] = "0" * 64
    elif change == "page":
        value["chunks"][0]["pages"] = [0]
    else:
        value["chunks"][0]["kind"] = "script"
    with pytest.raises(ContractOpsError):
        map_inspector_result(value)


def test_inspector_endpoint_rejects_untrusted_cleartext_and_credentials() -> None:
    for endpoint in ("http://remote.example", "http://user:pass@localhost", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            PDFInspectorClient(endpoint, None, "manual_query", 1)


@pytest.mark.parametrize(
    "suffix", ["/original", "/text", "/versions", "/processing", "/pages/1.png"]
)
def test_details_api_requires_authentication(client, suffix: str) -> None:
    document = _upload(client, _base(client)).json()
    response = client.get(
        f"/v1/knowledge/documents/{document['id']}{suffix}",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert response.status_code == 401


def test_late_inspector_progress_cannot_overwrite_a_terminal_result(tmp_path: Path) -> None:
    actor, engine = _actor(), FakeInspector()
    store = SQLiteKnowledgeRepository(tmp_path / "progress.sqlite3")
    service = KnowledgeManagementService(store, KnowledgeInspector(store, engine))
    base = service.create_base(actor, "progress", "")
    document, _ = service.upload(
        actor, base.id, "source.pdf", "CONTRACT", "application/pdf", b"pdf"
    )
    store.save_inspector_job(
        actor,
        document.id,
        {"status": "NEEDS_REVIEW", "progress": 100},
        expected_version=document.state_version,
    )
    store.save_inspector_job(
        actor,
        document.id,
        {"status": "PROCESSING", "progress": 50},
        expected_version=document.state_version,
    )
    assert service.processing(actor, document.id)["status"] == "NEEDS_REVIEW"
    engine.status = "needs_review"
    assert service.sync(actor, document.id).status == "READY"
    store.save_inspector_job(
        actor,
        document.id,
        {"status": "FAILED", "progress": 100},
        expected_version=document.state_version,
    )
    assert service.processing(actor, document.id)["status"] == "NEEDS_REVIEW"
