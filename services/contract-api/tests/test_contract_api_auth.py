from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient

from contractops.api.dependencies import authenticated_actor
from contractops.application.contracts import (
    AddContractVersionCommand,
    CreateContractCommand,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.contract import Contract, ContractStatus, ContractVersion
from contractops.main import create_app


class UnusedLedger:
    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Contract, bool]:
        raise AssertionError("unauthenticated requests must not reach the ledger")

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None:
        raise AssertionError("unauthenticated requests must not reach the ledger")

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]:
        raise AssertionError("unauthenticated requests must not reach the ledger")


class StaticLedger:
    def __init__(self, contract: Contract) -> None:
        self.contract = contract
        self.received_idempotency_key: str | None = None

    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Contract, bool]:
        self.received_idempotency_key = idempotency_key
        return self.contract, False

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None:
        return self.contract if contract_id == self.contract.id else None

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]:
        raise AssertionError("this test does not add a version")


def test_contract_endpoint_requires_bearer_token_with_stable_error() -> None:
    with TestClient(create_app(contract_ledger=UnusedLedger())) as client:
        response = client.get("/v1/contracts/00000000-0000-0000-0000-000000000001")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "authentication_required",
            "message": "a bearer access token is required",
            "request_id": response.headers["X-Request-ID"],
        }
    }


def test_contract_endpoint_rejects_invalid_token_with_stable_error() -> None:
    with TestClient(create_app(contract_ledger=UnusedLedger())) as client:
        response = client.get(
            "/v1/contracts/00000000-0000-0000-0000-000000000001",
            headers={"Authorization": "Bearer invalid-token"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_invalid"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


def test_authorized_actor_can_create_and_read_contract() -> None:
    tenant_id = UUID("00000000-0000-0000-0000-000000000010")
    user_id = UUID("00000000-0000-0000-0000-000000000011")
    department_id = UUID("00000000-0000-0000-0000-000000000012")
    contract_id = UUID("00000000-0000-0000-0000-000000000013")
    now = datetime(2026, 9, 26, tzinfo=UTC)
    actor = ActorContext(
        tenant_id=tenant_id,
        user_id=user_id,
        roles=frozenset({Role.CONTRACT_OWNER}),
        department_ids=frozenset({department_id}),
        data_scope=DataScope.DEPARTMENT,
    )
    ledger = StaticLedger(
        Contract(
            id=contract_id,
            tenant_id=tenant_id,
            contract_number="PO-2026-001",
            title="Office equipment purchase",
            contract_type="PURCHASE",
            counterparty_name="Example Supplier",
            department_id=department_id,
            amount=Decimal("120000.00"),
            currency="CNY",
            valid_from=None,
            valid_until=None,
            status=ContractStatus.DRAFT,
            current_version_id=None,
            state_version=0,
            created_by=user_id,
            created_at=now,
            updated_at=now,
        )
    )
    application = create_app(contract_ledger=ledger)
    application.dependency_overrides[authenticated_actor] = lambda: actor

    with TestClient(application) as client:
        created = client.post(
            "/v1/contracts",
            headers={"Idempotency-Key": "create-po-2026-001"},
            json={
                "contract_number": "PO-2026-001",
                "title": "Office equipment purchase",
                "contract_type": "PURCHASE",
                "counterparty_name": "Example Supplier",
                "department_id": str(department_id),
                "amount": "120000.00",
                "currency": "CNY",
            },
        )
        fetched = client.get(f"/v1/contracts/{contract_id}")

    assert created.status_code == 201
    assert created.headers["X-Idempotent-Replay"] == "false"
    assert created.json()["id"] == str(contract_id)
    assert created.json()["currency"] == "CNY"
    assert ledger.received_idempotency_key == "create-po-2026-001"
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Office equipment purchase"
