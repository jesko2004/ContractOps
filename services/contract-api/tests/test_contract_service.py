from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from contractops.application.contracts import (
    AddContractVersionCommand,
    ContractService,
    CreateContractCommand,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.contract import Contract, ContractStatus, ContractVersion
from contractops.errors import ContractOpsError


class RecordingLedger:
    def __init__(self, contract: Contract) -> None:
        self.contract = contract
        self.request_hashes: list[str] = []

    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Contract, bool]:
        self.request_hashes.append(request_hash)
        return self.contract, False

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None:
        return self.contract if self.contract.id == contract_id else None

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]:
        raise AssertionError("not used by these tests")


def _actor(
    *,
    user_id: UUID,
    department_id: UUID,
    scope: DataScope = DataScope.DEPARTMENT,
    roles: frozenset[Role] = frozenset({Role.CONTRACT_OWNER}),
) -> ActorContext:
    return ActorContext(
        tenant_id=uuid4(),
        user_id=user_id,
        roles=roles,
        department_ids=frozenset({department_id}),
        data_scope=scope,
    )


def _contract(owner_id: UUID, department_id: UUID) -> Contract:
    timestamp = datetime.now(UTC)
    return Contract(
        id=uuid4(),
        tenant_id=uuid4(),
        contract_number="CT-001",
        title="Office lease",
        contract_type="LEASE",
        counterparty_name="Example Ltd",
        department_id=department_id,
        amount=Decimal("1000.00"),
        currency="CNY",
        valid_from=date(2026, 1, 1),
        valid_until=date(2026, 12, 31),
        status=ContractStatus.DRAFT,
        current_version_id=None,
        state_version=0,
        created_by=owner_id,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _command(department_id: UUID) -> CreateContractCommand:
    return CreateContractCommand(
        contract_number="CT-001",
        title="Office lease",
        contract_type="LEASE",
        counterparty_name="Example Ltd",
        department_id=department_id,
        amount=Decimal("1000.00"),
        currency="CNY",
        valid_from=date(2026, 1, 1),
        valid_until=date(2026, 12, 31),
    )


def test_create_requires_writer_role_and_department_scope() -> None:
    owner_id = uuid4()
    allowed_department = uuid4()
    other_department = uuid4()
    contract = _contract(owner_id, allowed_department)
    ledger = RecordingLedger(contract)
    service = ContractService(ledger)

    with pytest.raises(ContractOpsError) as role_error:
        service.create_contract(
            _actor(
                user_id=owner_id,
                department_id=allowed_department,
                roles=frozenset({Role.AUDITOR}),
            ),
            _command(allowed_department),
            idempotency_key="create-001",
        )
    assert role_error.value.code == "authorization_denied"

    with pytest.raises(ContractOpsError) as department_error:
        service.create_contract(
            _actor(user_id=owner_id, department_id=allowed_department),
            _command(other_department),
            idempotency_key="create-002",
        )
    assert department_error.value.code == "authorization_denied"


def test_create_validates_amount_currency_and_date_range() -> None:
    owner_id = uuid4()
    department_id = uuid4()
    service = ContractService(RecordingLedger(_contract(owner_id, department_id)))
    actor = _actor(user_id=owner_id, department_id=department_id)
    missing_currency = CreateContractCommand(
        contract_number="CT-001",
        title="Office lease",
        contract_type="LEASE",
        counterparty_name="Example Ltd",
        department_id=department_id,
        amount=Decimal("1000.00"),
        currency=None,
        valid_from=date(2026, 1, 1),
        valid_until=date(2026, 12, 31),
    )

    with pytest.raises(ContractOpsError) as currency_error:
        service.create_contract(actor, missing_currency, idempotency_key="create-003")
    assert currency_error.value.code == "contract_amount_currency_required"

    invalid_dates = _command(department_id)
    invalid_dates = CreateContractCommand(
        contract_number=invalid_dates.contract_number,
        title=invalid_dates.title,
        contract_type=invalid_dates.contract_type,
        counterparty_name=invalid_dates.counterparty_name,
        department_id=invalid_dates.department_id,
        amount=invalid_dates.amount,
        currency=invalid_dates.currency,
        valid_from=date(2026, 12, 31),
        valid_until=date(2026, 1, 1),
    )
    with pytest.raises(ContractOpsError) as date_error:
        service.create_contract(actor, invalid_dates, idempotency_key="create-004")
    assert date_error.value.code == "contract_validity_invalid"


def test_department_scope_cannot_read_another_department() -> None:
    owner_id = uuid4()
    contract_department = uuid4()
    actor_department = uuid4()
    contract = _contract(owner_id, contract_department)
    service = ContractService(RecordingLedger(contract))
    actor = _actor(user_id=uuid4(), department_id=actor_department)

    with pytest.raises(ContractOpsError) as error:
        service.get_contract(actor, contract.id)

    assert error.value.code == "authorization_denied"


def test_create_produces_stable_request_hash() -> None:
    owner_id = uuid4()
    department_id = uuid4()
    ledger = RecordingLedger(_contract(owner_id, department_id))
    service = ContractService(ledger)
    actor = _actor(user_id=owner_id, department_id=department_id)
    command = _command(department_id)

    service.create_contract(actor, command, idempotency_key="create-005")
    service.create_contract(actor, command, idempotency_key="create-006")

    assert len(ledger.request_hashes) == 2
    assert ledger.request_hashes[0] == ledger.request_hashes[1]
