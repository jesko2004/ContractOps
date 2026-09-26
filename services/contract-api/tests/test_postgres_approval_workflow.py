from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from contractops.application.approvals import (
    ApprovalService,
    CreateApprovalPolicyCommand,
    StepActionCommand,
    TransferStepCommand,
)
from contractops.application.contracts import (
    AddContractVersionCommand,
    ContractService,
    CreateContractCommand,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.approval import (
    ApprovalDecision,
    ApprovalInstanceStatus,
    ApprovalStepStatus,
    ApprovalStepTemplate,
    PolicyCondition,
    PolicyStatus,
)
from contractops.domain.contract import ContractStatus
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres import (
    Database,
    PostgresApprovalWorkflow,
    PostgresContractLedger,
)

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


def _actor(tenant_id: UUID, *roles: Role, department_id: UUID | None = None) -> ActorContext:
    return ActorContext(
        tenant_id=tenant_id,
        user_id=uuid4(),
        roles=frozenset(roles),
        department_ids=(
            frozenset({department_id}) if department_id is not None else frozenset()
        ),
        data_scope=DataScope.TENANT,
    )


@pytest.fixture()
def database() -> Database:
    assert RUNTIME_DATABASE_URL is not None
    value = Database(RUNTIME_DATABASE_URL)
    try:
        yield value
    finally:
        value.dispose()


def _services(database: Database) -> tuple[ContractService, ApprovalService]:
    contracts = ContractService(PostgresContractLedger(database))
    approvals = ApprovalService(PostgresApprovalWorkflow(database), contracts)
    return contracts, approvals


def _create_policy(
    approvals: ApprovalService,
    admin: ActorContext,
    *,
    name: str = "Standard contract approval",
) -> tuple[UUID, int]:
    policy, replayed = approvals.create_policy(
        admin,
        _policy_command(name),
        idempotency_key=f"policy-{uuid4()}",
    )
    assert replayed is False
    published = approvals.publish_policy(admin, policy.id)
    assert published.status is PolicyStatus.PUBLISHED
    return published.id, published.version_number


def _policy_command(name: str) -> CreateApprovalPolicyCommand:
    return CreateApprovalPolicyCommand(
        policy_key="standard-contract",
        name=name,
        priority=100,
        condition=PolicyCondition(currency="CNY"),
        steps=(
            ApprovalStepTemplate(1, "Legal review", Role.LEGAL_ADMIN),
            ApprovalStepTemplate(
                2,
                "Finance review",
                Role.FINANCE_APPROVER,
                minimum_amount=Decimal("100000.00"),
            ),
        ),
    )


def _create_contract(
    contracts: ContractService,
    owner: ActorContext,
    department_id: UUID,
    amount: Decimal,
):
    contract, _ = contracts.create_contract(
        owner,
        CreateContractCommand(
            contract_number=f"CT-{uuid4().hex[:12]}",
            title="Office services agreement",
            contract_type="SERVICE",
            counterparty_name="Example Supplier",
            department_id=department_id,
            amount=amount,
            currency="CNY",
            valid_from=None,
            valid_until=None,
        ),
        idempotency_key=f"contract-{uuid4()}",
    )
    contracts.add_version(
        owner,
        contract.id,
        AddContractVersionCommand(
            file_name="agreement.pdf",
            media_type="application/pdf",
            size_bytes=1024,
            content_hash=uuid4().hex + uuid4().hex,
        ),
        idempotency_key=f"version-{uuid4()}",
    )
    return contract


def test_high_amount_workflow_and_stale_decision_conflict(database: Database) -> None:
    tenant_id = _tenant()
    department_id = uuid4()
    admin = _actor(tenant_id, Role.TENANT_ADMIN)
    owner = _actor(tenant_id, Role.CONTRACT_OWNER, department_id=department_id)
    legal = _actor(tenant_id, Role.LEGAL_ADMIN)
    finance = _actor(tenant_id, Role.FINANCE_APPROVER)
    contracts, approvals = _services(database)
    _create_policy(approvals, admin)
    contract = _create_contract(contracts, owner, department_id, Decimal("250000.00"))

    instance, replayed = approvals.submit_contract(
        owner, contract.id, idempotency_key=f"submit-{uuid4()}"
    )

    assert replayed is False
    assert [step.status for step in instance.steps] == [
        ApprovalStepStatus.READY,
        ApprovalStepStatus.PENDING,
    ]
    legal_step = instance.steps[0]
    claimed, _ = approvals.claim_step(
        legal,
        legal_step.id,
        StepActionCommand(expected_version=0),
        idempotency_key=f"claim-{uuid4()}",
    )
    approvals.decide_step(
        legal,
        legal_step.id,
        ApprovalDecision.APPROVE,
        StepActionCommand(expected_version=claimed.state_version, comment="Legal approved"),
        idempotency_key=f"decision-{uuid4()}",
    )
    with pytest.raises(ContractOpsError) as stale_error:
        approvals.decide_step(
            legal,
            legal_step.id,
            ApprovalDecision.REJECT,
            StepActionCommand(expected_version=claimed.state_version),
            idempotency_key=f"decision-{uuid4()}",
        )
    assert stale_error.value.code == "approval_step_conflict"

    current = approvals.get_instance(owner, instance.id)
    finance_step = current.steps[1]
    assert finance_step.status is ApprovalStepStatus.READY
    finance_claimed, _ = approvals.claim_step(
        finance,
        finance_step.id,
        StepActionCommand(expected_version=finance_step.state_version),
        idempotency_key=f"claim-{uuid4()}",
    )
    approvals.decide_step(
        finance,
        finance_step.id,
        ApprovalDecision.APPROVE,
        StepActionCommand(expected_version=finance_claimed.state_version),
        idempotency_key=f"decision-{uuid4()}",
    )

    completed = approvals.get_instance(owner, instance.id)
    assert completed.status is ApprovalInstanceStatus.APPROVED
    assert contracts.get_contract(owner, contract.id).status is ContractStatus.APPROVED


def test_published_policy_is_immutable_and_snapshot_survives_new_version(
    database: Database,
) -> None:
    tenant_id = _tenant()
    department_id = uuid4()
    admin = _actor(tenant_id, Role.TENANT_ADMIN)
    owner = _actor(tenant_id, Role.CONTRACT_OWNER, department_id=department_id)
    contracts, approvals = _services(database)
    first_policy_id, first_version = _create_policy(approvals, admin, name="Policy v1")
    first_contract = _create_contract(
        contracts, owner, department_id, Decimal("50000.00")
    )
    first_instance, _ = approvals.submit_contract(
        owner, first_contract.id, idempotency_key=f"submit-{uuid4()}"
    )
    assert first_instance.policy_snapshot["version_number"] == first_version
    assert first_instance.steps[1].status is ApprovalStepStatus.SKIPPED

    with (
        pytest.raises(DBAPIError, match="immutable"),
        database.transaction(admin) as connection,
    ):
        connection.execute(
            text("UPDATE approval_policies SET name = 'mutated' WHERE id = :id"),
            {"id": first_policy_id},
        )

    second_policy, _ = approvals.create_policy(
        admin,
        _policy_command("Policy v2"),
        idempotency_key=f"policy-{uuid4()}",
    )
    with pytest.raises(ContractOpsError) as draft_error:
        approvals.create_policy(
            admin,
            _policy_command("Conflicting draft"),
            idempotency_key=f"policy-{uuid4()}",
        )
    assert draft_error.value.code == "approval_policy_draft_exists"
    published_second = approvals.publish_policy(admin, second_policy.id)
    second_policy_id = published_second.id
    second_version = published_second.version_number
    assert second_policy_id != first_policy_id
    assert second_version == first_version + 1
    reloaded_first = approvals.get_instance(owner, first_instance.id)
    assert reloaded_first.policy_id == first_policy_id
    assert reloaded_first.policy_snapshot["name"] == "Policy v1"

    second_contract = _create_contract(
        contracts, owner, department_id, Decimal("50000.00")
    )
    second_instance, _ = approvals.submit_contract(
        owner, second_contract.id, idempotency_key=f"submit-{uuid4()}"
    )
    assert second_instance.policy_id == second_policy_id
    assert second_instance.policy_snapshot["version_number"] == second_version


def test_personal_task_transfer_and_request_changes(database: Database) -> None:
    tenant_id = _tenant()
    department_id = uuid4()
    admin = _actor(tenant_id, Role.TENANT_ADMIN)
    owner = _actor(tenant_id, Role.CONTRACT_OWNER, department_id=department_id)
    first_legal = _actor(tenant_id, Role.LEGAL_ADMIN)
    second_legal = _actor(tenant_id, Role.LEGAL_ADMIN)
    contracts, approvals = _services(database)
    _create_policy(approvals, admin)
    contract = _create_contract(contracts, owner, department_id, Decimal("50000.00"))
    instance, _ = approvals.submit_contract(
        owner, contract.id, idempotency_key=f"submit-{uuid4()}"
    )
    step = instance.steps[0]

    assert [item.id for item in approvals.list_tasks(first_legal)] == [step.id]
    claimed, _ = approvals.claim_step(
        first_legal,
        step.id,
        StepActionCommand(expected_version=step.state_version),
        idempotency_key=f"claim-{uuid4()}",
    )
    transferred, _ = approvals.transfer_step(
        first_legal,
        step.id,
        TransferStepCommand(
            expected_version=claimed.state_version,
            target_user_id=second_legal.user_id,
            comment="Coverage handoff",
        ),
        idempotency_key=f"transfer-{uuid4()}",
    )
    assert transferred.status is ApprovalStepStatus.READY
    assert approvals.list_tasks(first_legal) == ()
    assert [item.id for item in approvals.list_tasks(second_legal)] == [step.id]

    second_claim, _ = approvals.claim_step(
        second_legal,
        step.id,
        StepActionCommand(expected_version=transferred.state_version),
        idempotency_key=f"claim-{uuid4()}",
    )
    approvals.decide_step(
        second_legal,
        step.id,
        ApprovalDecision.REQUEST_CHANGES,
        StepActionCommand(
            expected_version=second_claim.state_version,
            comment="Please revise the liability cap",
        ),
        idempotency_key=f"decision-{uuid4()}",
    )

    updated = approvals.get_instance(owner, instance.id)
    assert updated.status is ApprovalInstanceStatus.CHANGES_REQUESTED
    assert contracts.get_contract(owner, contract.id).status is ContractStatus.CHANGES_REQUESTED
    with pytest.raises(ContractOpsError) as submit_error:
        approvals.submit_contract(
            owner, contract.id, idempotency_key=f"submit-{uuid4()}"
        )
    assert submit_error.value.code == "contract_not_submittable"

    revised, _ = contracts.add_version(
        owner,
        contract.id,
        AddContractVersionCommand(
            file_name="agreement-revised.pdf",
            media_type="application/pdf",
            size_bytes=2048,
            content_hash=uuid4().hex + uuid4().hex,
        ),
        idempotency_key=f"version-{uuid4()}",
    )
    editable = contracts.get_contract(owner, contract.id)
    assert editable.status is ContractStatus.DRAFT
    assert editable.current_version_id == revised.id
