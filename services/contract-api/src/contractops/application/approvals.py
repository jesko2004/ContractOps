from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from contractops.application.contracts import ContractService
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.approval import (
    ApprovalDecision,
    ApprovalInstance,
    ApprovalPolicy,
    ApprovalStep,
    ApprovalStepTemplate,
    PolicyCondition,
)
from contractops.errors import ContractOpsError


@dataclass(frozen=True, slots=True)
class CreateApprovalPolicyCommand:
    policy_key: str
    name: str
    priority: int
    condition: PolicyCondition
    steps: tuple[ApprovalStepTemplate, ...]


@dataclass(frozen=True, slots=True)
class StepActionCommand:
    expected_version: int
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class TransferStepCommand:
    expected_version: int
    target_user_id: UUID
    comment: str | None = None


class ApprovalWorkflow(Protocol):
    def create_policy(
        self,
        actor: ActorContext,
        command: CreateApprovalPolicyCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalPolicy, bool]: ...

    def publish_policy(self, actor: ActorContext, policy_id: UUID) -> ApprovalPolicy: ...

    def submit_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalInstance, bool]: ...

    def get_instance(
        self, actor: ActorContext, instance_id: UUID
    ) -> ApprovalInstance | None: ...

    def list_tasks(self, actor: ActorContext) -> tuple[ApprovalStep, ...]: ...

    def claim_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: StepActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]: ...

    def decide_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        decision: ApprovalDecision,
        command: StepActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]: ...

    def transfer_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: TransferStepCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]: ...


def _request_hash(
    value: CreateApprovalPolicyCommand
    | StepActionCommand
    | TransferStepCommand
    | dict[str, object],
) -> str:
    serialized = json.dumps(
        value if isinstance(value, dict) else asdict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _forbidden(message: str) -> ContractOpsError:
    return ContractOpsError(code="authorization_denied", message=message, status_code=403)


class ApprovalService:
    _POLICY_ROLES = (Role.LEGAL_ADMIN, Role.TENANT_ADMIN)
    _APPROVER_ROLES = (
        Role.APPROVER,
        Role.LEGAL_ADMIN,
        Role.FINANCE_APPROVER,
        Role.BUSINESS_APPROVER,
        Role.TENANT_ADMIN,
    )
    _STEP_ROLES = frozenset(
        {Role.LEGAL_ADMIN, Role.FINANCE_APPROVER, Role.BUSINESS_APPROVER}
    )

    def __init__(self, workflow: ApprovalWorkflow, contracts: ContractService) -> None:
        self._workflow = workflow
        self._contracts = contracts

    def create_policy(
        self,
        actor: ActorContext,
        command: CreateApprovalPolicyCommand,
        *,
        idempotency_key: str,
    ) -> tuple[ApprovalPolicy, bool]:
        if not actor.has_any_role(*self._POLICY_ROLES):
            raise _forbidden("the caller cannot manage approval policies")
        self._validate_policy(command)
        return self._workflow.create_policy(
            actor,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def publish_policy(self, actor: ActorContext, policy_id: UUID) -> ApprovalPolicy:
        if not actor.has_any_role(*self._POLICY_ROLES):
            raise _forbidden("the caller cannot publish approval policies")
        return self._workflow.publish_policy(actor, policy_id)

    def submit_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        idempotency_key: str,
    ) -> tuple[ApprovalInstance, bool]:
        if not actor.has_any_role(Role.CONTRACT_OWNER, Role.TENANT_ADMIN):
            raise _forbidden("the caller cannot submit contracts")
        contract = self._contracts.get_contract(actor, contract_id)
        if actor.data_scope is DataScope.OWN and contract.created_by != actor.user_id:
            raise _forbidden("the caller cannot submit this contract")
        if contract.current_version_id is None:
            raise ContractOpsError(
                code="contract_version_required",
                message="a contract version is required before submission",
            )
        return self._workflow.submit_contract(
            actor,
            contract_id,
            idempotency_key=idempotency_key,
            request_hash=_request_hash({"contract_id": str(contract_id)}),
        )

    def get_instance(self, actor: ActorContext, instance_id: UUID) -> ApprovalInstance:
        if not actor.has_any_role(
            Role.CONTRACT_OWNER,
            *self._APPROVER_ROLES,
            Role.AUDITOR,
        ):
            raise _forbidden("the caller cannot read approval instances")
        value = self._workflow.get_instance(actor, instance_id)
        if value is None:
            raise ContractOpsError(
                code="approval_instance_not_found",
                message="approval instance was not found",
                status_code=404,
            )
        self._contracts.get_contract(actor, value.contract_id)
        return value

    def list_tasks(self, actor: ActorContext) -> tuple[ApprovalStep, ...]:
        if not actor.has_any_role(*self._APPROVER_ROLES):
            raise _forbidden("the caller cannot list approval tasks")
        return self._workflow.list_tasks(actor)

    def claim_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: StepActionCommand,
        *,
        idempotency_key: str,
    ) -> tuple[ApprovalStep, bool]:
        self._require_approver(actor)
        return self._workflow.claim_step(
            actor,
            step_id,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def decide_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        decision: ApprovalDecision,
        command: StepActionCommand,
        *,
        idempotency_key: str,
    ) -> tuple[ApprovalStep, bool]:
        self._require_approver(actor)
        return self._workflow.decide_step(
            actor,
            step_id,
            decision,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(
                {"decision": decision.value, "command": asdict(command)}
            ),
        )

    def transfer_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: TransferStepCommand,
        *,
        idempotency_key: str,
    ) -> tuple[ApprovalStep, bool]:
        self._require_approver(actor)
        if command.target_user_id == actor.user_id:
            raise ContractOpsError(
                code="approval_transfer_target_invalid",
                message="the target user must be different from the caller",
            )
        return self._workflow.transfer_step(
            actor,
            step_id,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def _require_approver(self, actor: ActorContext) -> None:
        if not actor.has_any_role(*self._APPROVER_ROLES):
            raise _forbidden("the caller cannot act on approval tasks")

    @classmethod
    def _validate_policy(cls, command: CreateApprovalPolicyCommand) -> None:
        if not command.steps:
            raise ContractOpsError(
                code="approval_policy_steps_required",
                message="at least one approval step is required",
            )
        orders = tuple(step.step_order for step in command.steps)
        if orders != tuple(range(1, len(command.steps) + 1)):
            raise ContractOpsError(
                code="approval_policy_step_order_invalid",
                message="approval step_order values must be consecutive starting at one",
            )
        if any(step.required_role not in cls._STEP_ROLES for step in command.steps):
            raise ContractOpsError(
                code="approval_policy_role_invalid",
                message="approval steps must use a specialized approval role",
            )
        if all(step.minimum_amount is not None for step in command.steps):
            raise ContractOpsError(
                code="approval_policy_unconditional_step_required",
                message="at least one approval step must be unconditional",
            )
        condition = command.condition
        if (
            condition.minimum_amount is not None
            and condition.maximum_amount is not None
            and condition.minimum_amount > condition.maximum_amount
        ):
            raise ContractOpsError(
                code="approval_policy_amount_range_invalid",
                message="minimum_amount cannot exceed maximum_amount",
            )
        amounts: tuple[Decimal | None, ...] = tuple(
            step.minimum_amount for step in command.steps
        )
        if any(value is not None and value < 0 for value in amounts):
            raise ContractOpsError(
                code="approval_policy_step_amount_invalid",
                message="step minimum_amount cannot be negative",
            )
