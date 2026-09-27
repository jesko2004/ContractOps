from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from contractops.application.audits import AuditService
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.audit import AuditCategory, AuditEvent, RuntimeFailure
from contractops.errors import ContractOpsError
from contractops.main import create_app
from contractops.observability import JsonFormatter


class EmptyAuditRepository:
    def list_events(
        self,
        actor: ActorContext,
        *,
        request_id: str | None,
        trace_id: str | None,
        category: AuditCategory | None,
        limit: int,
    ) -> Sequence[AuditEvent]:
        return []

    def list_failures(
        self, actor: ActorContext, *, limit: int
    ) -> Sequence[RuntimeFailure]:
        return []


def _actor(role: Role) -> ActorContext:
    return ActorContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        roles=frozenset({role}),
        department_ids=frozenset(),
        data_scope=DataScope.TENANT,
    )


def test_audit_service_requires_auditor_and_validates_trace_id() -> None:
    service = AuditService(EmptyAuditRepository())

    with pytest.raises(ContractOpsError) as forbidden:
        service.list_events(_actor(Role.CONTRACT_OWNER))
    assert forbidden.value.code == "audit_forbidden"

    with pytest.raises(ContractOpsError) as invalid_filter:
        service.list_events(_actor(Role.AUDITOR), trace_id="not-a-trace")
    assert invalid_filter.value.code == "audit_filter_invalid"

    assert service.list_events(_actor(Role.AUDITOR), trace_id="a" * 32) == []


def test_json_formatter_uses_an_allowlist_and_drops_sensitive_extras() -> None:
    record = logging.LogRecord(
        name="contractops.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "req_safe"
    record.authorization = "Bearer should-never-appear"
    record.contract_text = "confidential contract body"

    payload = json.loads(JsonFormatter().format(record))

    assert payload["request_id"] == "req_safe"
    assert "authorization" not in payload
    assert "contract_text" not in payload
    assert "should-never-appear" not in json.dumps(payload)
    assert "confidential contract body" not in json.dumps(payload)


def test_metrics_endpoint_exposes_prometheus_data_and_correlation_headers() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "contractops_http_requests_total" in response.text
    assert response.headers["X-Request-ID"].startswith("req_")
    assert len(response.headers["X-Trace-ID"]) == 32
