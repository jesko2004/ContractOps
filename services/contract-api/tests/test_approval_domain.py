from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from contractops.context import Role
from contractops.domain.approval import (
    ApprovalStepStatus,
    ApprovalStepTemplate,
    PolicyCondition,
    initial_step_statuses,
)
from contractops.domain.contract import Contract, ContractStatus


def _contract(amount: Decimal, *, contract_type: str = "SERVICE") -> Contract:
    now = datetime.now(UTC)
    return Contract(
        id=uuid4(),
        tenant_id=uuid4(),
        contract_number="CT-001",
        title="Office services agreement",
        contract_type=contract_type,
        counterparty_name="Example Supplier",
        department_id=uuid4(),
        amount=amount,
        currency="CNY",
        valid_from=None,
        valid_until=None,
        status=ContractStatus.DRAFT,
        current_version_id=uuid4(),
        state_version=0,
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def test_high_amount_contract_adds_finance_step() -> None:
    contract = _contract(Decimal("250000.00"))
    steps = (
        ApprovalStepTemplate(1, "Legal review", Role.LEGAL_ADMIN),
        ApprovalStepTemplate(
            2,
            "Finance review",
            Role.FINANCE_APPROVER,
            minimum_amount=Decimal("100000.00"),
        ),
    )

    assert initial_step_statuses(contract, steps) == (
        ApprovalStepStatus.READY,
        ApprovalStepStatus.PENDING,
    )


def test_low_amount_contract_skips_finance_step() -> None:
    contract = _contract(Decimal("50000.00"))
    steps = (
        ApprovalStepTemplate(1, "Legal review", Role.LEGAL_ADMIN),
        ApprovalStepTemplate(
            2,
            "Finance review",
            Role.FINANCE_APPROVER,
            minimum_amount=Decimal("100000.00"),
        ),
    )

    assert initial_step_statuses(contract, steps) == (
        ApprovalStepStatus.READY,
        ApprovalStepStatus.SKIPPED,
    )


def test_policy_condition_uses_controlled_contract_fields() -> None:
    contract = _contract(Decimal("125000.00"), contract_type="PROCUREMENT")
    assert PolicyCondition(
        contract_types=("PROCUREMENT",),
        minimum_amount=Decimal("100000.00"),
        maximum_amount=Decimal("200000.00"),
        currency="CNY",
    ).matches(contract)
    assert not PolicyCondition(contract_types=("SERVICE",)).matches(contract)
