from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from contractops.application.contracts import (
    AddContractVersionCommand,
    ContractService,
    CreateContractCommand,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres import Database, PostgresContractLedger

RUNTIME_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_DATABASE_URL")
ADMIN_DATABASE_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not RUNTIME_DATABASE_URL or not ADMIN_DATABASE_URL,
    reason="PostgreSQL integration URLs are not configured",
)


def _tenant() -> UUID:
    tenant_id = uuid4()
    assert ADMIN_DATABASE_URL is not None
    with psycopg.connect(ADMIN_DATABASE_URL) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, %s)",
            (tenant_id, f"Tenant {tenant_id}"),
        )
    return tenant_id


def _actor(tenant_id: UUID, department_id: UUID) -> ActorContext:
    return ActorContext(
        tenant_id=tenant_id,
        user_id=uuid4(),
        roles=frozenset({Role.CONTRACT_OWNER}),
        department_ids=frozenset({department_id}),
        data_scope=DataScope.DEPARTMENT,
    )


def _command(department_id: UUID) -> CreateContractCommand:
    return CreateContractCommand(
        contract_number=f"CT-{uuid4().hex[:12]}",
        title="Office services agreement",
        contract_type="SERVICE",
        counterparty_name="Example Supplier",
        department_id=department_id,
        amount=Decimal("250000.00"),
        currency="CNY",
        valid_from=date(2026, 1, 1),
        valid_until=date(2026, 12, 31),
    )


@pytest.fixture()
def database() -> Database:
    assert RUNTIME_DATABASE_URL is not None
    value = Database(RUNTIME_DATABASE_URL)
    try:
        yield value
    finally:
        value.dispose()


def test_rls_hides_contract_from_another_tenant(database: Database) -> None:
    first_department = uuid4()
    first_actor = _actor(_tenant(), first_department)
    second_actor = _actor(_tenant(), uuid4())
    ledger = PostgresContractLedger(database)
    service = ContractService(ledger)
    contract, replayed = service.create_contract(
        first_actor,
        _command(first_department),
        idempotency_key=f"create-{uuid4()}",
    )

    assert replayed is False
    assert ledger.get_contract(second_actor, contract.id) is None
    with pytest.raises(ContractOpsError) as error:
        service.get_contract(second_actor, contract.id)
    assert error.value.code == "contract_not_found"


def test_idempotent_version_creation_and_tenant_object_key(database: Database) -> None:
    department_id = uuid4()
    actor = _actor(_tenant(), department_id)
    ledger = PostgresContractLedger(database)
    service = ContractService(ledger)
    contract_key = f"create-{uuid4()}"
    contract_command = _command(department_id)
    contract, first_contract_replay = service.create_contract(
        actor,
        contract_command,
        idempotency_key=contract_key,
    )
    repeated_contract, second_contract_replay = service.create_contract(
        actor,
        contract_command,
        idempotency_key=contract_key,
    )

    assert first_contract_replay is False
    assert second_contract_replay is True
    assert repeated_contract.id == contract.id

    version_command = AddContractVersionCommand(
        file_name="../office agreement.pdf",
        media_type="application/pdf",
        size_bytes=1024,
        content_hash="a" * 64,
    )
    version_key = f"version-{uuid4()}"
    version, first_version_replay = service.add_version(
        actor,
        contract.id,
        version_command,
        idempotency_key=version_key,
    )
    repeated_version, second_version_replay = service.add_version(
        actor,
        contract.id,
        version_command,
        idempotency_key=version_key,
    )

    assert first_version_replay is False
    assert second_version_replay is True
    assert repeated_version.id == version.id
    assert version.object_key.startswith(f"tenants/{actor.tenant_id}/contracts/{contract.id}/")
    assert ".." not in version.object_key
    reloaded = service.get_contract(actor, contract.id)
    assert len(reloaded.versions) == 1
    assert reloaded.current_version_id == version.id


def test_database_rejects_contract_version_core_mutation(database: Database) -> None:
    department_id = uuid4()
    actor = _actor(_tenant(), department_id)
    service = ContractService(PostgresContractLedger(database))
    contract, _ = service.create_contract(
        actor,
        _command(department_id),
        idempotency_key=f"create-{uuid4()}",
    )
    version, _ = service.add_version(
        actor,
        contract.id,
        AddContractVersionCommand(
            file_name="immutable.pdf",
            media_type="application/pdf",
            size_bytes=10,
            content_hash="b" * 64,
        ),
        idempotency_key=f"version-{uuid4()}",
    )

    with (
        pytest.raises(DBAPIError, match="immutable"),
        database.transaction(actor) as connection,
    ):
        connection.execute(
            text("UPDATE contract_versions SET file_name = :name WHERE id = :id"),
            {"name": "mutated.pdf", "id": version.id},
        )
