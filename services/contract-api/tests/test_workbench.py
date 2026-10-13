from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_knowledge_management import DOCX, _base, _docx, _upload

from contractops.config import Settings
from contractops.context import DataScope
from contractops.errors import ContractOpsError
from contractops.main import create_app


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        settings=Settings(environment="test"),
        local_knowledge_path=tmp_path / "workbench.sqlite3",
        local_token="workbench-test-only",
    )
    with TestClient(app, headers={"Authorization": "Bearer workbench-test-only"}) as value:
        yield value


def post(client: TestClient, path: str, body: dict, key: str | None = None) -> dict:
    response = client.post(path, json=body, headers={"Idempotency-Key": key or str(uuid4())})
    assert response.status_code in {200, 201}, response.text
    return response.json()


def scenario(client: TestClient) -> tuple[str, dict, dict, dict]:
    base = _base(client)
    source = _upload(
        client,
        base,
        kind="CONTRACT",
        content=_docx("Payment CNY 1,000.00. Delivery due on 2026-11-01. 合同付款期限30天。"),
        name="contract.docx",
    ).json()
    policy = _upload(
        client,
        base,
        kind="POLICY",
        content=_docx("Payment policy: deposits require human approval. 付款制度：交付后付款。"),
        name="policy.docx",
    ).json()
    contract = post(
        client,
        "/v1/contracts",
        dict(
            title="Knowledge-linked purchase",
            contract_type="PURCHASE",
            counterparty_name="Local Test Supplier",
            department_id=str(UUID(int=3)),
            amount="1000.00",
            currency="CNY",
        ),
    )
    body = dict(document_id=source["id"], expected_version=contract["state_version"])
    linked = post(
        client, f"/v1/workspace/contracts/{contract['id']}/documents", body, "bind-replay-test"
    )
    assert (
        post(
            client, f"/v1/workspace/contracts/{contract['id']}/documents", body, "bind-replay-test"
        )
        == linked
    )
    current = client.get(f"/v1/contracts/{contract['id']}").json()
    assert len(current["versions"]) == 1 and current["versions"][0]["status"] == "READY"
    return base, source, policy, current


def switch_reviewer(client: TestClient) -> None:
    value = post(client, "/workspace/local-identity", {"identity": "reviewer"})
    client.headers["Authorization"] = "Bearer " + value["token"]
    assert client.get("/workspace/session").json()["local_identity"] == "reviewer"


@pytest.mark.parametrize(
    "decision,status",
    [("APPROVE", "APPROVED"), ("REJECT", "REJECTED"), ("REQUEST_CHANGES", "CHANGES_REQUESTED")],
)
def test_local_knowledge_contract_review_and_human_approval(
    client: TestClient, decision: str, status: str
) -> None:
    base, source, _, contract = scenario(client)
    identifier = contract["id"]
    detail = client.get(f"/v1/workspace/contracts/{identifier}").json()
    assert detail["document_id"] == source["id"] and detail["candidates"]
    post(
        client,
        f"/v1/workspace/contracts/{identifier}:confirm",
        dict(
            expected_version=contract["state_version"],
            candidate_ids=[c["id"] for c in detail["candidates"]],
        ),
    )
    contract = client.get(f"/v1/contracts/{identifier}").json()
    report = post(client, f"/v1/workspace/contracts/{identifier}/reviews", {"policy_base_id": base})
    assert report["status"] == "CURRENT" and report["mode"] == "RULES_AND_EVIDENCE"
    assert report["citations"] and any(pair["policy_sources"] for pair in report["pairs"])
    assert not report["semantic_suggestions"]
    body = dict(expected_version=contract["state_version"], report_id=report["id"])
    instance = post(
        client, f"/v1/workspace/contracts/{identifier}:submit", body, "submit-replay-test"
    )
    assert (
        post(client, f"/v1/workspace/contracts/{identifier}:submit", body, "submit-replay-test")[
            "id"
        ]
        == instance["id"]
    )
    assert client.get("/v1/approval-tasks").status_code == 403
    switch_reviewer(client)
    tasks = client.get("/v1/approval-tasks").json()
    assert len(tasks) == 1 and tasks[0]["instance_id"] == instance["id"]
    step = tasks[0]
    claim_body = {"expected_version": step["state_version"]}
    claimed = post(
        client, f"/v1/approval-steps/{step['id']}:claim", claim_body, "claim-replay-test"
    )
    assert (
        post(client, f"/v1/approval-steps/{step['id']}:claim", claim_body, "claim-replay-test")
        == claimed
    )
    assert (
        client.post(
            f"/v1/approval-steps/{step['id']}:claim",
            json=claim_body,
            headers={"Idempotency-Key": "claim-stale-request"},
        ).status_code
        == 409
    )
    body = dict(
        expected_version=claimed["state_version"],
        decision=decision,
        comment="已核对合同与制度原文。",
    )
    decided = post(client, f"/v1/approval-steps/{step['id']}:decide", body, "decide-replay-test")
    assert decided["decision_comment"] == body["comment"]
    assert (
        post(client, f"/v1/approval-steps/{step['id']}:decide", body, "decide-replay-test")
        == decided
    )
    assert client.get(f"/v1/contracts/{identifier}").json()["status"] == status
    assert client.get("/v1/workspace/approval-history?handled=true").json()[0]["status"] == status
    assert client.get("/v1/approval-tasks").json() == []
    assert (
        client.post(
            f"/v1/workspace/contracts/{identifier}:confirm",
            json={
                "expected_version": contract["state_version"],
                "candidate_ids": [],
            },
        ).status_code
        == 403
    )


