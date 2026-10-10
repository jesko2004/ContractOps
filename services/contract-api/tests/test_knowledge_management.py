from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from contractops.application.knowledge import KnowledgeService
from contractops.application.knowledge_management import KnowledgeManagementService
from contractops.config import Settings
from contractops.context import ActorContext, DataScope, Role
from contractops.errors import ContractOpsError
from contractops.infrastructure.knowledge_schema import documents
from contractops.infrastructure.knowledge_store import SQLiteKnowledgeRepository
from contractops.main import create_app

TOKEN = "knowledge-management-test-only"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _actor() -> ActorContext:
    return ActorContext(
        uuid4(),
        uuid4(),
        frozenset({Role.CONTRACT_OWNER}),
        frozenset({uuid4()}),
        DataScope.DEPARTMENT,
    )


def _docx(content: str = "Payment due in 30 days. 付款期限30天。") -> bytes:
    from xml.sax.saxutils import escape

    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{escape(content)}</w:t></w:r></w:p></w:body></w:document>",
        )
    return result.getvalue()


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    application = create_app(
        settings=Settings(environment="test"),
        local_knowledge_path=tmp_path / "shared.sqlite3",
        local_token=TOKEN,
    )
    with TestClient(application, headers={"Authorization": f"Bearer {TOKEN}"}) as value:
        yield value


