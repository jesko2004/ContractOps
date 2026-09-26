from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from contractops.application.approvals import (
    ApprovalWorkflow,
    CreateApprovalPolicyCommand,
    StepActionCommand,
    TransferStepCommand,
)
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.approval import (
    ApprovalDecision,
    ApprovalInstance,
    ApprovalInstanceStatus,
    ApprovalPolicy,
    ApprovalStep,
    ApprovalStepStatus,
    ApprovalStepTemplate,
    PolicyCondition,
    PolicyStatus,
    initial_step_statuses,
)
from contractops.domain.contract import Contract, ContractStatus
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database


class PostgresApprovalWorkflow(ApprovalWorkflow):
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_policy(
        self,
        actor: ActorContext,
        command: CreateApprovalPolicyCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalPolicy, bool]:
        operation = "approval.policy.create"
        with self._database.transaction(actor) as connection:
            self._lock_idempotency(connection, actor, operation, idempotency_key)
            replay_id = self._replay_resource_id(
                connection, operation, idempotency_key, request_hash
            )
            if replay_id is not None:
                replay = self._load_policy(connection, replay_id)
                if replay is None:
                    raise RuntimeError("idempotency record references a missing policy")
                return replay, True

            policy_lock_key = f"{actor.tenant_id}:approval-policy:{command.policy_key}"
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": policy_lock_key},
            )
            draft_id = connection.execute(
                text(
                    """
                    SELECT id FROM approval_policies
                    WHERE policy_key = :policy_key AND status = 'DRAFT'
                    """
                ),
                {"policy_key": command.policy_key},
            ).scalar_one_or_none()
            if draft_id is not None:
                raise ContractOpsError(
                    code="approval_policy_draft_exists",
                    message="publish or remove the existing draft before creating another",
                    status_code=409,
                )

            version_number = cast(
                int,
                connection.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(version_number), 0) + 1
                        FROM approval_policies WHERE policy_key = :policy_key
                        """
                    ),
                    {"policy_key": command.policy_key},
                ).scalar_one(),
            )
            policy_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO approval_policies (
                        id, tenant_id, policy_key, version_number, name, priority,
                        status, condition, steps, created_by
                    ) VALUES (
                        :id, :tenant_id, :policy_key, :version_number, :name, :priority,
                        'DRAFT', CAST(:condition AS jsonb), CAST(:steps AS jsonb), :created_by
                    )
                    """
                ),
                {
                    "id": policy_id,
                    "tenant_id": actor.tenant_id,
                    "policy_key": command.policy_key,
                    "version_number": version_number,
                    "name": command.name,
                    "priority": command.priority,
                    "condition": json.dumps(command.condition.snapshot()),
                    "steps": json.dumps([step.snapshot() for step in command.steps]),
                    "created_by": actor.user_id,
                },
            )
            self._record_idempotency(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                policy_id,
            )
            policy = self._load_policy(connection, policy_id)
            if policy is None:
                raise RuntimeError("created approval policy could not be reloaded")
            return policy, False

    def publish_policy(self, actor: ActorContext, policy_id: UUID) -> ApprovalPolicy:
        with self._database.transaction(actor) as connection:
            status = cast(
                str | None,
                connection.execute(
                    text("SELECT status FROM approval_policies WHERE id = :id FOR UPDATE"),
                    {"id": policy_id},
                ).scalar_one_or_none(),
            )
            if status is None:
                raise ContractOpsError(
                    code="approval_policy_not_found",
                    message="approval policy was not found",
                    status_code=404,
                )
            if status == PolicyStatus.DRAFT.value:
                connection.execute(
                    text(
                        """
                        UPDATE approval_policies
                        SET status = 'PUBLISHED', published_by = :actor_id, published_at = now()
                        WHERE id = :id AND status = 'DRAFT'
                        """
                    ),
                    {"id": policy_id, "actor_id": actor.user_id},
                )
            policy = self._load_policy(connection, policy_id)
            if policy is None:
                raise RuntimeError("published approval policy could not be reloaded")
            return policy

    def submit_contract(
        self,
        actor: ActorContext,
        contract_id: UUID,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalInstance, bool]:
        operation = f"approval.submit:{contract_id}"
        with self._database.transaction(actor) as connection:
            self._lock_idempotency(connection, actor, operation, idempotency_key)
            replay_id = self._replay_resource_id(
                connection, operation, idempotency_key, request_hash
            )
            if replay_id is not None:
                replay = self._load_instance(connection, replay_id)
                if replay is None:
                    raise RuntimeError("idempotency record references a missing instance")
                return replay, True

            row = (
                connection.execute(
                    text(
                        """
                        SELECT id, tenant_id, contract_number, title, contract_type,
                               counterparty_name, department_id, amount, currency,
                               valid_from, valid_until, status, current_version_id,
                               state_version, created_by, created_at, updated_at
                        FROM contracts WHERE id = :id FOR UPDATE
                        """
                    ),
                    {"id": contract_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ContractOpsError(
                    code="contract_not_found",
                    message="contract was not found",
                    status_code=404,
                )
            contract = self._contract_from_row(row)
            if contract.status is not ContractStatus.DRAFT:
                raise ContractOpsError(
                    code="contract_not_submittable",
                    message="only draft contracts can be submitted",
                    status_code=409,
                )
            if contract.current_version_id is None:
                raise ContractOpsError(
                    code="contract_version_required",
                    message="a contract version is required before submission",
                )
            policy = self._select_policy(connection, contract)
            if policy is None:
                raise ContractOpsError(
                    code="approval_policy_not_matched",
                    message="no published approval policy matches this contract",
                    status_code=409,
                )

            instance_id = uuid4()
            snapshot = policy.snapshot()
            connection.execute(
                text(
                    """
                    INSERT INTO approval_instances (
                        id, tenant_id, contract_id, contract_version_id, policy_id,
                        policy_snapshot, requested_by
                    ) VALUES (
                        :id, :tenant_id, :contract_id, :contract_version_id, :policy_id,
                        CAST(:policy_snapshot AS jsonb), :requested_by
                    )
                    """
                ),
                {
                    "id": instance_id,
                    "tenant_id": actor.tenant_id,
                    "contract_id": contract.id,
                    "contract_version_id": contract.current_version_id,
                    "policy_id": policy.id,
                    "policy_snapshot": json.dumps(snapshot),
                    "requested_by": actor.user_id,
                },
            )
            statuses = initial_step_statuses(contract, policy.steps)
            if ApprovalStepStatus.READY not in statuses:
                raise ContractOpsError(
                    code="approval_policy_no_applicable_steps",
                    message="the matched policy has no applicable approval steps",
                    status_code=409,
                )
            for template, step_status in zip(policy.steps, statuses, strict=True):
                connection.execute(
                    text(
                        """
                        INSERT INTO approval_workflow_steps (
                            id, tenant_id, approval_instance_id, step_order, name,
                            required_role, status
                        ) VALUES (
                            :id, :tenant_id, :instance_id, :step_order, :name,
                            :required_role, :status
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "tenant_id": actor.tenant_id,
                        "instance_id": instance_id,
                        "step_order": template.step_order,
                        "name": template.name,
                        "required_role": template.required_role.value,
                        "status": step_status.value,
                    },
                )
            self._set_contract_status(connection, contract.id, "SUBMITTED")
            self._set_contract_status(connection, contract.id, "IN_APPROVAL")
            self._record_idempotency(
                connection,
                actor,
                operation,
                idempotency_key,
                request_hash,
                instance_id,
            )
            self._emit_event(
                connection,
                actor,
                "approval.instance.started",
                "approval_instance",
                instance_id,
                {"contract_id": str(contract.id), "policy_id": str(policy.id)},
            )
            instance = self._load_instance(connection, instance_id)
            if instance is None:
                raise RuntimeError("created approval instance could not be reloaded")
            return instance, False

    def get_instance(
        self, actor: ActorContext, instance_id: UUID
    ) -> ApprovalInstance | None:
        with self._database.transaction(actor) as connection:
            return self._load_instance(connection, instance_id)

    def list_tasks(self, actor: ActorContext) -> tuple[ApprovalStep, ...]:
        role_values = [role.value for role in actor.roles]
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT step.*
                        FROM approval_workflow_steps AS step
                        JOIN approval_instances AS instance
                          ON instance.tenant_id = step.tenant_id
                         AND instance.id = step.approval_instance_id
                        JOIN contracts AS contract
                          ON contract.tenant_id = instance.tenant_id
                         AND contract.id = instance.contract_id
                        WHERE step.status IN ('READY', 'CLAIMED')
                          AND (
                            step.assigned_to = :user_id
                            OR (
                              step.assigned_to IS NULL
                              AND (
                                step.required_role = ANY(CAST(:roles AS text[]))
                                OR 'APPROVER' = ANY(CAST(:roles AS text[]))
                                OR 'TENANT_ADMIN' = ANY(CAST(:roles AS text[]))
                              )
                            )
                          )
                          AND (
                            :data_scope = 'TENANT'
                            OR contract.department_id = ANY(CAST(:department_ids AS uuid[]))
                            OR (:data_scope = 'OWN' AND step.assigned_to = :user_id)
                          )
                        ORDER BY step.created_at, step.step_order
                        """
                    ),
                    {
                        "user_id": actor.user_id,
                        "roles": role_values,
                        "data_scope": actor.data_scope.value,
                        "department_ids": [str(value) for value in actor.department_ids],
                    },
                )
                .mappings()
                .all()
            )
            return tuple(self._step_from_row(row) for row in rows)

    def claim_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: StepActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]:
        operation = f"approval.step.claim:{step_id}"
        with self._database.transaction(actor) as connection:
            replay = self._step_replay(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return replay, True
            row = self._lock_step(connection, step_id)
            self._authorize_step(actor, row, require_assignment=False)
            updated = connection.execute(
                text(
                    """
                    UPDATE approval_workflow_steps
                    SET status = 'CLAIMED', assigned_to = :actor_id,
                        state_version = state_version + 1, updated_at = now()
                    WHERE id = :id AND status = 'READY' AND state_version = :expected_version
                    RETURNING *
                    """
                ),
                {
                    "id": step_id,
                    "actor_id": actor.user_id,
                    "expected_version": command.expected_version,
                },
            ).mappings().one_or_none()
            if updated is None:
                self._conflict()
            self._record_idempotency(
                connection, actor, operation, idempotency_key, request_hash, step_id
            )
            return self._step_from_row(updated), False

    def decide_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        decision: ApprovalDecision,
        command: StepActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]:
        operation = f"approval.step.decide:{step_id}"
        with self._database.transaction(actor) as connection:
            replay = self._step_replay(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return replay, True
            row = self._lock_step(connection, step_id)
            self._authorize_step(actor, row, require_assignment=True)
            target_status = {
                ApprovalDecision.APPROVE: ApprovalStepStatus.APPROVED,
                ApprovalDecision.REJECT: ApprovalStepStatus.REJECTED,
                ApprovalDecision.REQUEST_CHANGES: ApprovalStepStatus.CHANGES_REQUESTED,
            }[decision]
            updated = connection.execute(
                text(
                    """
                    UPDATE approval_workflow_steps
                    SET status = :status, decided_by = :actor_id,
                        decision_comment = :comment, decided_at = now(),
                        state_version = state_version + 1, updated_at = now()
                    WHERE id = :id AND status = 'CLAIMED'
                      AND assigned_to = :actor_id AND state_version = :expected_version
                    RETURNING *
                    """
                ),
                {
                    "id": step_id,
                    "status": target_status.value,
                    "actor_id": actor.user_id,
                    "comment": command.comment,
                    "expected_version": command.expected_version,
                },
            ).mappings().one_or_none()
            if updated is None:
                self._conflict()
            instance_id = cast(UUID, row["approval_instance_id"])
            if decision is ApprovalDecision.APPROVE:
                next_id = cast(
                    UUID | None,
                    connection.execute(
                        text(
                            """
                            SELECT id FROM approval_workflow_steps
                            WHERE approval_instance_id = :instance_id AND status = 'PENDING'
                            ORDER BY step_order LIMIT 1
                            """
                        ),
                        {"instance_id": instance_id},
                    ).scalar_one_or_none(),
                )
                if next_id is None:
                    self._complete_instance(connection, instance_id, "APPROVED")
                else:
                    connection.execute(
                        text(
                            """
                            UPDATE approval_workflow_steps
                            SET status = 'READY', state_version = state_version + 1,
                                updated_at = now()
                            WHERE id = :id AND status = 'PENDING'
                            """
                        ),
                        {"id": next_id},
                    )
            elif decision is ApprovalDecision.REJECT:
                self._complete_instance(connection, instance_id, "REJECTED")
            else:
                self._complete_instance(connection, instance_id, "CHANGES_REQUESTED")
            self._record_idempotency(
                connection, actor, operation, idempotency_key, request_hash, step_id
            )
            self._emit_event(
                connection,
                actor,
                f"approval.step.{decision.value.lower()}",
                "approval_step",
                step_id,
                {"instance_id": str(instance_id)},
            )
            return self._step_from_row(updated), False

    def transfer_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: TransferStepCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]:
        operation = f"approval.step.transfer:{step_id}"
        with self._database.transaction(actor) as connection:
            replay = self._step_replay(
                connection, actor, operation, idempotency_key, request_hash
            )
            if replay is not None:
                return replay, True
            row = self._lock_step(connection, step_id)
            self._authorize_step(actor, row, require_assignment=True)
            updated = connection.execute(
                text(
                    """
                    UPDATE approval_workflow_steps
                    SET status = 'READY', assigned_to = :target_user_id,
                        transfer_comment = :comment, transferred_by = :actor_id,
                        state_version = state_version + 1, updated_at = now()
                    WHERE id = :id AND status = 'CLAIMED'
                      AND assigned_to = :actor_id AND state_version = :expected_version
                    RETURNING *
                    """
                ),
                {
                    "id": step_id,
                    "target_user_id": command.target_user_id,
                    "comment": command.comment,
                    "actor_id": actor.user_id,
                    "expected_version": command.expected_version,
                },
            ).mappings().one_or_none()
            if updated is None:
                self._conflict()
            self._record_idempotency(
                connection, actor, operation, idempotency_key, request_hash, step_id
            )
            return self._step_from_row(updated), False

    def _select_policy(
        self, connection: Connection, contract: Contract
    ) -> ApprovalPolicy | None:
        rows = (
            connection.execute(
                text(
                    """
                    SELECT DISTINCT ON (policy_key) *
                    FROM approval_policies
                    WHERE status = 'PUBLISHED'
                    ORDER BY policy_key, version_number DESC
                    """
                )
            )
            .mappings()
            .all()
        )
        candidates = [self._policy_from_row(row) for row in rows]
        matches = [item for item in candidates if item.condition.matches(contract)]
        matches.sort(key=lambda item: (item.priority, item.version_number), reverse=True)
        return matches[0] if matches else None

    @staticmethod
    def _set_contract_status(
        connection: Connection, contract_id: UUID, status: str
    ) -> None:
        connection.execute(
            text(
                """
                UPDATE contracts SET status = :status, state_version = state_version + 1,
                    updated_at = now() WHERE id = :id
                """
            ),
            {"id": contract_id, "status": status},
        )

    def _complete_instance(
        self, connection: Connection, instance_id: UUID, status: str
    ) -> None:
        contract_id = cast(
            UUID,
            connection.execute(
                text(
                    """
                    UPDATE approval_instances
                    SET status = :status, state_version = state_version + 1,
                        completed_at = now(), updated_at = now()
                    WHERE id = :id AND status = 'IN_PROGRESS'
                    RETURNING contract_id
                    """
                ),
                {"id": instance_id, "status": status},
            ).scalar_one(),
        )
        self._set_contract_status(connection, contract_id, status)

    def _lock_step(self, connection: Connection, step_id: UUID) -> RowMapping:
        row = (
            connection.execute(
                text(
                    """
                    SELECT step.*, contract.department_id
                    FROM approval_workflow_steps AS step
                    JOIN approval_instances AS instance
                      ON instance.tenant_id = step.tenant_id
                     AND instance.id = step.approval_instance_id
                    JOIN contracts AS contract
                      ON contract.tenant_id = instance.tenant_id
                     AND contract.id = instance.contract_id
                    WHERE step.id = :id FOR UPDATE OF step
                    """
                ),
                {"id": step_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ContractOpsError(
                code="approval_step_not_found",
                message="approval step was not found",
                status_code=404,
            )
        return row

    @staticmethod
    def _authorize_step(
        actor: ActorContext, row: RowMapping, *, require_assignment: bool
    ) -> None:
        department_id = cast(UUID, row["department_id"])
        if actor.data_scope is DataScope.DEPARTMENT and department_id not in actor.department_ids:
            raise ContractOpsError(
                code="authorization_denied",
                message="the approval task is outside the caller's data scope",
                status_code=403,
            )
        assigned_to = cast(UUID | None, row["assigned_to"])
        required_role = Role(cast(str, row["required_role"]))
        privileged = actor.has_any_role(Role.APPROVER, Role.TENANT_ADMIN)
        if actor.data_scope is DataScope.OWN and assigned_to != actor.user_id:
            raise ContractOpsError(
                code="authorization_denied",
                message="the approval task is outside the caller's data scope",
                status_code=403,
            )
        if require_assignment and assigned_to != actor.user_id:
            raise ContractOpsError(
                code="authorization_denied",
                message="the approval task is not assigned to the caller",
                status_code=403,
            )
        if assigned_to not in {None, actor.user_id}:
            raise ContractOpsError(
                code="authorization_denied",
                message="the approval task is assigned to another user",
                status_code=403,
            )
        if assigned_to is None and required_role not in actor.roles and not privileged:
            raise ContractOpsError(
                code="authorization_denied",
                message="the caller does not have the role required by this step",
                status_code=403,
            )

    @staticmethod
    def _conflict() -> NoReturn:
        raise ContractOpsError(
            code="approval_step_conflict",
            message="the approval step changed; refresh it before trying again",
            status_code=409,
        )

    def _step_replay(
        self,
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> ApprovalStep | None:
        self._lock_idempotency(connection, actor, operation, idempotency_key)
        replay_id = self._replay_resource_id(
            connection, operation, idempotency_key, request_hash
        )
        return None if replay_id is None else self._load_step(connection, replay_id)

    def _load_policy(
        self, connection: Connection, policy_id: UUID
    ) -> ApprovalPolicy | None:
        row = (
            connection.execute(
                text("SELECT * FROM approval_policies WHERE id = :id"), {"id": policy_id}
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._policy_from_row(row)

    def _load_instance(
        self, connection: Connection, instance_id: UUID
    ) -> ApprovalInstance | None:
        row = (
            connection.execute(
                text("SELECT * FROM approval_instances WHERE id = :id"),
                {"id": instance_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        step_rows = (
            connection.execute(
                text(
                    """
                    SELECT * FROM approval_workflow_steps
                    WHERE approval_instance_id = :id ORDER BY step_order
                    """
                ),
                {"id": instance_id},
            )
            .mappings()
            .all()
        )
        return ApprovalInstance(
            id=cast(UUID, row["id"]),
            tenant_id=cast(UUID, row["tenant_id"]),
            contract_id=cast(UUID, row["contract_id"]),
            contract_version_id=cast(UUID, row["contract_version_id"]),
            policy_id=cast(UUID, row["policy_id"]),
            policy_snapshot=cast(dict[str, Any], row["policy_snapshot"]),
            status=ApprovalInstanceStatus(cast(str, row["status"])),
            state_version=cast(int, row["state_version"]),
            requested_by=cast(UUID, row["requested_by"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
            completed_at=cast(datetime | None, row["completed_at"]),
            steps=tuple(self._step_from_row(item) for item in step_rows),
        )

    def _load_step(self, connection: Connection, step_id: UUID) -> ApprovalStep | None:
        row = (
            connection.execute(
                text("SELECT * FROM approval_workflow_steps WHERE id = :id"),
                {"id": step_id},
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._step_from_row(row)

    @staticmethod
    def _policy_from_row(row: RowMapping) -> ApprovalPolicy:
        condition_data = cast(dict[str, Any], row["condition"])
        step_data = cast(list[dict[str, Any]], row["steps"])
        condition = PolicyCondition(
            contract_types=tuple(condition_data.get("contract_types") or ()),
            department_ids=tuple(
                UUID(value) for value in condition_data.get("department_ids") or ()
            ),
            minimum_amount=(
                Decimal(condition_data["minimum_amount"])
                if condition_data.get("minimum_amount") is not None
                else None
            ),
            maximum_amount=(
                Decimal(condition_data["maximum_amount"])
                if condition_data.get("maximum_amount") is not None
                else None
            ),
            currency=condition_data.get("currency"),
        )
        steps = tuple(
            ApprovalStepTemplate(
                step_order=int(item["step_order"]),
                name=str(item["name"]),
                required_role=Role(str(item["required_role"])),
                minimum_amount=(
                    Decimal(item["minimum_amount"])
                    if item.get("minimum_amount") is not None
                    else None
                ),
            )
            for item in step_data
        )
        return ApprovalPolicy(
            id=cast(UUID, row["id"]),
            tenant_id=cast(UUID, row["tenant_id"]),
            policy_key=cast(str, row["policy_key"]),
            version_number=cast(int, row["version_number"]),
            name=cast(str, row["name"]),
            priority=cast(int, row["priority"]),
            status=PolicyStatus(cast(str, row["status"])),
            condition=condition,
            steps=steps,
            created_by=cast(UUID, row["created_by"]),
            created_at=cast(datetime, row["created_at"]),
            published_by=cast(UUID | None, row["published_by"]),
            published_at=cast(datetime | None, row["published_at"]),
        )

    @staticmethod
    def _step_from_row(row: RowMapping) -> ApprovalStep:
        return ApprovalStep(
            id=cast(UUID, row["id"]),
            instance_id=cast(UUID, row["approval_instance_id"]),
            step_order=cast(int, row["step_order"]),
            name=cast(str, row["name"]),
            required_role=Role(cast(str, row["required_role"])),
            status=ApprovalStepStatus(cast(str, row["status"])),
            assigned_to=cast(UUID | None, row["assigned_to"]),
            state_version=cast(int, row["state_version"]),
            decided_by=cast(UUID | None, row["decided_by"]),
            decision_comment=cast(str | None, row["decision_comment"]),
            decided_at=cast(datetime | None, row["decided_at"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    @staticmethod
    def _contract_from_row(row: RowMapping) -> Contract:
        return Contract(
            id=cast(UUID, row["id"]),
            tenant_id=cast(UUID, row["tenant_id"]),
            contract_number=cast(str | None, row["contract_number"]),
            title=cast(str, row["title"]),
            contract_type=cast(str, row["contract_type"]),
            counterparty_name=cast(str | None, row["counterparty_name"]),
            department_id=cast(UUID, row["department_id"]),
            amount=cast(Decimal | None, row["amount"]),
            currency=cast(str | None, row["currency"]),
            valid_from=cast(Any, row["valid_from"]),
            valid_until=cast(Any, row["valid_until"]),
            status=ContractStatus(cast(str, row["status"])),
            current_version_id=cast(UUID | None, row["current_version_id"]),
            state_version=cast(int, row["state_version"]),
            created_by=cast(UUID, row["created_by"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    @staticmethod
    def _lock_idempotency(
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
    ) -> None:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:value, 0))"),
            {"value": f"{actor.tenant_id}:{operation}:{idempotency_key}"},
        )

    @staticmethod
    def _replay_resource_id(
        connection: Connection,
        operation: str,
        idempotency_key: str,
        request_hash: str,
    ) -> UUID | None:
        row = (
            connection.execute(
                text(
                    """
                    SELECT request_hash, resource_id FROM idempotency_records
                    WHERE operation = :operation AND idempotency_key = :idempotency_key
                    """
                ),
                {"operation": operation, "idempotency_key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ContractOpsError(
                code="idempotency_key_reused",
                message="Idempotency-Key was already used with a different request",
                status_code=409,
            )
        return cast(UUID, row["resource_id"])

    @staticmethod
    def _record_idempotency(
        connection: Connection,
        actor: ActorContext,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        resource_id: UUID,
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    tenant_id, operation, idempotency_key, request_hash,
                    response_status, response_body, resource_id, expires_at
                ) VALUES (
                    :tenant_id, :operation, :idempotency_key, :request_hash,
                    200, CAST(:body AS jsonb), :resource_id, :expires_at
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "body": json.dumps({"resource_id": str(resource_id)}),
                "resource_id": resource_id,
                "expires_at": datetime.now(UTC) + timedelta(hours=24),
            },
        )

    @staticmethod
    def _emit_event(
        connection: Connection,
        actor: ActorContext,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        payload: dict[str, str],
    ) -> None:
        connection.execute(
            text(
                """
                INSERT INTO outbox_events (
                    tenant_id, event_type, aggregate_type, aggregate_id, payload
                ) VALUES (
                    :tenant_id, :event_type, :aggregate_type, :aggregate_id,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "payload": json.dumps(payload),
            },
        )
