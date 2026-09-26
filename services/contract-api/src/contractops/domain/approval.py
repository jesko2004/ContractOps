from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from contractops.context import Role
from contractops.domain.contract import Contract


class PolicyStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class ApprovalInstanceStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ApprovalStepStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    SKIPPED = "SKIPPED"


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


@dataclass(frozen=True, slots=True)
class PolicyCondition:
    contract_types: tuple[str, ...] = ()
    department_ids: tuple[UUID, ...] = ()
    minimum_amount: Decimal | None = None
    maximum_amount: Decimal | None = None
    currency: str | None = None

    def matches(self, contract: Contract) -> bool:
        if self.contract_types and contract.contract_type not in self.contract_types:
            return False
        if self.department_ids and contract.department_id not in self.department_ids:
            return False
        if self.currency is not None and contract.currency != self.currency:
            return False
        if self.minimum_amount is not None and (
            contract.amount is None or contract.amount < self.minimum_amount
        ):
            return False
        return not (
            self.maximum_amount is not None
            and (contract.amount is None or contract.amount > self.maximum_amount)
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "contract_types": list(self.contract_types),
            "department_ids": [str(value) for value in self.department_ids],
            "minimum_amount": (
                str(self.minimum_amount) if self.minimum_amount is not None else None
            ),
            "maximum_amount": (
                str(self.maximum_amount) if self.maximum_amount is not None else None
            ),
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class ApprovalStepTemplate:
    step_order: int
    name: str
    required_role: Role
    minimum_amount: Decimal | None = None

    def applies(self, contract: Contract) -> bool:
        return self.minimum_amount is None or (
            contract.amount is not None and contract.amount >= self.minimum_amount
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "step_order": self.step_order,
            "name": self.name,
            "required_role": self.required_role.value,
            "minimum_amount": (
                str(self.minimum_amount) if self.minimum_amount is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    id: UUID
    tenant_id: UUID
    policy_key: str
    version_number: int
    name: str
    priority: int
    status: PolicyStatus
    condition: PolicyCondition
    steps: tuple[ApprovalStepTemplate, ...]
    created_by: UUID
    created_at: datetime
    published_by: UUID | None = None
    published_at: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "policy_id": str(self.id),
            "policy_key": self.policy_key,
            "version_number": self.version_number,
            "name": self.name,
            "priority": self.priority,
            "condition": self.condition.snapshot(),
            "steps": [step.snapshot() for step in self.steps],
        }


@dataclass(frozen=True, slots=True)
class ApprovalStep:
    id: UUID
    instance_id: UUID
    step_order: int
    name: str
    required_role: Role
    status: ApprovalStepStatus
    assigned_to: UUID | None
    state_version: int
    decided_by: UUID | None
    decision_comment: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalInstance:
    id: UUID
    tenant_id: UUID
    contract_id: UUID
    contract_version_id: UUID
    policy_id: UUID
    policy_snapshot: dict[str, Any]
    status: ApprovalInstanceStatus
    state_version: int
    requested_by: UUID
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    steps: tuple[ApprovalStep, ...]


def initial_step_statuses(
    contract: Contract,
    steps: tuple[ApprovalStepTemplate, ...],
) -> tuple[ApprovalStepStatus, ...]:
    """Skip unmet conditions and make only the first applicable step actionable."""
    statuses: list[ApprovalStepStatus] = []
    activated = False
    for step in steps:
        if not step.applies(contract):
            statuses.append(ApprovalStepStatus.SKIPPED)
        elif not activated:
            statuses.append(ApprovalStepStatus.READY)
            activated = True
        else:
            statuses.append(ApprovalStepStatus.PENDING)
    return tuple(statuses)
