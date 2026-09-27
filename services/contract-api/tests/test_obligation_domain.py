from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from contractops.application.obligations import ObligationScheduler
from contractops.domain.obligation import (
    InvalidObligationTransition,
    Obligation,
    ObligationStatus,
    ObligationType,
    RiskAssessment,
    RiskSeverity,
    RiskType,
    assess_obligation,
    transition_obligation,
)


def _obligation(
    *,
    due_at: datetime,
    status: ObligationStatus = ObligationStatus.ACTIVE,
    grace_period_seconds: int = 86_400,
    next_action_at: datetime | None = None,
) -> Obligation:
    now = datetime.now(UTC)
    return Obligation(
        id=uuid4(),
        tenant_id=uuid4(),
        contract_id=uuid4(),
        obligation_type=ObligationType.PAYMENT,
        title="Pay annual service fee",
        owner_id=uuid4(),
        due_at=due_at,
        grace_period_seconds=grace_period_seconds,
        status=status,
        state_version=0,
        evidence_reference=None,
        next_action_at=next_action_at or due_at - timedelta(days=7),
        created_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def test_obligation_state_machine_rejects_terminal_reopen() -> None:
    assert (
        transition_obligation(ObligationStatus.ACTIVE, ObligationStatus.COMPLETED)
        is ObligationStatus.COMPLETED
    )
    with pytest.raises(InvalidObligationTransition):
        transition_obligation(ObligationStatus.COMPLETED, ObligationStatus.ACTIVE)


def test_due_soon_assessment_schedules_exact_due_time() -> None:
    now = datetime(2026, 9, 27, 8, tzinfo=UTC)
    obligation = _obligation(due_at=now + timedelta(days=2))

    assessment = assess_obligation(obligation, now)

    assert assessment.risk_type is RiskType.DUE_SOON
    assert assessment.severity is RiskSeverity.LOW
    assert assessment.obligation_status is ObligationStatus.ACTIVE
    assert assessment.next_action_at == obligation.due_at


def test_overdue_risk_escalates_by_grace_window() -> None:
    due_at = datetime(2026, 9, 20, tzinfo=UTC)
    obligation = _obligation(due_at=due_at, grace_period_seconds=86_400)

    medium = assess_obligation(obligation, due_at + timedelta(hours=2))
    high = assess_obligation(obligation, due_at + timedelta(days=1, hours=1))
    critical = assess_obligation(obligation, due_at + timedelta(days=2, hours=1))

    assert medium.severity is RiskSeverity.MEDIUM
    assert high.severity is RiskSeverity.HIGH
    assert critical.severity is RiskSeverity.CRITICAL
    assert {medium.risk_type, high.risk_type, critical.risk_type} == {RiskType.OVERDUE}


class FakeSchedulerStore:
    def __init__(self, obligations: Sequence[Obligation]) -> None:
        self.obligations = obligations
        self.applied: list[tuple[object, RiskSeverity, str]] = []

    def claim_due(
        self,
        worker_id: str,
        *,
        now: datetime,
        batch_size: int,
        lease_seconds: int,
    ) -> Sequence[Obligation]:
        del worker_id, now, lease_seconds
        return self.obligations[:batch_size]

    def apply_assessment(
        self,
        obligation: Obligation,
        assessment: RiskAssessment,
        *,
        worker_id: str,
        evaluated_at: datetime,
    ) -> bool:
        del evaluated_at
        self.applied.append((obligation.id, assessment.severity, worker_id))
        return True


def test_scheduler_claims_and_assesses_a_bounded_batch() -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    store = FakeSchedulerStore(
        (
            _obligation(due_at=now + timedelta(days=1)),
            _obligation(due_at=now - timedelta(days=3)),
        )
    )
    scheduler = ObligationScheduler(store, worker_id="scheduler-a", batch_size=1)

    assert scheduler.run_once(now) == 1
    assert store.applied == [
        (store.obligations[0].id, RiskSeverity.LOW, "scheduler-a")
    ]
