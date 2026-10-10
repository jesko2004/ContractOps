from __future__ import annotations

import json
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_knowledge_management import _actor, _base, _docx, _upload

from contractops.application.knowledge import KnowledgeService
from contractops.config import Settings
from contractops.infrastructure.grounded_model import GroundedModel
from contractops.infrastructure.knowledge_store import SQLiteKnowledgeRepository
from contractops.main import create_app


@pytest.fixture()
def model_server() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {"mode": "valid", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(payload)
            assert self.headers["Authorization"] == "Bearer test-model-private-key"
            context = json.loads(payload["messages"][1]["content"])
            quote = context["evidence"][0]["excerpt"]
            mode = state["mode"]
            if mode == "unavailable":
                self.send_response(503)
                self.end_headers()
                return
            item = {
                "text": "付款期限需结合原文核对。",
                "evidence": [{"citation": 1, "quote": quote}],
            }
            output: dict[str, Any] = {"insufficient": False, "claims": [item]}
            if mode == "bad_quote":
                item["evidence"][0]["quote"] = "fabricated evidence not in document"
            if mode == "bad_index":
                item["evidence"][0]["citation"] = 10
            if mode == "extra":
                output["approve_contract"] = True
            if mode == "insufficient":
                output = {"insufficient": True, "claims": []}
            result = {
                "choices": [
                    {
                        "finish_reason": "length" if mode == "truncated" else "stop",
                        "message": {
                            "content": "bad-json" if mode == "malformed" else json.dumps(output)
                        },
                    }
                ]
            }
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "bad_quote",
        "bad_index",
        "extra",
        "malformed",
        "truncated",
        "unavailable",
        "insufficient",
    ],
)
def test_real_http_model_boundary_and_explicit_fallback(
    tmp_path: Path, model_server, mode: str
) -> None:
    endpoint, state = model_server
    state["mode"] = mode
    settings = Settings(
        environment="test",
        knowledge_model_enabled=True,
        model_base_url=endpoint,
        document_model_name="test-model",
        knowledge_model_api_key="test-model-private-key",
    )
    app = create_app(
        settings=settings, local_knowledge_path=tmp_path / "qa.sqlite3", local_token="qa-test-token"
    )
    with TestClient(app, headers={"Authorization": "Bearer qa-test-token"}) as client:
        base = _base(client)
        doc = _upload(client, base, content=_docx("Payment due in 30 days.")).json()
        response = client.post(
            "/v1/knowledge/ask",
            json={
                "question": "Payment?",
                "document_id": doc["id"],
                "base_id": base,
                "history": ["Payment due?"],
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["scope"] == {"document_id": doc["id"], "base_id": base, "contract_id": None}
        assert result["citations"][0]["document_id"] == doc["id"]
        if mode in {"valid", "insufficient"}:
            assert result["mode"] == "RAG" and not result["fallback_reason"]
            assert result["status"] == (
                "INSUFFICIENT_EVIDENCE" if mode == "insufficient" else "MODEL_ANSWER"
            )
        else:
            assert result["mode"] == "EXTRACTIVE"
            assert result["fallback_reason"] == "MODEL_UNAVAILABLE_OR_INVALID"
            assert "Payment due in 30 days" in result["answer"]
        assert state["requests"][0]["model"] == "test-model"
        assert "test-model-private-key" not in response.text
        count = len(state["requests"])
        empty = client.post(
            "/v1/knowledge/ask", json={"question": "nonexistent-term", "base_id": base}
        ).json()
        assert empty["status"] == "INSUFFICIENT_EVIDENCE" and len(state["requests"]) == count
        assert (
            client.post(
                "/v1/knowledge/ask", json={"question": "Payment", "history": ["x" * 301]}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/v1/knowledge/ask",
                json={"question": "Payment", "document_id": doc["id"], "contract_id": doc["id"]},
            ).status_code
            == 422
        )


def test_archived_evidence_during_model_call_is_not_returned(tmp_path: Path) -> None:
    from contractops.application.knowledge_management import KnowledgeManagementService

    actor = _actor()
    store = SQLiteKnowledgeRepository(tmp_path / "stale.sqlite3")
    management = KnowledgeManagementService(store)
    base = management.create_base(actor, "stale", "")
    doc, _ = management.upload(
        actor,
        base.id,
        "source.docx",
        "CONTRACT",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _docx("Payment"),
    )

    class ArchiveDuringCall:
        def answer(self, *args: Any) -> str:
            management.archive(actor, doc.id, doc.state_version)
            return "stale generated answer"

    answer = KnowledgeService(store, ArchiveDuringCall()).ask(actor, "Payment", base_id=base.id)
    assert answer.mode == "EXTRACTIVE" and answer.citations == ()
    assert "stale generated answer" not in answer.answer


def test_model_rejects_cleartext_remote_and_embedded_credentials() -> None:
    for url in ("http://model.example/v1", "https://user:secret@model.example/v1", "file:///test"):
        with pytest.raises(ValueError):
            GroundedModel(url, "test", None, 1)
