"""Persistent SQLite adapters for the existing contract/approval application services.

Only constructed in explicit development/test local mode; no PostgreSQL fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Uuid,
    and_,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection

from contractops.application.approvals import (
    CreateApprovalPolicyCommand,
    StepActionCommand,
    TransferStepCommand,
)
from contractops.application.contracts import AddContractVersionCommand, CreateContractCommand
from contractops.application.task_pagination import TaskCursor
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
from contractops.domain.contract import (
    Contract,
    ContractStatus,
    ContractVersion,
    ContractVersionStatus,
    transition_contract,
)
from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres.database import Database

local_metadata = MetaData()
records = Table(
    "local_business_records",
    local_metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("kind", String(30), nullable=False),
    Column("owner_id", Uuid, nullable=False),
    Column("department_id", Uuid, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
replays = Table(
    "local_business_idempotency",
    local_metadata,
    Column("tenant_id", Uuid, primary_key=True),
    Column("user_id", Uuid, primary_key=True),
    Column("operation", String(200), primary_key=True),
    Column("key", String(200), primary_key=True),
    Column("request_hash", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)
operations = Table(
    "local_business_operations",
    local_metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("actor_id", Uuid, nullable=False),
    Column("action", String(200), nullable=False),
    Column("resource_id", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
T = TypeVar("T", Contract, ContractVersion, ApprovalPolicy, ApprovalInstance, ApprovalStep)


def conflict(message: str = "状态已变化，请刷新后重试。") -> ContractOpsError:
    return ContractOpsError(code="business_state_conflict", message=message, status_code=409)


class SQLiteBusiness:
    def __init__(self, database: Database) -> None:
        self.database = database
        local_metadata.create_all(database._engine)

    @contextmanager
    def transaction(self, actor: ActorContext) -> Iterator[Connection]:
        with self.database.transaction(actor) as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def scope(actor: ActorContext) -> Any:
        return and_(
            records.c.tenant_id == actor.tenant_id,
            or_(
                literal(actor.data_scope is DataScope.TENANT),
                records.c.owner_id == actor.user_id,
                and_(
                    literal(actor.data_scope is DataScope.DEPARTMENT),
                    records.c.department_id.in_(actor.department_ids),
                ),
            ),
        )

    def load(
        self, connection: Connection, actor: ActorContext, kind: str, identifier: UUID, cls: type[T]
    ) -> T | None:
        value = connection.execute(
            select(records.c.payload).where(
                self.scope(actor),
                records.c.kind == kind,
                records.c.id == identifier,
            )
        ).scalar_one_or_none()
        return None if value is None else TypeAdapter(cls).validate_python(value)

    def save(
        self,
        connection: Connection,
        actor: ActorContext,
        kind: str,
        value: Any,
        department: UUID,
        owner: UUID,
    ) -> None:
        payload = TypeAdapter(type(value)).dump_python(value, mode="json")
        existing = connection.execute(
            select(records.c.id).where(
                records.c.tenant_id == actor.tenant_id,
                records.c.id == value.id,
            )
        ).first()
        data = dict(
            kind=kind,
            owner_id=owner,
            department_id=department,
            payload=payload,
            updated_at=datetime.now(UTC),
        )
        if existing:
            connection.execute(
                update(records)
                .where(records.c.id == value.id, records.c.tenant_id == actor.tenant_id)
                .values(**data)
            )
        else:
            count = connection.execute(
                select(records.c.id).where(records.c.tenant_id == actor.tenant_id).limit(1001)
            ).all()
            if len(count) >= 1000:
                raise conflict("本地 MVP 每租户最多1000个业务记录。")
            connection.execute(
                insert(records).values(id=value.id, tenant_id=actor.tenant_id, **data)
            )

    def run(
        self,
        actor: ActorContext,
        operation: str,
        key: str,
        digest: str,
        cls: type[T],
        action: Callable[[Connection], T],
    ) -> tuple[T, bool]:
        with self.transaction(actor) as connection:
            predicate = and_(
                replays.c.tenant_id == actor.tenant_id,
                replays.c.user_id == actor.user_id,
                replays.c.operation == operation,
                replays.c.key == key,
            )
            replay = connection.execute(select(replays).where(predicate)).mappings().one_or_none()
            if replay:
                if replay["request_hash"] != digest:
                    raise conflict("同一幂等键不能用于不同请求。")
                return TypeAdapter(cls).validate_python(replay["payload"]), True
            value = action(connection)
            connection.execute(
                insert(replays).values(
                    tenant_id=actor.tenant_id,
                    user_id=actor.user_id,
                    operation=operation,
                    key=key,
                    request_hash=digest,
                    payload=TypeAdapter(cls).dump_python(value, mode="json"),
                )
            )
            connection.execute(
                insert(operations).values(
                    id=uuid4(),
                    tenant_id=actor.tenant_id,
                    actor_id=actor.user_id,
                    action=operation,
                    resource_id=value.id,
                    created_at=datetime.now(UTC),
                )
            )
            return value, False

    def create_contract(
        self,
        actor: ActorContext,
        command: CreateContractCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Contract, bool]:
        def action(connection: Connection) -> Contract:
            if command.contract_number:
                for payload in self.payloads(connection, actor, "contract", scoped=False):
                    if payload["contract_number"] == command.contract_number:
                        raise conflict("合同编号已存在。")
            now = datetime.now(UTC)
            from dataclasses import asdict

            value = Contract(
                id=uuid4(),
                tenant_id=actor.tenant_id,
                **asdict(command),
                status=ContractStatus.DRAFT,
                current_version_id=None,
                state_version=0,
                created_by=actor.user_id,
                created_at=now,
                updated_at=now,
            )
            self.save(connection, actor, "contract", value, value.department_id, value.created_by)
            return value

        return self.run(actor, "contract.create", idempotency_key, request_hash, Contract, action)

    def get_contract(self, actor: ActorContext, contract_id: UUID) -> Contract | None:
        with self.database.transaction(actor) as connection:
            return self.load(connection, actor, "contract", contract_id, Contract)

    def add_version(
        self,
        actor: ActorContext,
        contract_id: UUID,
        command: AddContractVersionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ContractVersion, bool]:
        def action(connection: Connection) -> ContractVersion:
            contract = self.load(connection, actor, "contract", contract_id, Contract)
            if contract is None:
                raise ContractOpsError(
                    code="contract_not_found", message="合同不存在或不可访问。", status_code=404
                )
            return self.version_in(
                connection, actor, contract, command, ContractVersionStatus.UPLOADING
            )

        return self.run(
            actor,
            f"contract.version:{contract_id}",
            idempotency_key,
            request_hash,
            ContractVersion,
            action,
        )

    def version_in(
        self,
        connection: Connection,
        actor: ActorContext,
        contract: Contract,
        command: AddContractVersionCommand,
        status: ContractVersionStatus,
    ) -> ContractVersion:
        if contract.status not in {
            ContractStatus.DRAFT,
            ContractStatus.CHANGES_REQUESTED,
            ContractStatus.REJECTED,
        }:
            raise conflict("只有可修订合同才能关联合同文件。")
        if any(version.content_hash == command.content_hash for version in contract.versions):
            raise conflict("该合同已有相同内容的版本。")
        now, identifier = datetime.now(UTC), uuid4()
        version = ContractVersion(
            identifier,
            contract.id,
            len(contract.versions) + 1,
            status,
            f"knowledge-linked/{identifier}",
            command.file_name,
            command.media_type,
            command.size_bytes,
            command.content_hash,
            actor.user_id,
            now,
        )
        current_status = (
            contract.status
            if contract.status is ContractStatus.DRAFT
            else transition_contract(contract.status, ContractStatus.DRAFT)
        )
        updated = replace(
            contract,
            current_version_id=version.id,
            state_version=contract.state_version + 1,
            status=current_status,
            versions=(*contract.versions, version),
            updated_at=now,
        )
        self.save(
            connection, actor, "contract", updated, contract.department_id, contract.created_by
        )
        return version

    def create_policy(
        self,
        actor: ActorContext,
        command: CreateApprovalPolicyCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalPolicy, bool]:
        def action(connection: Connection) -> ApprovalPolicy:
            previous = [
                TypeAdapter(ApprovalPolicy).validate_python(payload)
                for payload in self.payloads(connection, actor, "policy", scoped=False)
            ]
            same = [policy for policy in previous if policy.policy_key == command.policy_key]
            if any(policy.status is PolicyStatus.DRAFT for policy in same):
                raise conflict("该策略已有未发布草稿。")
            value = ApprovalPolicy(
                uuid4(),
                actor.tenant_id,
                command.policy_key,
                max((p.version_number for p in same), default=0) + 1,
                command.name,
                command.priority,
                PolicyStatus.DRAFT,
                command.condition,
                command.steps,
                actor.user_id,
                datetime.now(UTC),
            )
            self.save(
                connection,
                actor,
                "policy",
                value,
                next(iter(actor.department_ids), UUID(int=3)),
                actor.user_id,
            )
            return value

        return self.run(
            actor, "policy.create", idempotency_key, request_hash, ApprovalPolicy, action
        )

    def publish_policy(self, actor: ActorContext, policy_id: UUID) -> ApprovalPolicy:
        with self.transaction(actor) as connection:
            value = self.load(connection, actor, "policy", policy_id, ApprovalPolicy)
            if value is None:
                raise ContractOpsError(
                    code="policy_not_found", message="审批策略不存在。", status_code=404
                )
            if value.status is PolicyStatus.PUBLISHED:
                return value
            value = replace(
                value,
                status=PolicyStatus.PUBLISHED,
                published_by=actor.user_id,
                published_at=datetime.now(UTC),
            )
            self.save(
                connection,
                actor,
                "policy",
                value,
                next(iter(actor.department_ids), UUID(int=3)),
                value.created_by,
            )
            return value

    def ensure_default_policy(self, actor: ActorContext) -> None:
        # Real configuration, not fabricated contracts/tasks. Published once via the same adapter.
        command = CreateApprovalPolicyCommand(
            "local-human-review",
            "本地人工法务核查",
            0,
            PolicyCondition(),
            (ApprovalStepTemplate(1, "人工法务核查", Role.LEGAL_ADMIN),),
        )
        policy, _ = self.create_policy(
            actor, command, idempotency_key="local-policy-v1", request_hash="local-policy-v1"
        )
        self.publish_policy(actor, policy.id)

    def submit_contract(
        self, actor: ActorContext, contract_id: UUID, *, idempotency_key: str, request_hash: str
    ) -> tuple[ApprovalInstance, bool]:
        def action(connection: Connection) -> ApprovalInstance:
            contract = self.load(connection, actor, "contract", contract_id, Contract)
            if contract is None or contract.status is not ContractStatus.DRAFT:
                raise conflict("只有草稿合同可提交审批。")
            version = next(
                (v for v in contract.versions if v.id == contract.current_version_id), None
            )
            if version is None or version.status is not ContractVersionStatus.READY:
                raise conflict("请先关联已解析完成的合同文档。")
            policies = [
                TypeAdapter(ApprovalPolicy).validate_python(p)
                for p in self.payloads(connection, actor, "policy", scoped=False)
            ]
            latest: dict[str, ApprovalPolicy] = {}
            for policy in policies:
                previous = latest.get(policy.policy_key)
                if policy.status is PolicyStatus.PUBLISHED and (
                    previous is None or policy.version_number > previous.version_number
                ):
                    latest[policy.policy_key] = policy
            matches = sorted(
                (p for p in latest.values() if p.condition.matches(contract)),
                key=lambda p: (-p.priority, p.policy_key),
            )
            if not matches:
                raise conflict("没有匹配的已发布审批策略。")
            policy, now, identifier = matches[0], datetime.now(UTC), uuid4()
            statuses = initial_step_statuses(contract, policy.steps)
            if ApprovalStepStatus.READY not in statuses:
                raise conflict("审批策略没有可执行步骤。")
            steps = tuple(
                ApprovalStep(
                    uuid4(),
                    identifier,
                    template.step_order,
                    template.name,
                    template.required_role,
                    status,
                    None,
                    0,
                    None,
                    None,
                    None,
                    now,
                    now,
                )
                for template, status in zip(policy.steps, statuses, strict=True)
            )
            value = ApprovalInstance(
                identifier,
                actor.tenant_id,
                contract.id,
                version.id,
                policy.id,
                policy.snapshot(),
                ApprovalInstanceStatus.IN_PROGRESS,
                0,
                actor.user_id,
                now,
                now,
                None,
                steps,
            )
            self.save(
                connection, actor, "instance", value, contract.department_id, contract.created_by
            )
            status = transition_contract(
                transition_contract(contract.status, ContractStatus.SUBMITTED),
                ContractStatus.IN_APPROVAL,
            )
            self.save(
                connection,
                actor,
                "contract",
                replace(
                    contract,
                    status=status,
                    state_version=contract.state_version + 2,
                    updated_at=now,
                ),
                contract.department_id,
                contract.created_by,
            )
            return value

        return self.run(
            actor,
            f"approval.submit:{contract_id}",
            idempotency_key,
            request_hash,
            ApprovalInstance,
            action,
        )

    def get_instance(self, actor: ActorContext, instance_id: UUID) -> ApprovalInstance | None:
        with self.database.transaction(actor) as connection:
            return self.load(connection, actor, "instance", instance_id, ApprovalInstance)

    def instances(self, connection: Connection, actor: ActorContext) -> list[ApprovalInstance]:
        return [
            TypeAdapter(ApprovalInstance).validate_python(payload)
            for payload in self.payloads(connection, actor, "instance")
        ]

    def payloads(
        self, connection: Connection, actor: ActorContext, kind: str, *, scoped: bool = True
    ) -> list[Any]:
        statement = select(records.c.payload).where(
            records.c.tenant_id == actor.tenant_id, records.c.kind == kind
        )
        if scoped:
            statement = statement.where(self.scope(actor))
        values: list[Any] = list(connection.execute(statement).scalars().all())
        return values

    @staticmethod
    def eligible(actor: ActorContext, step: ApprovalStep) -> bool:
        return actor.has_any_role(step.required_role, Role.TENANT_ADMIN)

    def list_tasks(
        self, actor: ActorContext, *, limit: int = 50, after: TaskCursor | None = None
    ) -> tuple[ApprovalStep, ...]:
        with self.database.transaction(actor) as connection:
            steps = [
                s
                for instance in self.instances(connection, actor)
                for s in instance.steps
                if instance.status is ApprovalInstanceStatus.IN_PROGRESS
                and s.status in {ApprovalStepStatus.READY, ApprovalStepStatus.CLAIMED}
                and self.eligible(actor, s)
                and s.assigned_to in {None, actor.user_id}
            ]
        ordered = sorted(steps, key=lambda s: (s.created_at, s.id))
        return tuple(
            s
            for s in ordered
            if after is None or (s.created_at, s.id) > (after.created_at, after.id)
        )[:limit]

    def step_action(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: StepActionCommand,
        decision: ApprovalDecision | None,
        key: str,
        digest: str,
    ) -> tuple[ApprovalStep, bool]:
        def action(connection: Connection) -> ApprovalStep:
            found = [
                (instance, step)
                for instance in self.instances(connection, actor)
                for step in instance.steps
                if step.id == step_id
            ]
            if not found:
                raise ContractOpsError(
                    code="approval_step_not_found",
                    message="审批步骤不存在或不可访问。",
                    status_code=404,
                )
            instance, step = found[0]
            contract = self.load(connection, actor, "contract", instance.contract_id, Contract)
            if contract is None:
                raise conflict()
            if not self.eligible(actor, step):
                raise ContractOpsError(
                    code="authorization_denied", message="当前角色不能处理此步骤。", status_code=403
                )
            if (
                instance.status is not ApprovalInstanceStatus.IN_PROGRESS
                or step.state_version != command.expected_version
            ):
                raise conflict()
            if decision is None:
                if step.status is not ApprovalStepStatus.READY or step.assigned_to is not None:
                    raise conflict("该步骤不能领取。")
                updated = replace(
                    step,
                    assigned_to=actor.user_id,
                    status=ApprovalStepStatus.CLAIMED,
                    state_version=step.state_version + 1,
                    updated_at=datetime.now(UTC),
                )
            else:
                if (
                    step.status is not ApprovalStepStatus.CLAIMED
                    or step.assigned_to != actor.user_id
                ):
                    raise conflict("须先领取审批任务，再作决定。")
                status = {
                    ApprovalDecision.APPROVE: ApprovalStepStatus.APPROVED,
                    ApprovalDecision.REJECT: ApprovalStepStatus.REJECTED,
                    ApprovalDecision.REQUEST_CHANGES: ApprovalStepStatus.CHANGES_REQUESTED,
                }[decision]
                updated = replace(
                    step,
                    status=status,
                    state_version=step.state_version + 1,
                    decided_by=actor.user_id,
                    decision_comment=command.comment,
                    decided_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            steps = [updated if s.id == step.id else s for s in instance.steps]
            instance_status: ApprovalInstanceStatus = instance.status
            if decision is ApprovalDecision.APPROVE:
                pending = next((s for s in steps if s.status is ApprovalStepStatus.PENDING), None)
                if pending:
                    steps = [
                        replace(
                            s, status=ApprovalStepStatus.READY, state_version=s.state_version + 1
                        )
                        if s.id == pending.id
                        else s
                        for s in steps
                    ]
                else:
                    instance_status = ApprovalInstanceStatus.APPROVED
            elif decision is not None:
                instance_status = (
                    ApprovalInstanceStatus.REJECTED
                    if decision is ApprovalDecision.REJECT
                    else ApprovalInstanceStatus.CHANGES_REQUESTED
                )
                steps = [
                    replace(s, status=ApprovalStepStatus.SKIPPED, state_version=s.state_version + 1)
                    if s.status is ApprovalStepStatus.PENDING
                    else s
                    for s in steps
                ]
            terminal = instance_status is not ApprovalInstanceStatus.IN_PROGRESS
            now = datetime.now(UTC)
            value = replace(
                instance,
                steps=tuple(steps),
                status=instance_status,
                state_version=instance.state_version + 1,
                updated_at=now,
                completed_at=now if terminal else None,
            )
            self.save(
                connection, actor, "instance", value, contract.department_id, contract.created_by
            )
            if terminal:
                target = ContractStatus(instance_status.value)
                self.save(
                    connection,
                    actor,
                    "contract",
                    replace(
                        contract,
                        status=transition_contract(contract.status, target),
                        state_version=contract.state_version + 1,
                        updated_at=now,
                    ),
                    contract.department_id,
                    contract.created_by,
                )
            return updated

        operation = f"approval.{'claim' if decision is None else 'decide'}:{step_id}"
        return self.run(actor, operation, key, digest, ApprovalStep, action)

    def claim_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: StepActionCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]:
        return self.step_action(actor, step_id, command, None, idempotency_key, request_hash)

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
        return self.step_action(actor, step_id, command, decision, idempotency_key, request_hash)

    def transfer_step(
        self,
        actor: ActorContext,
        step_id: UUID,
        command: TransferStepCommand,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[ApprovalStep, bool]:
        raise ContractOpsError(
            code="local_transfer_unsupported",
            message="本地 MVP 未开放转交，正式环境使用现有审批转交服务。",
            status_code=409,
        )
