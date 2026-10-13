from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from contractops.api.dependencies import authenticated_actor
from contractops.application.knowledge import (
    EXCERPT_LENGTH,
    MAX_TERMS,
    KnowledgeHit,
    KnowledgeService,
    SearchTerm,
    search_terms,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.demo.knowledge import SQLiteDemoKnowledgeRepository, create_demo_app
from contractops.errors import ContractOpsError
from contractops.main import create_app

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TOKEN = "test-local-only-token"


def actor() -> ActorContext:
    return ActorContext(
        uuid4(),
        uuid4(),
        frozenset({Role.CONTRACT_OWNER}),
        frozenset({uuid4()}),
        DataScope.DEPARTMENT,
    )


def docx(text: str = "付款期限为验收后30天。Payment due in 30 days.") -> bytes:
    from xml.sax.saxutils import escape

    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return result.getvalue()


@pytest.mark.parametrize("query", ["", "  ", "%_", "???", "a" * 301, "请问"])
def test_empty_or_unsearchable_queries_are_rejected(query: str) -> None:
    with pytest.raises(ContractOpsError) as error:
        search_terms(query)
    assert error.value.code == "knowledge_query_invalid"


def test_chinese_and_english_terms_are_literal_deduplicated_and_bounded() -> None:
    terms = search_terms("什么时候付款 PAYMENT payment")
    assert SearchTerm("payment", 4) in terms
    assert SearchTerm("付款", 1) in terms
    assert len([term for term in terms if term.text == "payment"]) == 1
    assert len(search_terms(" ".join(f"w{index}" for index in range(50)))) == MAX_TERMS
    assert search_terms("PAYMENT") == search_terms("payment")


def test_local_upload_persists_evidence_and_answer_is_exact_source(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.sqlite3"
    current = actor()
    repository = SQLiteDemoKnowledgeRepository(path)
    content = docx()
    uploaded = repository.upload(current, "../../contract.docx", DOCX_TYPE, content)
    service = KnowledgeService(SQLiteDemoKnowledgeRepository(path))
    documents = service.list_documents(current)
    assert documents == (uploaded,)
    assert documents[0].file_name == "contract.docx"
    assert documents[0].file_hash == hashlib.sha256(content).hexdigest()
    answer = service.ask(current, "什么时候付款")
    assert answer.status == "EVIDENCE_FOUND"
    assert answer.mode == "EXTRACTIVE"
    citation = answer.citations[0]
    assert citation.source.contract_id == uploaded.contract_id
    assert citation.source.version_id == uploaded.version_id
    assert citation.source.heading_path == ()
    assert citation.source.page_number is None  # DOCX has no stable PDF page coordinate.
    assert (
        citation.excerpt == citation.source.content[citation.excerpt_start : citation.excerpt_end]
    )
    assert (
        hashlib.sha256(citation.source.content.encode()).hexdigest() == citation.source.content_hash
    )
    assert citation.excerpt in answer.answer
    assert "不是模型" in answer.answer


@pytest.mark.parametrize("scope", [DataScope.OWN, DataScope.DEPARTMENT, DataScope.TENANT])
def test_other_tenant_is_excluded_before_ranking_and_pagination(
    tmp_path: Path,
    scope: DataScope,
) -> None:
    repository = SQLiteDemoKnowledgeRepository(tmp_path / "knowledge.sqlite3")
    current = actor()
    mine = repository.upload(current, "mine.docx", DOCX_TYPE, docx("付款30天"))
    other = replace(current, tenant_id=uuid4(), data_scope=scope)
    # The foreign record has stronger phrase matches; it must not displace mine at limit=1.
    foreign = repository.upload(other, "secret.docx", DOCX_TYPE, docx("付款30天 PAYMENT"))
    service = KnowledgeService(repository)
    results = service.search(current, "付款30天 PAYMENT", limit=1)
    assert results[0].source.contract_id == mine.contract_id
    assert service.list_documents(current, limit=1)[0].contract_id == mine.contract_id
    assert service.search(current, "PAYMENT", contract_id=foreign.contract_id) == ()
    assert (
        service.ask(current, "PAYMENT", contract_id=foreign.contract_id).status
        == "INSUFFICIENT_EVIDENCE"
    )


def test_department_and_own_scopes_include_owner_but_exclude_other_departments(
    tmp_path: Path,
) -> None:
    repository = SQLiteDemoKnowledgeRepository(tmp_path / "knowledge.sqlite3")
    owner = actor()
    document = repository.upload(owner, "contract.docx", DOCX_TYPE, docx())
    service = KnowledgeService(repository)
    peer = replace(owner, user_id=uuid4())
    outsider = replace(peer, department_ids=frozenset({uuid4()}))
    assert service.search(peer, "付款")[0].source.contract_id == document.contract_id
    assert service.search(outsider, "付款") == ()
    assert service.list_documents(outsider) == ()
    assert service.search(replace(peer, data_scope=DataScope.OWN), "付款") == ()
    owner_without_department = replace(owner, department_ids=frozenset())
    assert service.search(owner_without_department, "付款")
    assert service.search(replace(outsider, data_scope=DataScope.TENANT), "付款")


def test_no_match_is_not_a_success_and_role_checks_precede_repository(tmp_path: Path) -> None:
    repository = SQLiteDemoKnowledgeRepository(tmp_path / "knowledge.sqlite3")
    current = actor()
    repository.upload(current, "contract.docx", DOCX_TYPE, docx())
    service = KnowledgeService(repository)
    answer = service.ask(current, "unfindablekeyword")
    assert answer.status == "INSUFFICIENT_EVIDENCE" and not answer.citations
    with pytest.raises(ContractOpsError) as error:
        service.search(replace(current, roles=frozenset()), "付款")
    assert error.value.status_code == 403


@pytest.mark.parametrize("limit", [0, -1, 11])
def test_service_rejects_unbounded_search(tmp_path: Path, limit: int) -> None:
    service = KnowledgeService(SQLiteDemoKnowledgeRepository(tmp_path / "knowledge.sqlite3"))
    with pytest.raises(ContractOpsError) as error:
        service.search(actor(), "payment", limit=limit)
    assert error.value.status_code == 422


def test_excerpt_offsets_survive_unicode_lowercase_expansion() -> None:
    content = "İ" * 200 + "付款30天" + "x" * 2000
    hit = KnowledgeHit(
        uuid4(),
        "contract",
        uuid4(),
        1,
        "contract.pdf",
        "a" * 64,
        uuid4(),
        0,
        2,
        (),
        content,
        "b" * 64,
        4,
    )

    class Repository:
        def list_documents(self, *args: object, **kwargs: object) -> tuple[object, ...]:
            raise AssertionError("not used")

        def search(self, *args: object, **kwargs: object) -> tuple[KnowledgeHit, ...]:
            return (hit,)

    citation = KnowledgeService(Repository()).search(actor(), "付款")[0]
    assert citation.excerpt_start == 50
    assert len(citation.excerpt) == EXCERPT_LENGTH
    assert citation.excerpt == content[citation.excerpt_start : citation.excerpt_end]
    assert "付款30天" in citation.excerpt


def test_demo_api_requires_auth_and_upload_search_ask_roundtrip(tmp_path: Path) -> None:
    application = create_demo_app(tmp_path / "demo.sqlite3", token=TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(application) as client:
        base = client.post("/v1/knowledge/bases", headers=headers, json={"name": "Roundtrip"})
        upload_url = f"/v1/knowledge/bases/{base.json()['id']}/documents"
        assert client.get("/").status_code == 200
        assert TOKEN not in client.get("/").text
        assert client.get("/v1/knowledge/documents").status_code == 401
        assert client.post("/v1/knowledge/ask", json={"question": "付款"}).status_code == 401
        assert (
            client.post(
                upload_url,
                params={"file_name": "c.docx", "media_type": DOCX_TYPE},
                content=docx(),
            ).status_code
            == 401
        )
        uploaded = client.post(
            upload_url,
            headers=headers,
            params={"file_name": "c.docx", "media_type": DOCX_TYPE},
            content=docx(),
        )
        assert uploaded.status_code == 201, uploaded.text
        documents = client.get("/v1/knowledge/documents", headers=headers)
        assert documents.json()[0]["searchable"] is True
        response = client.get("/v1/knowledge/search", headers=headers, params={"query": "付款"})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json()["results"][0]["excerpt"]
        response = client.post("/v1/knowledge/ask", headers=headers, json={"question": "付款"})
        assert response.status_code == 200
        assert response.json()["status"] == "EVIDENCE_FOUND"
        assert response.json()["citations"][0]["version_id"] == uploaded.json()["version_id"]
        response = client.post(
            "/v1/knowledge/ask",
            headers=headers,
            json={"question": "付款", "contract_id": str(uuid4())},
        )
        assert response.json()["status"] == "INSUFFICIENT_EVIDENCE"
        assert (
            client.get(
                "/v1/knowledge/search", headers=headers, params={"query": "付款", "limit": 10000}
            ).status_code
            == 422
        )
        assert (
            client.post("/v1/knowledge/ask", headers=headers, json={"question": "   "}).status_code
            == 422
        )


def test_demo_rejects_nontext_pdf_unsupported_and_oversized_files(tmp_path: Path) -> None:
    repository = SQLiteDemoKnowledgeRepository(tmp_path / "demo.sqlite3")
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = io.BytesIO()
    writer.write(pdf)
    for media_type, data, code in [
        ("application/pdf", pdf.getvalue(), "document_text_missing"),
        ("text/plain", b"hello", "document_media_type_unsupported"),
        (DOCX_TYPE, b"x" * (5 * 1024 * 1024 + 1), "document_size_invalid"),
    ]:
        with pytest.raises(ContractOpsError) as error:
            repository.upload(actor(), "file", media_type, data)
        assert error.value.code == code


def test_text_pdf_upload_preserves_real_page_number(tmp_path: Path) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (Payment due in 45 days.) Tj ET")
    page[NameObject("/Contents")] = stream
    buffer = io.BytesIO()
    writer.write(buffer)
    application = create_demo_app(tmp_path / "demo.sqlite3", token=TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(application) as client:
        base = client.post("/v1/knowledge/bases", headers=headers, json={"name": "PDF"})
        response = client.post(
            f"/v1/knowledge/bases/{base.json()['id']}/documents",
            headers=headers,
            params={"file_name": "payment.pdf", "media_type": "application/pdf"},
            content=buffer.getvalue(),
        )
        assert response.status_code == 201, response.text
        answer = client.post("/v1/knowledge/ask", headers=headers, json={"question": "Payment"})
    assert answer.json()["citations"][0]["page_number"] == 1
    assert answer.json()["citations"][0]["excerpt"] == "Payment due in 45 days."


def test_original_app_uses_injected_knowledge_repository(tmp_path: Path) -> None:
    current = actor()
    repository = SQLiteDemoKnowledgeRepository(tmp_path / "demo.sqlite3")
    repository.upload(current, "c.docx", DOCX_TYPE, docx())
    application = create_app(knowledge_repository=repository)
    application.dependency_overrides[authenticated_actor] = lambda: current
    with TestClient(application) as client:
        response = client.post("/v1/knowledge/ask", json={"question": "付款"})
    assert response.status_code == 200
    assert response.json()["mode"] == "EXTRACTIVE"


def test_production_app_never_mounts_demo_upload(tmp_path: Path) -> None:
    with TestClient(create_app()) as client:
        assert client.post("/demo/documents").status_code == 404


def test_demo_real_http_upload_and_ask(tmp_path: Path) -> None:
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    application = create_demo_app(tmp_path / "http.sqlite3", token=TOKEN)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            log_level="error",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "local HTTP server failed to start"
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert client.get("/").status_code == 200
            assert client.get("/v1/knowledge/documents").status_code == 401
            headers = {"Authorization": f"Bearer {TOKEN}"}
            base = client.post("/v1/knowledge/bases", headers=headers, json={"name": "HTTP"})
            response = client.post(
                f"/v1/knowledge/bases/{base.json()['id']}/documents",
                headers=headers,
                params={"file_name": "http.docx", "media_type": DOCX_TYPE},
                content=docx(),
            )
            assert response.status_code == 201
            answer = client.post("/v1/knowledge/ask", headers=headers, json={"question": "Payment"})
            assert answer.status_code == 200
            assert answer.json()["status"] == "EVIDENCE_FOUND"
            assert "30 days" in answer.json()["citations"][0]["excerpt"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()