def test_report_policy_change_blocks_approval_but_allows_return_for_changes(
    client: TestClient,
) -> None:
    base, _, policy, contract = scenario(client)
    identifier = contract["id"]
    report = post(client, f"/v1/workspace/contracts/{identifier}/reviews", {"policy_base_id": base})
    post(
        client,
        f"/v1/workspace/contracts/{identifier}:submit",
        dict(expected_version=contract["state_version"], report_id=report["id"]),
    )
    step = client.app.state.approval_service._workflow.list_tasks(
        client.app.state.jwt_decoder.decode(client.app.state.local_review_token)
    )[0]
    changed = client.post(
        f"/v1/knowledge/documents/{policy['id']}/versions",
        params=dict(
            expected_version=policy["state_version"], file_name="policy-v2.docx", media_type=DOCX
        ),
        content=_docx("Payment policy changed: manual review required."),
    )
    assert changed.status_code == 201
    assert client.get(f"/v1/workspace/contracts/{identifier}/review").json()["status"] == "STALE"
    switch_reviewer(client)
    claimed = post(
        client, f"/v1/approval-steps/{step.id}:claim", {"expected_version": step.state_version}
    )
    body = dict(expected_version=claimed["state_version"], decision="APPROVE")
    assert (
        client.post(
            f"/v1/approval-steps/{step.id}:decide",
            json=body,
            headers={"Idempotency-Key": "stale-review-block"},
        ).status_code
        == 409
    )
    body["decision"] = "REQUEST_CHANGES"
    post(client, f"/v1/approval-steps/{step.id}:decide", body)
    assert client.get(f"/v1/contracts/{identifier}").json()["status"] == "CHANGES_REQUESTED"


def test_settled_decision_and_submission_replay_survive_policy_changes(client: TestClient) -> None:
    base, _, policy, contract = scenario(client)
    identifier = contract["id"]
    report = post(client, f"/v1/workspace/contracts/{identifier}/reviews", {"policy_base_id": base})
    submit_body = dict(expected_version=contract["state_version"], report_id=report["id"])
    instance = post(
        client, f"/v1/workspace/contracts/{identifier}:submit", submit_body, "stable-submit"
    )
    switch_reviewer(client)
    step = client.get("/v1/approval-tasks").json()[0]
    claim = post(
        client,
        f"/v1/approval-steps/{step['id']}:claim",
        {"expected_version": step["state_version"]},
    )
    body = dict(expected_version=claim["state_version"], decision="APPROVE", comment="人工核对通过")
    decided = post(client, f"/v1/approval-steps/{step['id']}:decide", body, "stable-decision")
    reviewer_headers = dict(client.headers)
    client.headers["Authorization"] = "Bearer workbench-test-only"
    assert (
        client.post(
            f"/v1/knowledge/documents/{policy['id']}:archive",
            json={"expected_version": policy["state_version"]},
        ).status_code
        == 200
    )
    assert client.get(f"/v1/workspace/contracts/{identifier}/review").json()["status"] == "STALE"
    assert (
        post(client, f"/v1/workspace/contracts/{identifier}:submit", submit_body, "stable-submit")
        == instance
    )
    client.headers.update(reviewer_headers)
    assert (
        post(client, f"/v1/approval-steps/{step['id']}:decide", body, "stable-decision") == decided
    )