def _base(client: TestClient, name: str = "采购合同与制度") -> str:
    response = client.post(
        "/v1/knowledge/bases", json={"name": name, "description": "真实保存的说明"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(
    client: TestClient,
    base_id: str,
    *,
    content: bytes | None = None,
    kind: str = "CONTRACT",
    name: str = "contract.docx",
):
    return client.post(
        f"/v1/knowledge/bases/{base_id}/documents",
        params={
            "file_name": name,
            "media_type": DOCX,
            "document_type": kind,
        },
        content=_docx() if content is None else content,
    )


def test_main_and_local_share_workspace_and_management_routes(tmp_path: Path) -> None:
    with (
        TestClient(create_app()) as main,
        TestClient(
            create_app(
                local_knowledge_path=tmp_path / "same.sqlite3",
                local_token=TOKEN,
            )
        ) as local,
    ):
        assert main.get("/").text == local.get("/").text
        assert "/demo/documents" not in local.get("/").text
        for path in ("/v1/knowledge/bases", "/workspace/session"):
            assert main.get(path).status_code == 401
            assert local.get(path).status_code == 401
        session = local.get(
            "/workspace/session", headers={"Authorization": f"Bearer {TOKEN}"}
        ).json()
        assert session["mode"] == "LOCAL_SQLITE"
        assert session["capabilities"]["contracts"]
        assert session["capabilities"]["approvals"]
        assert (
            local.get(
                f"/v1/contracts/{uuid4()}", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 404
        )
        assert (
            local.get(
                "/v1/approval-tasks", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 403
        )


def test_bases_categories_filters_and_actual_metadata(client: TestClient) -> None:
    base_id = _base(client)
    for index, kind in enumerate(("CONTRACT", "POLICY", "TEMPLATE")):
        response = _upload(
            client, base_id, kind=kind, content=_docx(f"Payment {index}"), name=f"{kind}.docx"
        )
        assert response.status_code == 201
        assert response.json()["document_type"] == kind
        assert response.json()["original_available"]
    bases = client.get("/v1/knowledge/bases").json()
    assert bases[0]["description"] == "真实保存的说明" and bases[0]["document_count"] == 3
    rows = client.get(
        f"/v1/knowledge/bases/{base_id}/documents",
        params={"document_type": "POLICY", "query": "policy"},
    ).json()
    assert len(rows) == 1 and rows[0]["document_type"] == "POLICY"
    assert rows[0]["created_at"] and rows[0]["updated_at"]
    assert rows[0]["status"] == "READY" and rows[0]["chunk_count"] == 1
    assert "original" not in rows[0]
    record = client.get("/v1/knowledge/documents/" + rows[0]["id"])
    assert record.status_code == 200 and record.json() == rows[0]
    assert (
        client.get(f"/v1/knowledge/bases/{base_id}/documents", params={"query": "%_"}).json() == []
    )
    assert (
        client.get(
            f"/v1/knowledge/bases/{base_id}/documents", params={"status": "UNKNOWN"}
        ).status_code
        == 422
    )


def test_upload_dedup_and_archive_removes_from_retrieval(client: TestClient) -> None:
    base_id = _base(client)
    content = _docx()
    first = _upload(client, base_id, content=content)
    second = _upload(client, base_id, content=content)
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/v1/knowledge/bases").json()[0]["document_count"] == 1
    assert (
        client.post("/v1/knowledge/ask", json={"question": "Payment", "base_id": base_id}).json()[
            "status"
        ]
        == "EVIDENCE_FOUND"
    )
    document = first.json()
    archived = client.post(
        f"/v1/knowledge/documents/{document['id']}:archive",
        json={"expected_version": document["state_version"]},
    )
    assert archived.status_code == 200 and archived.json()["status"] == "ARCHIVED"
    assert archived.json()["original_available"]  # Archiving does not delete user data.
    assert not archived.json()["searchable"]
    assert (
        client.post("/v1/knowledge/ask", json={"question": "Payment", "base_id": base_id}).json()[
            "status"
        ]
        == "INSUFFICIENT_EVIDENCE"
    )
    assert (
        len(
            client.get(
                f"/v1/knowledge/bases/{base_id}/documents", params={"status": "ARCHIVED"}
            ).json()
        )
        == 1
    )
    assert (
        client.post(
            f"/v1/knowledge/documents/{document['id']}:archive",
            json={"expected_version": document["state_version"]},
        ).status_code
        == 409
    )


def test_failed_document_is_visible_and_retry_uses_original(client: TestClient) -> None:
    base_id = _base(client)
    response = _upload(client, base_id, content=b"broken docx")
    assert response.status_code == 201
    document = response.json()
    assert document["status"] == "FAILED" and document["can_retry"]
    assert document["error_code"] == "document_parse_failed"
    assert (
        client.get(f"/v1/knowledge/bases/{base_id}/documents", params={"status": "FAILED"}).json()[
            0
        ]["id"]
        == document["id"]
    )
    retried = client.post(
        f"/v1/knowledge/documents/{document['id']}:retry",
        json={"expected_version": document["state_version"]},
    )
    assert retried.status_code == 200 and retried.json()["status"] == "FAILED"
    assert retried.json()["state_version"] > document["state_version"]
    assert retried.json()["file_hash"] == document["file_hash"]
    assert (
        client.post(
            f"/v1/knowledge/documents/{document['id']}:retry",
            json={"expected_version": document["state_version"]},
        ).status_code
        == 409
    )


def test_base_selection_is_used_by_ask(client: TestClient) -> None:
    first, second = _base(client, "first"), _base(client, "second")
    _upload(client, first, content=_docx("Payment due"))
    _upload(client, second, content=_docx("Delivery date"))
    assert (
        client.post("/v1/knowledge/ask", json={"question": "Payment", "base_id": second}).json()[
            "status"
        ]
        == "INSUFFICIENT_EVIDENCE"
    )
    citations = client.post(
        "/v1/knowledge/ask", json={"question": "Payment", "base_id": first}
    ).json()["citations"]
    assert citations[0]["base_id"] == first and citations[0]["document_id"]


def test_management_read_write_isolation(tmp_path: Path) -> None:
    repository = SQLiteKnowledgeRepository(tmp_path / "security.sqlite3")
    service = KnowledgeManagementService(repository)
    owner = _actor()
    base = service.create_base(owner, "Private", "")
    doc, _ = service.upload(owner, base.id, "c.docx", "CONTRACT", DOCX, _docx())
    for outsider in (
        replace(owner, tenant_id=uuid4()),
        replace(owner, user_id=uuid4(), department_ids=frozenset({uuid4()})),
        replace(owner, user_id=uuid4(), data_scope=DataScope.OWN),
    ):
        assert service.list_bases(outsider) == ()
        assert KnowledgeService(repository).search(outsider, "Payment") == ()
        with pytest.raises(ContractOpsError) as hidden:
            service.document_chunks(outsider, doc.id)
        assert hidden.value.status_code == 404
        for operation in (
            lambda current=outsider: service.documents(current, base.id),
            lambda current=outsider: service.upload(
                current, base.id, "s.docx", "CONTRACT", DOCX, _docx()
            ),
            lambda current=outsider: service.archive(current, doc.id, doc.state_version),
        ):
            with pytest.raises(ContractOpsError) as error:
                operation()
            assert error.value.status_code == 404
    peer = replace(owner, user_id=uuid4())
    assert service.documents(peer, base.id)[0].id == doc.id
    with pytest.raises(ContractOpsError) as error:
        service.archive(peer, doc.id, doc.state_version)
    assert error.value.status_code == 403
    reader = replace(owner, roles=frozenset({Role.AUDITOR}))
    assert service.documents(reader, base.id)
    assert "Payment" in service.document_chunks(reader, doc.id)[0].content
    with pytest.raises(ContractOpsError):
        service.create_base(reader, "deny", "")


def test_document_content_endpoint_is_protected_and_paginated(client: TestClient) -> None:
    base_id = _base(client)
    doc = _upload(client, base_id).json()
    url = f"/v1/knowledge/documents/{doc['id']}/chunks"
    response = client.get(url, params={"limit": 1})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    chunk = response.json()[0]
    assert "Payment" in chunk["content"]
    assert hashlib.sha256(chunk["content"].encode()).hexdigest() == chunk["content_hash"]
    assert client.get(url, params={"offset": 1}).json() == []
    assert client.get(url, params={"limit": 101}).status_code == 422
    assert client.get(url, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_stale_parser_cannot_commit_after_reclaim(tmp_path: Path) -> None:
    repository = SQLiteKnowledgeRepository(tmp_path / "lease.sqlite3")
    service = KnowledgeManagementService(repository)
    actor = _actor()
    base = service.create_base(actor, "leases", "")
    content = _docx()
    old, _ = repository.save_upload(
        actor, base, "c.docx", "CONTRACT", DOCX, content, hashlib.sha256(content).hexdigest()
    )
    with repository.database.transaction(actor) as connection:
        connection.execute(
            update(documents)
            .where(documents.c.id == old.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    current = service.retry(actor, old.id, old.state_version)
    assert current.status == "READY"
    with pytest.raises(ContractOpsError) as error:
        repository.complete_parse(actor, old, (), ContractOpsError(code="late", message="late"))
    assert error.value.status_code == 409
    assert repository.get_document(actor, old.id).status == "READY"


def test_legacy_demo_text_is_imported_once_and_retained(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    actor = _actor()
    document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4()
    content = "Payment legacy"
    with sqlite3.connect(path) as connection:
        connection.executescript("""CREATE TABLE documents(
          contract_id TEXT, contract_title TEXT, tenant_id TEXT,
          owner_id TEXT, department_id TEXT, version_id TEXT, version_number INTEGER,
          file_name TEXT, status TEXT, file_hash TEXT);
          CREATE TABLE chunks(chunk_id TEXT, version_id TEXT, sequence INTEGER, page_number INTEGER,
          heading_path TEXT, content TEXT, content_hash TEXT);""")
        connection.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,1,'old.pdf','READY',?)",
            (
                str(document_id),
                "old",
                str(actor.tenant_id),
                str(actor.user_id),
                str(next(iter(actor.department_ids))),
                str(version_id),
                "a" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO chunks VALUES(?,?,0,1,?,?,?)",
            (
                str(chunk_id),
                str(version_id),
                json.dumps([]),
                content,
                hashlib.sha256(content.encode()).hexdigest(),
            ),
        )
    for _ in range(2):
        repository = SQLiteKnowledgeRepository(path)
        bases = repository.list_bases(actor)
        assert len(bases) == 1 and bases[0].name == "旧演示导入"
        docs = KnowledgeManagementService(repository).documents(actor, bases[0].id)
        assert len(docs) == 1 and docs[0].id == document_id
        assert not docs[0].original_available
        assert KnowledgeService(repository).search(actor, "Payment")[0].source.chunk_id == chunk_id
        repository.database.dispose()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
