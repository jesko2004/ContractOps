from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from uuid import UUID

from contractops.context import ActorContext, Role
from contractops.domain.obligation import (
    Obligation,
    ObligationType,
    RiskAction,
    RiskAssessment,
    RiskEvent,
    assess_obligation,
)
from contractops.errors import ContractOpsError


@dataclass(frozen=True, slots=True)
class CreateObligationCommand:
    obligation_type: ObligationType
    title: str
    owner_id: UUID
    due_at: datetime
    grace_period_seconds: int = 86_400
    reminder_seconds_before: int = 604_800


@dataclass(frozen=True, slots=True)
class CompleteObligationCommand:
    expected_version: int
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class RiskActionCommand:
    action: RiskAction
    expected_version: int
    reason: str
    deferred_until: datetime | None = None


class ObligationRepository(Protocol):
    def activate_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]: ...

    def create_obligation(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: CreateObligationCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Obligation, bool]: ...

    def complete_obligation(
        self,
        actor: ActorContext,
        obligation_id: UUID,
        command: CompleteObligationCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Obligation, bool]: ...

    def list_risks(self, actor: ActorContext, *, limit: int) -> Sequence[RiskEvent]: ...

    def act_on_risk(
        self,
        actor: ActorContext,
        risk_id: UUID,
        command: RiskActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[RiskEvent, bool]: ...

    def terminate_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]: ...


class SchedulerObligationStore(Protocol):
    def claim_due(
        self,
        worker_id: str,
        *,
        now: datetime,
        batch_size: int,
        lease_seconds: int,
    ) -> Sequence[Obligation]: ...

    def apply_assessment(
        self,
        obligation: Obligation,
        assessment: RiskAssessment,
        *,
        worker_id: str,
        evaluated_at: datetime,
    ) -> bool: ...


def _request_hash(value: object) -> str:
    serialized = json.dumps(
        asdict(cast(Any, value)) if hasattr(value, "__dataclass_fields__") else value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ObligationService:
    _WRITER_ROLES = (Role.CONTRACT_OWNER, Role.LEGAL_ADMIN, Role.TENANT_ADMIN)
    _RISK_READER_ROLES = (
        Role.CONTRACT_OWNER,
        Role.APPROVER,
        Role.LEGAL_ADMIN,
        Role.FINANCE_APPROVER,
        Role.BUSINESS_APPROVER,
        Role.AUDITOR,
        Role.TENANT_ADMIN,
    )

    def __init__(self, repository: ObligationRepository) -> None:
        self._repository = repository

    @staticmethod
    def _require(actor: ActorContext, *roles: Role) -> None:
        if not actor.has_any_role(*roles):
            raise ContractOpsError(
                code="obligation_forbidden",
                message="the caller cannot manage obligations or risks",
                status_code=403,
            )

    def create_obligation(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: CreateObligationCommand,
        *,
        idempotency_key: str,
    ) -> tuple[Obligation, bool]:
        self._require(actor, *self._WRITER_ROLES)
        if command.due_at.utcoffset() is None:
            raise ContractOpsError(
                code="obligation_due_at_timezone_required",
                message="due_at must include a timezone",
            )
        if command.grace_period_seconds <= 0 or command.reminder_seconds_before < 0:
            raise ContractOpsError(
                code="obligation_schedule_invalid",
                message="grace period must be positive and reminder lead time cannot be negative",
            )
        return self._repository.create_obligation(
            actor,
            contract_id,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def activate_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> tuple[int, bool]:
        self._require(actor, Role.CONTRACT_OWNER, Role.TENANT_ADMIN)
        payload = {"expected_version": expected_version}
        return self._repository.activate_contract(
            actor,
            contract_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(payload),
        )

    def complete_obligation(
        self,
        actor: ActorContext,
        obligation_id: UUID,
        command: CompleteObligationCommand,
        *,
        idempotency_key: str,
    ) -> tuple[Obligation, bool]:
        self._require(actor, *self._WRITER_ROLES)
        if not command.evidence_reference.strip():
            raise ContractOpsError(
                code="obligation_evidence_required",
                message="completion evidence is required",
            )
        return self._repository.complete_obligation(
            actor,
            obligation_id,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def list_risks(self, actor: ActorContext, *, limit: int = 100) -> Sequence[RiskEvent]:
        self._require(actor, *self._RISK_READER_ROLES)
        return self._repository.list_risks(actor, limit=min(max(limit, 1), 200))

    def act_on_risk(
        self,
        actor: ActorContext,
        risk_id: UUID,
        command: RiskActionCommand,
        *,
        idempotency_key: str,
    ) -> tuple[RiskEvent, bool]:
        self._require(actor, *self._WRITER_ROLES)
        if command.action is RiskAction.DEFER:
            if command.deferred_until is None or command.deferred_until.utcoffset() is None:
                raise ContractOpsError(
                    code="risk_deferred_until_required",
                    message="a timezone-aware deferred_until is required",
                )
        elif command.deferred_until is not None:
            raise ContractOpsError(
                code="risk_deferred_until_not_allowed",
                message="deferred_until is only valid for a defer action",
            )
        return self._repository.act_on_risk(
            actor,
            risk_id,
            command,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(command),
        )

    def terminate_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
    ) -> tuple[int, bool]:
        self._require(actor, Role.CONTRACT_OWNER, Role.TENANT_ADMIN)
        payload = {
            "expected_version": expected_version,
            "reason": reason,
        }
        return self._repository.terminate_contract(
            actor,
            contract_id,
            expected_version=expected_version,
            reason=reason,
            idempotency_key=idempotency_key,
            request_hash=_request_hash(payload),
        )


class ObligationScheduler:
    def __init__(
        self,
        store: SchedulerObligationStore,
        *,
        worker_id: str,
        batch_size: int = 100,
        lease_seconds: int = 30,
    ) -> None:
        self._store = store
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds

    def run_once(self, now: datetime) -> int:
        processed = 0
        for obligation in self._store.claim_due(
            self._worker_id,
            now=now,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
        ):
            assessment = assess_obligation(obligation, now)
            if self._store.apply_assessment(
                obligation,
                assessment,
                worker_id=self._worker_id,
                evaluated_at=now,
            ):
                processed += 1
        return processed


def initial_next_action_at(command: CreateObligationCommand) -> datetime:
    return command.due_at - timedelta(seconds=command.reminder_seconds_before)
