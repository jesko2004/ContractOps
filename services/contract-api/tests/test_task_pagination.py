import base64
import json
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from contractops.api.dependencies import authenticated_actor, get_approval_service
from contractops.application.approvals import ApprovalService
from contractops.application.task_pagination import decode_cursor, encode_cursor
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.approval import ApprovalStep, ApprovalStepStatus
from contractops.errors import ContractOpsError
from contractops.main import create_app


def _actor() -> ActorContext:
    return ActorContext(uuid4(), uuid4(), frozenset({Role.APPROVER}), frozenset(), DataScope.TENANT)


def _step() -> ApprovalStep:
    now = datetime.now(UTC)
    return ApprovalStep(
        uuid4(),
        uuid4(),
        1,
        "Review",
        Role.LEGAL_ADMIN,
        ApprovalStepStatus.READY,
        None,
        0,
        None,
        None,
        None,
        now,
        now,
    )


def test_cursor_roundtrip_and_rejects_other_principal() -> None:
    actor, step = _actor(), _step()
    cursor = encode_cursor(actor, step)
    decoded = decode_cursor(actor, cursor)
    assert decoded and decoded.created_at == step.created_at and decoded.id == step.id
    with pytest.raises(ContractOpsError):
        decode_cursor(_actor(), cursor)


@pytest.mark.parametrize("value", [None, {}, [], [1], [1, "a", "b", {}, []]])
def test_malformed_cursor_is_client_error(value: object) -> None:
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    with pytest.raises(ContractOpsError) as error:
        decode_cursor(_actor(), cursor)
    assert error.value.status_code == 422


def test_api_paginates_without_changing_array_response_and_caps_limit() -> None:
    actor = _actor()
    first, second = _step(), _step()
    workflow = Mock()
    workflow.list_tasks.side_effect = [(first, second), (second,)]
    service = ApprovalService(workflow, Mock())
    app = create_app()
    app.dependency_overrides[authenticated_actor] = lambda: actor
    app.dependency_overrides[get_approval_service] = lambda: service
    with TestClient(app) as client:
        page = client.get("/v1/approval-tasks?limit=1")
        assert page.status_code == 200
        assert [item["id"] for item in page.json()] == [str(first.id)]
        cursor = page.headers["X-Next-Cursor"]
        page = client.get("/v1/approval-tasks", params={"limit": 1, "cursor": cursor})
        assert [item["id"] for item in page.json()] == [str(second.id)]
        assert "X-Next-Cursor" not in page.headers
        assert client.get("/v1/approval-tasks?limit=201").status_code == 422
        assert client.get("/v1/approval-tasks?cursor=not-base64").status_code == 422
    assert workflow.list_tasks.call_count == 2
    assert workflow.list_tasks.call_args.kwargs["limit"] == 2
    assert workflow.list_tasks.call_args.kwargs["after"].id == first.id
