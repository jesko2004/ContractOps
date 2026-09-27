from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class ObligationType(StrEnum):
    PAYMENT = "PAYMENT"
    DELIVERY = "DELIVERY"
    ACCEPTANCE = "ACCEPTANCE"
    RENEWAL = "RENEWAL"
    TERMINATION_NOTICE = "TERMINATION_NOTICE"
    OTHER = "OTHER"


class ObligationStatus(StrEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    OVERDUE = "OVERDUE"
    WAIVED = "WAIVED"
    CANCELLED = "CANCELLED"


class RiskType(StrEnum):
    DUE_SOON = "DUE_SOON"
    OVERDUE = "OVERDUE"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"


class RiskSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    DEFERRED = "DEFERRED"
    RESOLVED = "RESOLVED"


class RiskAction(StrEnum):
    ACKNOWLEDGE = "ACKNOWLEDGE"
    DEFER = "DEFER"
    RESOLVE = "RESOLVE"


@dataclass(frozen=True, slots=True)
class Obligation:
    id: UUID
    tenant_id: UUID
    contract_id: UUID
    obligation_type: ObligationType
    title: str
    owner_id: UUID
    due_at: datetime
    grace_period_seconds: int
    status: ObligationStatus
    state_version: int
    evidence_reference: str | None
    next_action_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    completed_by: UUID | None = None
    completed_at: datetime | None = None
    cancel_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RiskEvent:
    id: UUID
    tenant_id: UUID
    contract_id: UUID
    obligation_id: UUID
    risk_type: RiskType
    severity: RiskSeverity
    status: RiskStatus
    owner_id: UUID
    state_version: int
    occurrence_count: int
    first_detected_at: datetime
    last_detected_at: datetime
    deferred_until: datetime | None
    resolution: str | None
    resolved_by: UUID | None
    resolved_at: datetime | None


class InvalidObligationTransition(ValueError):
    def __init__(self, current: ObligationStatus, target: ObligationStatus) -> None:
        super().__init__(f"obligation transition {current.value} -> {target.value} is not allowed")
        self.current = current
        self.target = target


_ALLOWED_TRANSITIONS: dict[ObligationStatus, frozenset[ObligationStatus]] = {
    ObligationStatus.PLANNED: frozenset(
        {ObligationStatus.ACTIVE, ObligationStatus.WAIVED, ObligationStatus.CANCELLED}
    ),
    ObligationStatus.ACTIVE: frozenset(
        {
            ObligationStatus.COMPLETED,
            ObligationStatus.OVERDUE,
            ObligationStatus.WAIVED,
            ObligationStatus.CANCELLED,
        }
    ),
    ObligationStatus.OVERDUE: frozenset(
        {ObligationStatus.COMPLETED, ObligationStatus.WAIVED, ObligationStatus.CANCELLED}
    ),
    ObligationStatus.COMPLETED: frozenset(),
    ObligationStatus.WAIVED: frozenset(),
    ObligationStatus.CANCELLED: frozenset(),
}


def transition_obligation(
    current: ObligationStatus, target: ObligationStatus
) -> ObligationStatus:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidObligationTransition(current, target)
    return target


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    risk_type: RiskType
    severity: RiskSeverity
    obligation_status: ObligationStatus
    next_action_at: datetime


def assess_obligation(obligation: Obligation, now: datetime) -> RiskAssessment:
    if obligation.status not in {ObligationStatus.ACTIVE, ObligationStatus.OVERDUE}:
        raise ValueError("only active or overdue obligations can be assessed")
    grace = timedelta(seconds=obligation.grace_period_seconds)
    if now < obligation.due_at:
        return RiskAssessment(
            risk_type=RiskType.DUE_SOON,
            severity=RiskSeverity.LOW,
            obligation_status=ObligationStatus.ACTIVE,
            next_action_at=obligation.due_at,
        )
    if now < obligation.due_at + grace:
        return RiskAssessment(
            risk_type=RiskType.OVERDUE,
            severity=RiskSeverity.MEDIUM,
            obligation_status=ObligationStatus.OVERDUE,
            next_action_at=obligation.due_at + grace,
        )
    overdue_age = now - obligation.due_at
    severity = RiskSeverity.CRITICAL if overdue_age >= grace * 2 else RiskSeverity.HIGH
    return RiskAssessment(
        risk_type=RiskType.OVERDUE,
        severity=severity,
        obligation_status=ObligationStatus.OVERDUE,
        next_action_at=now + timedelta(days=1),
    )