def test_concurrent_claim_has_one_version_winner(client: TestClient) -> None:
    base, _, _, contract = scenario(client)
    report = post(
        client, f"/v1/workspace/contracts/{contract['id']}/reviews", {"policy_base_id": base}
    )
    post(
        client,
        f"/v1/workspace/contracts/{contract['id']}:submit",
        {"expected_version": contract["state_version"], "report_id": report["id"]},
    )
    switch_reviewer(client)
    step = client.get("/v1/approval-tasks").json()[0]

    def claim(key: str) -> int:
        return client.post(
            f"/v1/approval-steps/{step['id']}:claim",
            json={"expected_version": step["state_version"]},
            headers={"Idempotency-Key": key},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("concurrent-claim-one", "concurrent-claim-two")))
    assert sorted(results) == [200, 409]
    current = client.get(f"/v1/approval-instances/{step['instance_id']}").json()["steps"][0]
    assert current["state_version"] == step["state_version"] + 1


def test_linked_versions_diff_and_report_expiration(client: TestClient) -> None:
    base, source, _, contract = scenario(client)
    identifier = contract["id"]
    report = post(client, f"/v1/workspace/contracts/{identifier}/reviews", {"policy_base_id": base})
    revised = client.post(
        f"/v1/knowledge/documents/{source['id']}/versions",
        params=dict(
            expected_version=source["state_version"], file_name="contract-v2.docx", media_type=DOCX
        ),
        content=_docx("Payment CNY 2,000.00. Delivery due on 2026-12-01."),
    ).json()
    assert client.get(f"/v1/workspace/contracts/{identifier}/review").json()["status"] == "STALE"
    post(
        client,
        f"/v1/workspace/contracts/{identifier}/documents",
        dict(expected_version=contract["state_version"], document_id=revised["id"]),
    )
    diff = client.get(f"/v1/workspace/contracts/{identifier}/diff").json()
    assert {entry["change"] for entry in diff["entries"]} == {"ADDED", "REMOVED"}
    assert {entry["source"]["document_id"] for entry in diff["entries"]} == {
        source["id"],
        revised["id"],
    }
    assert (
        client.get(f"/v1/workspace/contracts/{identifier}/review?report_id={report['id']}").json()[
            "status"
        ]
        == "STALE"
    )


def test_workbench_scope_isolation_and_document_type_validation(client: TestClient) -> None:
    base, _, policy, contract = scenario(client)
    assert (
        client.post(
            f"/v1/workspace/contracts/{contract['id']}/documents",
            json=dict(expected_version=contract["state_version"], document_id=policy["id"]),
            headers={"Idempotency-Key": "cannot-bind-policy"},
        ).status_code
        == 409
    )
    service = client.app.state.contract_workbench
    actor = client.app.state.jwt_decoder.decode("workbench-test-only")
    for outsider in (
        replace(actor, tenant_id=uuid4()),
        replace(actor, user_id=uuid4(), data_scope=DataScope.OWN),
    ):
        assert service.list_contracts(outsider) == ()
        with pytest.raises(ContractOpsError):
            service.link(outsider, UUID(contract["id"]))
    assert (
        client.post(
            f"/v1/contracts/{contract['id']}:submit",
            headers={"Idempotency-Key": "no-report-submit"},
        ).status_code
        == 409
    )
    for path in (
        "/v1/workspace/contracts",
        "/v1/workspace/approval-history",
        f"/v1/workspace/contracts/{contract['id']}",
    ):
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_local_contracts_persist_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "restart.sqlite3"
    settings = Settings(environment="test")
    with TestClient(
        create_app(settings=settings, local_knowledge_path=path, local_token="restart-token"),
        headers={"Authorization": "Bearer restart-token"},
    ) as first:
        _, _, _, contract = scenario(first)
    with TestClient(
        create_app(settings=settings, local_knowledge_path=path, local_token="restart-token"),
        headers={"Authorization": "Bearer restart-token"},
    ) as second:
        assert second.get("/v1/workspace/contracts").json()[0]["id"] == contract["id"]
        assert second.get(f"/v1/workspace/contracts/{contract['id']}").json()["document_id"]
