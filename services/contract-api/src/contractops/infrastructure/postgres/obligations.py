from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from contractops.application.obligations import (
    CompleteObligationCommand,
    CreateObligationCommand,
    RiskActionCommand,
    initial_next_action_at,
)
from contractops.context import ActorContext
from contractops.domain.obligation import (
    Obligation,
    ObligationStatus,
    ObligationType,
    RiskAction,
    RiskAssessment,
    RiskEvent,
    RiskSeverity,
    RiskStatus,
    RiskType,
)
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database, WorkerDatabase


def _obligation_from_row(row: Mapping[str, Any] | RowMapping) -> Obligation:
    return Obligation(
        id=cast(UUID, row["id"]),
        tenant_id=cast(UUID, row["tenant_id"]),
        contract_id=cast(UUID, row["contract_id"]),
        obligation_type=ObligationType(row["obligation_type"]),
        title=cast(str, row["title"]),
        owner_id=cast(UUID, row["owner_id"]),
        due_at=row["due_at"],
        grace_period_seconds=cast(int, row["grace_period_seconds"]),
        status=ObligationStatus(row["status"]),
        state_version=cast(int, row["state_version"]),
        evidence_reference=cast(str | None, row["evidence_reference"]),
        next_action_at=row["next_action_at"],
        created_by=cast(UUID, row["created_by"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_by=cast(UUID | None, row["completed_by"]),
        completed_at=row["completed_at"],
        cancel_reason=cast(str | None, row["cancel_reason"]),
    )


def _risk_from_row(row: Mapping[str, Any] | RowMapping) -> RiskEvent:
    return RiskEvent(
        id=cast(UUID, row["id"]),
        tenant_id=cast(UUID, row["tenant_id"]),
        contract_id=cast(UUID, row["contract_id"]),
        obligation_id=cast(UUID, row["obligation_id"]),
        risk_type=RiskType(row["risk_type"]),
        severity=RiskSeverity(row["severity"]),
        status=RiskStatus(row["status"]),
        owner_id=cast(UUID, row["owner_id"]),
        state_version=cast(int, row["state_version"]),
        occurrence_count=cast(int, row["occurrence_count"]),
        first_detected_at=row["first_detected_at"],
        last_detected_at=row["last_detected_at"],
        deferred_until=row["deferred_until"],
        resolution=cast(str | None, row["resolution"]),
        resolved_by=cast(UUID | None, row["resolved_by"]),
        resolved_at=row["resolved_at"],
    )


def _not_found(resource: str) -> ContractOpsError:
    return ContractOpsError(
        code=f"{resource}_not_found",
        message=f"{resource.replace('_', ' ')} was not found",
        status_code=404,
    )


class PostgresObligationRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def activate_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]:
        operation = "contract.activate"
        with self._database.transaction(actor) as connection:
            replay = self._replay_body(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return int(replay["state_version"]), True
            version: int | None = cast(
                int | None,
                connection.execute(
                    text(
                    """
                    UPDATE contracts
                    SET status = 'ACTIVE', state_version = state_version + 1,
                        updated_at = now()
                    WHERE id = :id AND status = 'APPROVED'
                      AND state_version = :expected_version
                    RETURNING state_version
                    """
                    ),
                    {"id": contract_id, "expected_version": expected_version},
                ).scalar_one_or_none(),
            )
            if version is None:
                exists = connection.execute(
                    text("SELECT 1 FROM contracts WHERE id = :id"), {"id": contract_id}
                ).scalar_one_or_none()
                if exists is None:
                    raise _not_found("contract")
                raise ContractOpsError(
                    code="contract_activation_conflict",
                    message="only the expected version of an approved contract can be activated",
                    status_code=409,
                )
            body = {"state_version": version}
            self._emit(connection, actor, "contract.activated", "contract", contract_id, body)
            self._record(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                contract_id,
                body=body,
            )
            return version, False

    def create_obligation(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: CreateObligationCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Obligation, bool]:
        operation = "obligation.create"
        with self._database.transaction(actor) as connection:
            replay = self._replay(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return self._get_obligation(connection, replay), True
            contract = (
                connection.execute(
                    text("SELECT status FROM contracts WHERE id = :id FOR UPDATE"),
                    {"id": contract_id},
                )
                .mappings()
                .one_or_none()
            )
            if contract is None:
                raise _not_found("contract")
            if contract["status"] != "ACTIVE":
                raise ContractOpsError(
                    code="contract_not_active",
                    message="obligations can only be registered for an active contract",
                    status_code=409,
                )
            row = (
                connection.execute(
                    text(
                        """
                        INSERT INTO obligations (
                            tenant_id, contract_id, obligation_type, title, owner_id,
                            due_at, grace_period_seconds, status, next_action_at, created_by
                        ) VALUES (
                            :tenant_id, :contract_id, :obligation_type, :title, :owner_id,
                            :due_at, :grace_period_seconds, 'ACTIVE', :next_action_at, :created_by
                        ) RETURNING *
                        """
                    ),
                    {
                        "tenant_id": actor.tenant_id,
                        "contract_id": contract_id,
                        "obligation_type": command.obligation_type.value,
                        "title": command.title,
                        "owner_id": command.owner_id,
                        "due_at": command.due_at,
                        "grace_period_seconds": command.grace_period_seconds,
                        "next_action_at": initial_next_action_at(command),
                        "created_by": actor.user_id,
                    },
                )
                .mappings()
                .one()
            )
            obligation = _obligation_from_row(row)
            self._emit(
                connection,
                actor,
                "obligation.created",
                "obligation",
                obligation.id,
                {"contract_id": str(contract_id), "due_at": command.due_at.isoformat()},
            )
            self._record(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                obligation.id,
            )
            return obligation, False

    def complete_obligation(
        self,
        actor: ActorContext,
        obligation_id: UUID,
        command: CompleteObligationCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Obligation, bool]:
        operation = "obligation.complete"
        with self._database.transaction(actor) as connection:
            replay = self._replay(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return self._get_obligation(connection, replay), True
            row = (
                connection.execute(
                    text(
                        """
                        UPDATE obligations
                        SET status = 'COMPLETED', state_version = state_version + 1,
                            evidence_reference = :evidence_reference, completed_by = :actor_id,
                            completed_at = now(), next_action_at = NULL,
                            lease_owner = NULL, lease_until = NULL, updated_at = now()
                        WHERE id = :id AND status IN ('ACTIVE', 'OVERDUE')
                          AND state_version = :expected_version
                        RETURNING *
                        """
                    ),
                    {
                        "id": obligation_id,
                        "evidence_reference": command.evidence_reference,
                        "actor_id": actor.user_id,
                        "expected_version": command.expected_version,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                self._raise_obligation_conflict(connection, obligation_id)
            obligation = _obligation_from_row(cast(RowMapping, row))
            connection.execute(
                text(
                    """
                    UPDATE risk_events
                    SET status = 'RESOLVED', resolution = 'obligation completed',
                        resolved_by = :actor_id, resolved_at = now(),
                        state_version = state_version + 1, updated_at = now()
                    WHERE obligation_id = :obligation_id AND status <> 'RESOLVED'
                    """
                ),
                {"obligation_id": obligation_id, "actor_id": actor.user_id},
            )
            self._emit(
                connection,
                actor,
                "obligation.completed",
                "obligation",
                obligation.id,
                {"contract_id": str(obligation.contract_id)},
            )
            self._record(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                obligation.id,
            )
            return obligation, False

    def list_risks(self, actor: ActorContext, *, limit: int) -> Sequence[RiskEvent]:
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT * FROM risk_events
                        WHERE status <> 'RESOLVED'
                        ORDER BY CASE severity
                            WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                            WHEN 'MEDIUM' THEN 3 ELSE 4 END,
                            last_detected_at DESC, id DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
            return tuple(_risk_from_row(row) for row in rows)

    def act_on_risk(
        self,
        actor: ActorContext,
        risk_id: UUID,
        command: RiskActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[RiskEvent, bool]:
        operation = f"risk.{command.action.value.lower()}"
        with self._database.transaction(actor) as connection:
            replay = self._replay(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return self._get_risk(connection, replay), True
            target = {
                RiskAction.ACKNOWLEDGE: RiskStatus.ACKNOWLEDGED,
                RiskAction.DEFER: RiskStatus.DEFERRED,
                RiskAction.RESOLVE: RiskStatus.RESOLVED,
            }[command.action]
            row = (
                connection.execute(
                    text(
                        """
                        UPDATE risk_events
                        SET status = :status, state_version = state_version + 1,
                            deferred_until = CASE WHEN :status = 'DEFERRED'
                                THEN :deferred_until ELSE NULL END,
                            resolution = CASE WHEN :status = 'RESOLVED' THEN :reason
                                ELSE resolution END,
                            resolved_by = CASE WHEN :status = 'RESOLVED' THEN :actor_id
                                ELSE NULL END,
                            resolved_at = CASE WHEN :status = 'RESOLVED' THEN now()
                                ELSE NULL END,
                            updated_at = now()
                        WHERE id = :id AND status <> 'RESOLVED'
                          AND state_version = :expected_version
                        RETURNING *
                        """
                    ),
                    {
                        "id": risk_id,
                        "status": target.value,
                        "deferred_until": command.deferred_until,
                        "reason": command.reason,
                        "actor_id": actor.user_id,
                        "expected_version": command.expected_version,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                existing = connection.execute(
                    text("SELECT 1 FROM risk_events WHERE id = :id"), {"id": risk_id}
                ).scalar_one_or_none()
                if existing is None:
                    raise _not_found("risk_event")
                raise ContractOpsError(
                    code="risk_event_conflict",
                    message="risk event state changed or is already resolved",
                    status_code=409,
                )
            risk = _risk_from_row(row)
            self._emit(
                connection,
                actor,
                f"risk.{command.action.value.lower()}",
                "risk_event",
                risk.id,
                {"obligation_id": str(risk.obligation_id), "reason": command.reason},
            )
            self._record(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                risk.id,
            )
            return risk, False

    def terminate_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[int, bool]:
        operation = "contract.terminate"
        with self._database.transaction(actor) as connection:
            replay = self._replay_body(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return int(replay.get("cancelled_obligations", 0)), True
            updated: UUID | None = cast(
                UUID | None,
                connection.execute(
                    text(
                    """
                    UPDATE contracts
                    SET status = 'TERMINATED', state_version = state_version + 1,
                        updated_at = now()
                    WHERE id = :id AND status = 'ACTIVE'
                      AND state_version = :expected_version
                    RETURNING id
                    """
                    ),
                    {"id": contract_id, "expected_version": expected_version},
                ).scalar_one_or_none(),
            )
            if updated is None:
                exists = connection.execute(
                    text("SELECT 1 FROM contracts WHERE id = :id"), {"id": contract_id}
                ).scalar_one_or_none()
                if exists is None:
                    raise _not_found("contract")
                raise ContractOpsError(
                    code="contract_termination_conflict",
                    message="only the expected version of an active contract can be terminated",
                    status_code=409,
                )
            cancelled = cast(
                int,
                connection.execute(
                    text(
                        """
                        WITH cancelled AS (
                            UPDATE obligations
                            SET status = 'CANCELLED', cancel_reason = :reason,
                                state_version = state_version + 1, next_action_at = NULL,
                                lease_owner = NULL, lease_until = NULL, updated_at = now()
                            WHERE contract_id = :contract_id
                              AND status IN ('PLANNED', 'ACTIVE', 'OVERDUE')
                            RETURNING id
                        ) SELECT count(*) FROM cancelled
                        """
                    ),
                    {"contract_id": contract_id, "reason": reason},
                ).scalar_one(),
            )
            connection.execute(
                text(
                    """
                    UPDATE risk_events
                    SET status = 'RESOLVED', resolution = :reason,
                        resolved_by = :actor_id, resolved_at = now(),
                        state_version = state_version + 1, updated_at = now()
                    WHERE contract_id = :contract_id AND status <> 'RESOLVED'
                    """
                ),
                {"contract_id": contract_id, "reason": reason, "actor_id": actor.user_id},
            )
            body = {"cancelled_obligations": cancelled}
            self._emit(connection, actor, "contract.terminated", "contract", contract_id, body)
            self._record(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                contract_id,
                body=body,
            )
            return cancelled, False

    @staticmethod
    def _raise_obligation_conflict(connection: Connection, obligation_id: UUID) -> None:
        exists = connection.execute(
            text("SELECT 1 FROM obligations WHERE id = :id"), {"id": obligation_id}
        ).scalar_one_or_none()
        if exists is None:
            raise _not_found("obligation")
        raise ContractOpsError(
            code="obligation_conflict",
            message="obligation state or version changed",
            status_code=409,
        )

    @staticmethod
    def _get_obligation(connection: Connection, obligation_id: UUID) -> Obligation:
        row = connection.execute(
            text("SELECT * FROM obligations WHERE id = :id"), {"id": obligation_id}
        ).mappings().one_or_none()
        if row is None:
            raise _not_found("obligation")
        return _obligation_from_row(row)

    @staticmethod
    def _get_risk(connection: Connection, risk_id: UUID) -> RiskEvent:
        row = connection.execute(
            text("SELECT * FROM risk_events WHERE id = :id"), {"id": risk_id}
        ).mappings().one_or_none()
        if row is None:
            raise _not_found("risk_event")
        return _risk_from_row(row)

    @staticmethod
    def _lock(
        connection: Connection, actor: ActorContext, operation: str, idempotency_key: str
    ) -> None:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"),
            {"value": f"{actor.tenant_id}:{operation}:{idempotency_key}"},
        )

    def _replay(
        self,
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> UUID | None:
        body = self._replay_row(
            connection, actor, operation, idempotency_key, request_hash
        )
        return None if body is None else cast(UUID, body["resource_id"])

    def _replay_body(
        self,
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        row = self._replay_row(
            connection, actor, operation, idempotency_key, request_hash
        )
        if row is None:
            return None
        value = row["response_body"]
        return json.loads(value) if isinstance(value, str) else cast(dict[str, Any], value)

    def _replay_row(
        self,
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RowMapping | None:
        self._lock(connection, actor, operation, idempotency_key)
        row = connection.execute(
            text(
                """
                SELECT request_hash, resource_id, response_body
                FROM idempotency_records
                WHERE operation = :operation AND idempotency_key = :idempotency_key
                """
            ),
            {"operation": operation, "idempotency_key": idempotency_key},
        ).mappings().one_or_none()
        if row is not None and row["request_hash"] != request_hash:
            raise ContractOpsError(
                code="idempotency_key_reused",
                message="idempotency key was already used with a different request",
                status_code=409,
            )
        return row

    @staticmethod
    def _record(
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        resource_id: UUID,
        *,
        body: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    tenant_id, operation, idempotency_key, request_hash,
                    response_status, response_body, resource_id, expires_at
                ) VALUES (
                    :tenant_id, :operation, :idempotency_key, :request_hash,
                    200, CAST(:body AS jsonb), :resource_id, now() + interval '24 hours'
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "body": json.dumps(body or {}),
                "resource_id": resource_id,
            },
        )

    @staticmethod
    def _emit(
        connection: Connection,
        actor: ActorContext,
        event_type: str,
        resource_type: str,
        resource_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO outbox_events (
                    tenant_id, event_type, aggregate_type, aggregate_id, payload
                ) VALUES (
                    :tenant_id, :event_type, :resource_type, :resource_id,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "event_type": event_type,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "payload": json.dumps(payload),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    tenant_id, actor_id, action, resource_type, resource_id,
                    outcome, metadata
                ) VALUES (
                    :tenant_id, :actor_id, :event_type, :resource_type,
                    :resource_id, 'SUCCEEDED', CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "actor_id": actor.user_id,
                "event_type": event_type,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "payload": json.dumps(payload),
            },
        )


class PostgresSchedulerObligationStore:
    def __init__(self, database: WorkerDatabase) -> None:
        self._database = database

    def claim_due(
        self,
        worker_id: str,
        *,
        now: datetime,
        batch_size: int,
        lease_seconds: int,
    ) -> Sequence[Obligation]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                text(
                    """
                    WITH candidates AS (
                        SELECT id FROM obligations
                        WHERE status IN ('ACTIVE', 'OVERDUE')
                          AND next_action_at <= :now
                          AND (lease_until IS NULL OR lease_until < :now)
                        ORDER BY next_action_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT :batch_size
                    )
                    UPDATE obligations AS obligation
                    SET lease_owner = :worker_id,
                        lease_until = :now + make_interval(secs => :lease_seconds),
                        updated_at = now()
                    FROM candidates
                    WHERE obligation.id = candidates.id
                    RETURNING obligation.*
                    """
                ),
                {
                    "worker_id": worker_id,
                    "now": now,
                    "batch_size": batch_size,
                    "lease_seconds": lease_seconds,
                },
            ).mappings().all()
            return tuple(_obligation_from_row(row) for row in rows)

    def apply_assessment(
        self,
        obligation: Obligation,
        assessment: RiskAssessment,
        *,
        worker_id: str,
        evaluated_at: datetime,
    ) -> bool:
        with self._database.transaction() as connection:
            current = connection.execute(
                text(
                    """
                    SELECT obligation.*, contract.status AS contract_status
                    FROM obligations AS obligation
                    JOIN contracts AS contract
                      ON contract.tenant_id = obligation.tenant_id
                     AND contract.id = obligation.contract_id
                    WHERE obligation.id = :id
                    FOR UPDATE OF obligation
                    """
                ),
                {"id": obligation.id},
            ).mappings().one_or_none()
            if current is None:
                return False
            if current["lease_owner"] != worker_id:
                return False
            if current["contract_status"] in {"TERMINATED", "EXPIRED"}:
                connection.execute(
                    text(
                        """
                        UPDATE obligations
                        SET status = 'CANCELLED', cancel_reason = 'contract is terminal',
                            state_version = state_version + 1, next_action_at = NULL,
                            lease_owner = NULL, lease_until = NULL, updated_at = now()
                        WHERE id = :id
                        """
                    ),
                    {"id": obligation.id},
                )
                connection.execute(
                    text(
                        """
                        UPDATE risk_events
                        SET status = 'RESOLVED', resolution = 'contract is terminal',
                            resolved_at = now(), state_version = state_version + 1,
                            updated_at = now()
                        WHERE obligation_id = :id AND status <> 'RESOLVED'
                        """
                    ),
                    {"id": obligation.id},
                )
                return True
            if current["status"] not in {"ACTIVE", "OVERDUE"}:
                return False
            deferred_until: datetime | None = cast(
                datetime | None,
                connection.execute(
                    text(
                    """
                    SELECT deferred_until FROM risk_events
                    WHERE obligation_id = :id AND risk_type = :risk_type
                      AND status = 'DEFERRED' AND deferred_until > :evaluated_at
                    """
                    ),
                    {
                        "id": obligation.id,
                        "risk_type": assessment.risk_type.value,
                        "evaluated_at": evaluated_at,
                    },
                ).scalar_one_or_none(),
            )
            if deferred_until is not None:
                connection.execute(
                    text(
                        """
                        UPDATE obligations
                        SET next_action_at = :deferred_until,
                            lease_owner = NULL, lease_until = NULL, updated_at = now()
                        WHERE id = :id AND lease_owner = :worker_id
                        """
                    ),
                    {
                        "deferred_until": deferred_until,
                        "id": obligation.id,
                        "worker_id": worker_id,
                    },
                )
                return True
            if assessment.risk_type is RiskType.OVERDUE:
                connection.execute(
                    text(
                        """
                        UPDATE risk_events
                        SET status = 'RESOLVED', resolution = 'obligation became overdue',
                            resolved_at = :evaluated_at,
                            state_version = state_version + 1, updated_at = now()
                        WHERE obligation_id = :id AND risk_type = 'DUE_SOON'
                          AND status <> 'RESOLVED'
                        """
                    ),
                    {"id": obligation.id, "evaluated_at": evaluated_at},
                )
            risk_id = cast(
                UUID,
                connection.execute(
                    text(
                        """
                        INSERT INTO risk_events (
                            tenant_id, contract_id, obligation_id, risk_type,
                            severity, status, owner_id, first_detected_at,
                            last_detected_at
                        ) VALUES (
                            :tenant_id, :contract_id, :obligation_id, :risk_type,
                            :severity, 'OPEN', :owner_id, :evaluated_at, :evaluated_at
                        )
                        ON CONFLICT (tenant_id, obligation_id, risk_type)
                            WHERE status <> 'RESOLVED'
                        DO UPDATE SET
                            severity = EXCLUDED.severity,
                            status = CASE
                                WHEN risk_events.status = 'DEFERRED'
                                  AND risk_events.deferred_until > :evaluated_at
                                THEN risk_events.status ELSE 'OPEN' END,
                            occurrence_count = risk_events.occurrence_count + 1,
                            last_detected_at = :evaluated_at,
                            state_version = risk_events.state_version + 1,
                            updated_at = now()
                        RETURNING id
                        """
                    ),
                    {
                        "tenant_id": obligation.tenant_id,
                        "contract_id": obligation.contract_id,
                        "obligation_id": obligation.id,
                        "risk_type": assessment.risk_type.value,
                        "severity": assessment.severity.value,
                        "owner_id": obligation.owner_id,
                        "evaluated_at": evaluated_at,
                    },
                ).scalar_one(),
            )
            scheduled_at = current["next_action_at"] or evaluated_at
            reminder_id: UUID | None = cast(
                UUID | None,
                connection.execute(
                    text(
                    """
                    INSERT INTO obligation_reminders (
                        tenant_id, obligation_id, reminder_type, scheduled_at
                    ) VALUES (
                        :tenant_id, :obligation_id, :reminder_type, :scheduled_at
                    )
                    ON CONFLICT (tenant_id, obligation_id, reminder_type, scheduled_at)
                    DO NOTHING
                    RETURNING id
                    """
                    ),
                    {
                        "tenant_id": obligation.tenant_id,
                        "obligation_id": obligation.id,
                        "reminder_type": (
                            f"{assessment.risk_type.value}.{assessment.severity.value}"
                        ),
                        "scheduled_at": scheduled_at,
                    },
                ).scalar_one_or_none(),
            )
            if reminder_id is not None:
                event_id: UUID = cast(
                    UUID,
                    connection.execute(
                        text(
                        """
                        INSERT INTO outbox_events (
                            tenant_id, event_type, aggregate_type, aggregate_id, payload
                        ) VALUES (
                            :tenant_id, 'obligation.risk_detected', 'risk_event',
                            :risk_id, CAST(:payload AS jsonb)
                        ) RETURNING id
                        """
                        ),
                        {
                            "tenant_id": obligation.tenant_id,
                            "risk_id": risk_id,
                            "payload": json.dumps(
                                {
                                    "obligation_id": str(obligation.id),
                                    "contract_id": str(obligation.contract_id),
                                    "risk_type": assessment.risk_type.value,
                                    "severity": assessment.severity.value,
                                    "owner_id": str(obligation.owner_id),
                                }
                            ),
                        },
                    ).scalar_one(),
                )
                connection.execute(
                    text(
                        "UPDATE obligation_reminders SET outbox_event_id = :event_id "
                        "WHERE id = :id"
                    ),
                    {"event_id": event_id, "id": reminder_id},
                )
            connection.execute(
                text(
                    """
                    UPDATE obligations
                    SET status = :status, next_action_at = :next_action_at,
                        state_version = state_version + 1,
                        lease_owner = NULL, lease_until = NULL, updated_at = now()
                    WHERE id = :id AND lease_owner = :worker_id
                    """
                ),
                {
                    "status": assessment.obligation_status.value,
                    "next_action_at": assessment.next_action_at,
                    "id": obligation.id,
                    "worker_id": worker_id,
                },
            )
            return True
