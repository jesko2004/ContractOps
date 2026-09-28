from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from uuid import uuid4

from locust import HttpUser, between, task

TENANT_ID = "00000000-0000-0000-0000-00000000f001"
USER_ID = "00000000-0000-0000-0000-00000000f002"
DEPARTMENT_ID = "00000000-0000-0000-0000-00000000f003"


def _segment(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


def _token() -> str:
    header = _segment({"alg": "HS256", "typ": "JWT"})
    payload = _segment(
        {
            "iss": "contractops",
            "aud": "contractops-api",
            "exp": int(time.time()) + 3_600,
            "tenant_id": TENANT_ID,
            "sub": USER_ID,
            "roles": [
                "CONTRACT_OWNER",
                "APPROVER",
                "LEGAL_ADMIN",
                "FINANCE_APPROVER",
                "BUSINESS_APPROVER",
                "AUDITOR",
                "TENANT_ADMIN",
            ],
            "department_ids": [DEPARTMENT_ID],
            "data_scope": "TENANT",
        }
    )
    signed = f"{header}.{payload}"
    signature = hmac.new(
        os.environ["CONTRACTOPS_JWT_SECRET"].encode(),
        signed.encode(),
        hashlib.sha256,
    ).digest()
    return f"{signed}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


class ContractOpsUser(HttpUser):
    wait_time = between(0.05, 0.2)

    def on_start(self) -> None:
        self.headers = {"Authorization": f"Bearer {_token()}"}
        self.contract_ids: list[str] = []
        self._create_contract()

    def _create_contract(self) -> None:
        unique = uuid4().hex
        headers = {**self.headers, "Idempotency-Key": f"perf-{unique}"}
        with self.client.post(
            "/v1/contracts",
            name="POST /v1/contracts",
            headers=headers,
            json={
                "contract_number": f"PERF-{unique}",
                "title": "Performance test office services agreement",
                "contract_type": "SERVICE",
                "counterparty_name": "Performance Supplier",
                "department_id": DEPARTMENT_ID,
                "amount": "125000.00",
                "currency": "CNY",
            },
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure(f"contract create returned {response.status_code}")
                return
            self.contract_ids.append(str(response.json()["id"]))

    @task(2)
    def create_contract(self) -> None:
        self._create_contract()

    @task(8)
    def read_contract(self) -> None:
        if not self.contract_ids:
            self._create_contract()
            return
        self.client.get(
            f"/v1/contracts/{self.contract_ids[-1]}",
            name="GET /v1/contracts/:id",
            headers=self.headers,
        )

    @task(5)
    def list_approval_tasks(self) -> None:
        self.client.get("/v1/approval-tasks", headers=self.headers)

    @task(3)
    def query_audit_events(self) -> None:
        self.client.get("/v1/audit-events?limit=20", headers=self.headers)
