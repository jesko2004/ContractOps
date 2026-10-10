"""Knowledge-linked contract workspace; no model can call a write method."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text, update

from contractops.application.approvals import ApprovalService
from contractops.application.contracts import AddContractVersionCommand, ContractService
from contractops.application.knowledge import KnowledgeCitation, KnowledgeHit, KnowledgeService
from contractops.application.knowledge_management import KnowledgeManagementService, ManagedDocument
from contractops.context import ActorContext, DataScope, Role
from contractops.domain.approval import ApprovalDecision
from contractops.domain.contract import Contract, ContractStatus, ContractVersionStatus
from contractops.errors import ContractOpsError
from contractops.infrastructure.document_parsing import ParsedBlock, extract_rule_findings
from contractops.infrastructure.knowledge_schema import documents
from contractops.infrastructure.knowledge_store import KnowledgeStore, _utc
from contractops.infrastructure.local_business import SQLiteBusiness, conflict, records
from contractops.infrastructure.workbench_schema import links, reports


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


class ContractWorkbench:
    def __init__(
        self,
        store: KnowledgeStore,
        management: KnowledgeManagementService,
        knowledge: KnowledgeService,
        contracts: ContractService,
        approvals: ApprovalService,
        local: SQLiteBusiness | None = None,
    ) -> None:
        self.store, self.management, self.knowledge = store, management, knowledge
        self.contracts, self.approvals, self.local = contracts, approvals, local
        self.approvals.submission_guard = self.validate_submission
        self.approvals.decision_guard = self.validate_decision

    def validate_submission(self, actor: ActorContext, contract_id: UUID) -> None:
        if self.link(actor, contract_id) is None:
            return
        self.require_current_report(actor, contract_id)

    def require_current_report(
        self, actor: ActorContext, contract_id: UUID, version_id: UUID | None = None
    ) -> None:
        try:
            current = self.report(actor, contract_id)
        except ContractOpsError as error:
            if error.status_code == 404:
                raise conflict("预审报告缺失或当前不可访问，请核对资料并重新生成。") from None
            raise
        if current["status"] != "CURRENT" or (
            version_id and current["contract_version_id"] != str(version_id)
        ):
            raise conflict("预审报告已失效；更新报告后才能通过。仍可驳回或退回修改。")

    def validate_decision(
        self, actor: ActorContext, step_id: UUID, decision: ApprovalDecision
    ) -> None:
        if decision is not ApprovalDecision.APPROVE:
            return
        if self.local:
            with self.store._transaction(actor) as connection:
                instance = next(
                    (
                        i
                        for i in self.local.instances(connection, actor)
                        if any(s.id == step_id for s in i.steps)
                    ),
                    None,
                )
        else:
            with self.store._transaction(actor) as connection:
                identifier: Any = connection.execute(
                    text(
                        "SELECT approval_instance_id FROM approval_workflow_steps "
                        "WHERE id=:id AND tenant_id=:tenant"
                    ),
                    {"id": step_id, "tenant": actor.tenant_id},
                ).scalar_one_or_none()
            instance = self.approvals.get_instance(actor, identifier) if identifier else None
        # A settled step may only replay its original result or conflict in the workflow.
        # Re-checking newly changed policy evidence must not invalidate that saved replay.
        step = next((s for s in instance.steps if s.id == step_id), None) if instance else None
        if step and step.decided_at is not None:
            return
        if instance and self.link(actor, instance.contract_id):
            self.require_current_report(actor, instance.contract_id, instance.contract_version_id)

    def require_writer(self, actor: ActorContext, contract_id: UUID) -> Contract:
        self.management.require_writer(actor)
        value = self.contracts.get_contract(actor, contract_id)
        if value.created_by != actor.user_id and not actor.has_any_role(Role.TENANT_ADMIN):
            raise ContractOpsError(
                code="authorization_denied",
                message="只有经办人或有权限的租户管理员可修改合同。",
                status_code=403,
            )
        return value

    def list_contracts(
        self, actor: ActorContext, limit: int = 30, offset: int = 0
    ) -> tuple[Contract, ...]:
        self.knowledge._require_reader(actor)
        identifiers: Any
        with self.store._transaction(actor) as connection:
            if self.local:
                identifiers = (
                    connection.execute(
                        select(records.c.id)
                        .where(
                            self.local.scope(actor),
                            records.c.kind == "contract",
                        )
                        .order_by(records.c.updated_at.desc(), records.c.id)
                        .limit(limit)
                        .offset(offset)
                    )
                    .scalars()
                    .all()
                )
            else:
                identifiers = (
                    connection.execute(
                        text("""
                    SELECT id FROM contracts WHERE tenant_id=:tenant AND
                    (:tenant_scope OR created_by=:user OR
                     (:department_scope AND department_id=ANY(:departments)))
                    ORDER BY updated_at DESC,id LIMIT :limit OFFSET :offset
                """),
                        {
                            "tenant": actor.tenant_id,
                            "user": actor.user_id,
                            "tenant_scope": actor.data_scope is DataScope.TENANT,
                            "department_scope": actor.data_scope is DataScope.DEPARTMENT,
                            "departments": list(actor.department_ids),
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                    .scalars()
                    .all()
                )
        return tuple(
            self.contracts.get_contract(actor, UUID(str(identifier))) for identifier in identifiers
        )

    def link(
        self, actor: ActorContext, contract_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any] | None:
        contract = self.contracts.get_contract(actor, contract_id)
        with self.store._transaction(actor) as connection:
            row = (
                connection.execute(
                    select(links).where(
                        links.c.tenant_id == actor.tenant_id,
                        links.c.contract_id == contract_id,
                        links.c.contract_version_id == (version_id or contract.current_version_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row:
            self.management.repository.get_document(actor, row["document_id"])
        return None if row is None else dict(row)

    def bind_document(
        self,
        actor: ActorContext,
        contract_id: UUID,
        document_id: UUID,
        expected_version: int,
        request_key: str,
    ) -> dict[str, Any]:
        contract = self.require_writer(actor, contract_id)
        self.management.require_reader(actor)
        request_hash = digest([contract_id, document_id, expected_version])
        with self.store._transaction(actor) as connection:
            if self.local:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            existing = (
                connection.execute(
                    select(links).where(
                        links.c.tenant_id == actor.tenant_id,
                        links.c.contract_id == contract_id,
                        links.c.request_key == request_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing:
                if existing["request_hash"] != request_hash:
                    raise conflict("同一请求键不能关联不同文件。")
                self.management.repository.get_document(actor, existing["document_id"])
                return dict(existing) | {"created_at": _utc(existing["created_at"])}
            row = (
                connection.execute(
                    self.store._document_select(actor)
                    .where(documents.c.id == document_id)
                    .with_for_update(of=documents)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ContractOpsError(
                    code="knowledge_not_found", message="文档不存在或不可访问。", status_code=404
                )
            document = self.store._document(row)
            if (
                not document.is_current
                or document.status != "READY"
                or not document.chunk_count
                or document.document_type != "CONTRACT"
            ):
                raise conflict(
                    "请关联当前、已解析且有正文的合同资料，不可使用制度、历史或归档文件。"
                )
            if self.local:
                contract = cast(
                    Contract, self.local.load(connection, actor, "contract", contract_id, Contract)
                )
                if contract.state_version != expected_version:
                    raise conflict()
                command = AddContractVersionCommand(
                    document.file_name, document.media_type, document.size_bytes, document.file_hash
                )
                version = self.local.version_in(
                    connection, actor, contract, command, ContractVersionStatus.READY
                )
                identifier = version.id
            else:
                current = (
                    connection.execute(
                        text(
                            "SELECT state_version,status FROM contracts "
                            "WHERE id=:id AND tenant_id=:tenant FOR UPDATE"
                        ),
                        {"id": contract_id, "tenant": actor.tenant_id},
                    )
                    .mappings()
                    .one()
                )
                if current["state_version"] != expected_version or current["status"] not in {
                    "DRAFT",
                    "CHANGES_REQUESTED",
                    "REJECTED",
                }:
                    raise conflict()
                identifier = uuid4()
                duplicate = connection.execute(
                    text(
                        "SELECT id FROM contract_versions WHERE contract_id=:contract "
                        "AND tenant_id=:tenant AND content_hash=:hash"
                    ),
                    {
                        "contract": contract_id,
                        "tenant": actor.tenant_id,
                        "hash": document.file_hash,
                    },
                ).first()
                if duplicate:
                    raise conflict("该合同已有相同内容的版本。")
                connection.execute(
                    text("""
                    INSERT INTO contract_versions(id,tenant_id,contract_id,version_number,status,
                        object_key,file_name,media_type,size_bytes,content_hash,created_by)
                    VALUES(:id,:tenant,:contract,(SELECT COALESCE(MAX(version_number),0)+1
                        FROM contract_versions WHERE contract_id=:contract AND tenant_id=:tenant),
                        'READY',:reference,:name,:media,:size,:hash,:user)
                """),
                    {
                        "id": identifier,
                        "tenant": actor.tenant_id,
                        "contract": contract_id,
                        "reference": f"knowledge-linked/{document.id}",
                        "name": document.file_name,
                        "media": document.media_type,
                        "size": document.size_bytes,
                        "hash": document.file_hash,
                        "user": actor.user_id,
                    },
                )
                connection.execute(
                    text(
                        "UPDATE contracts SET current_version_id=:version,status='DRAFT',"
                        "state_version=state_version+1,updated_at=now() "
                        "WHERE id=:id AND tenant_id=:tenant"
                    ),
                    {"version": identifier, "id": contract_id, "tenant": actor.tenant_id},
                )
            value = dict(
                contract_version_id=identifier,
                tenant_id=actor.tenant_id,
                contract_id=contract_id,
                document_id=document.id,
                request_key=request_key,
                request_hash=request_hash,
                confirmation={},
                created_by=actor.user_id,
                created_at=datetime.now(UTC),
            )
            connection.execute(insert(links).values(**value))
        return value

    def source(
        self, actor: ActorContext, contract_id: UUID
    ) -> tuple[Contract, dict[str, Any], ManagedDocument]:
        contract = self.contracts.get_contract(actor, contract_id)
        link = self.link(actor, contract_id)
        if link is None:
            raise conflict("当前合同未关联知识库文档，请先关联已解析的合同资料。")
        document = self.management.repository.get_document(actor, link["document_id"])
        return contract, link, document

    def blocks(self, actor: ActorContext, document_id: UUID) -> list[Any]:
        result: list[Any] = []
        for offset in range(0, 2000, 100):
            page = self.management.document_chunks(actor, document_id, limit=100, offset=offset)
            result.extend(page)
            if len(page) < 100:
                break
        return result

    @staticmethod
    def citation(document: ManagedDocument, block: Any, quote: str | None = None) -> dict[str, Any]:
        excerpt = quote or block.content[:900]
        start = block.content.find(excerpt)
        return dict(
            document_id=str(document.id),
            base_id=str(document.base_id),
            version_id=str(document.version_id),
            version_number=document.version_number,
            file_name=document.file_name,
            file_hash=document.file_hash,
            chunk_id=str(block.id),
            sequence=block.sequence,
            page_number=block.page_number,
            heading_path=list(block.heading_path),
            content_hash=block.content_hash,
            source_pages=list(block.source_pages),
            excerpt=excerpt,
            excerpt_start=start,
            excerpt_end=start + len(excerpt),
        )

    def candidates(self, actor: ActorContext, contract_id: UUID) -> list[dict[str, Any]]:
        _, _, document = self.source(actor, contract_id)
        result = []
        for block in self.blocks(actor, document.id):
            parsed = ParsedBlock(
                block.sequence, block.page_number, block.heading_path, block.content
            )
            for candidate in extract_rule_findings(parsed):
                result.append(
                    dict(
                        id=digest([block.id, candidate.kind, candidate.source_text]),
                        kind=candidate.kind.value,
                        title={"AMOUNT": "金额候选", "DATE": "日期候选"}.get(
                            candidate.kind.value, candidate.title
                        ),
                        normalized_value=candidate.normalized_value,
                        origin="RULE",
                        source=self.citation(document, block, candidate.source_text),
                    )
                )
                if len(result) >= 50:
                    return result
        return result

    def confirm(
        self, actor: ActorContext, contract_id: UUID, expected_version: int, identifiers: list[str]
    ) -> dict[str, Any]:
        contract = self.require_writer(actor, contract_id)
        _, link, document = self.source(actor, contract_id)
        if (
            not document.is_current
            or document.status != "READY"
            or contract.status
            not in {ContractStatus.DRAFT, ContractStatus.CHANGES_REQUESTED, ContractStatus.REJECTED}
        ):
            raise conflict("只能核对可修订合同的当前可用文件。")
        available = {c["id"] for c in self.candidates(actor, contract_id)}
        if not set(identifiers) <= available:
            raise conflict("候选已变化，请重新核对。")
        old_confirmation = link["confirmation"]
        if (
            old_confirmation.get("candidate_ids") == sorted(set(identifiers))
            and old_confirmation.get("confirmed_by") == str(actor.user_id)
            and old_confirmation.get("file_hash") == document.file_hash
        ):
            return cast(dict[str, Any], old_confirmation)
        value = dict(
            candidate_ids=sorted(set(identifiers)),
            confirmed_by=str(actor.user_id),
            confirmed_at=datetime.now(UTC).isoformat(),
            file_hash=document.file_hash,
        )
        with self.store._transaction(actor) as connection:
            if self.local:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                current = self.local.load(connection, actor, "contract", contract_id, Contract)
                if current is None or current.state_version != expected_version:
                    raise conflict()
                from dataclasses import replace

                self.local.save(
                    connection,
                    actor,
                    "contract",
                    replace(
                        current,
                        state_version=current.state_version + 1,
                        updated_at=datetime.now(UTC),
                    ),
                    current.department_id,
                    current.created_by,
                )
            else:
                current_version: int = connection.execute(
                    text(
                        "SELECT state_version FROM contracts "
                        "WHERE id=:id AND tenant_id=:tenant FOR UPDATE"
                    ),
                    {"id": contract_id, "tenant": actor.tenant_id},
                ).scalar_one()
                if current_version != expected_version:
                    raise conflict()
                connection.execute(
                    text(
                        "UPDATE contracts SET state_version=state_version+1,updated_at=now() "
                        "WHERE id=:id AND tenant_id=:tenant"
                    ),
                    {"id": contract_id, "tenant": actor.tenant_id},
                )
            connection.execute(
                update(links)
                .where(
                    links.c.tenant_id == actor.tenant_id,
                    links.c.contract_version_id == link["contract_version_id"],
                )
                .values(confirmation=value)
            )
        return value

    def policy_documents(self, actor: ActorContext, base_id: UUID) -> tuple[ManagedDocument, ...]:
        return self.management.documents(actor, base_id, document_type="POLICY", limit=100)

    def fingerprint(self, actor: ActorContext, contract_id: UUID, base_id: UUID) -> str:
        contract, link, document = self.source(actor, contract_id)
        policies = self.policy_documents(actor, base_id)
        return digest(
            [
                contract.current_version_id,
                contract.title,
                contract.amount,
                contract.currency,
                contract.counterparty_name,
                contract.valid_from,
                contract.valid_until,
                document.id,
                document.file_hash,
                document.is_current,
                document.status,
                link["confirmation"],
                [(p.id, p.file_hash, p.state_version, p.status) for p in policies],
            ]
        )

    def generate_report(
        self, actor: ActorContext, contract_id: UUID, base_id: UUID
    ) -> dict[str, Any]:
        contract, link, document = self.source(actor, contract_id)
        if not actor.has_any_role(
            Role.CONTRACT_OWNER,
            Role.LEGAL_ADMIN,
            Role.APPROVER,
            Role.TENANT_ADMIN,
            Role.FINANCE_APPROVER,
            Role.BUSINESS_APPROVER,
        ):
            raise ContractOpsError(
                code="authorization_denied", message="当前角色不能生成预审报告。", status_code=403
            )
        if not document.is_current or document.status != "READY":
            raise conflict("合同文件已变为历史、归档或未就绪，请先更新关联。")
        before = self.fingerprint(actor, contract_id, base_id)
        contract_blocks = self.blocks(actor, document.id)
        policies = self.policy_documents(actor, base_id)
        policy_blocks = [
            (policy, block)
            for policy in policies
            if policy.status == "READY"
            for block in self.blocks(actor, policy.id)
        ]
        topics = [
            ("付款与金额", r"付款|支付|预付|金额|payment|amount|deposit"),
            ("交付与期限", r"交付|期限|日期|delivery|date|days"),
            ("违约与终止", r"违约|终止|赔偿|liability|termination|penalty"),
        ]
        pairs, citations = [], []
        for topic, pattern in topics:
            contract_sources = [
                self.citation(document, block)
                for block in contract_blocks
                if re.search(pattern, block.content, re.I)
            ][:2]
            policy_sources = [
                self.citation(policy, block)
                for policy, block in policy_blocks
                if re.search(pattern, block.content, re.I)
            ][:2]
            pairs.append(
                dict(
                    topic=topic,
                    contract_sources=contract_sources,
                    policy_sources=policy_sources,
                    note="关键词对应原文；制度适用性和是否存在差异须人工确认，不是已判定违规。",
                )
            )
            citations.extend(contract_sources + policy_sources)
        unique = {c["chunk_id"]: c for c in citations}
        if not unique and contract_blocks:
            c = self.citation(document, contract_blocks[0])
            unique[c["chunk_id"]] = c
        citations = list(unique.values())[:10]
        checks = []
        if not contract.counterparty_name:
            checks.append("台账未填写交易对方，请人工核实合同主体。")
        if contract.amount is None:
            checks.append("台账未填写金额/币种；这不等于原文没有金额。")
        if not policy_blocks:
            checks.append("所选库没有可用制度正文，不能判断制度要求。")
        if (
            document.parse_details
            and document.parse_details.get("quality_status") == "NEEDS_REVIEW"
        ):
            checks.append("解析结果需要人工检查，请对照原件核实缺失页和表格。")
        elif not document.parse_details or document.parse_details.get("quality_status") != "READY":
            checks.append("解析质量尚未完整评估；请对照原件检查，不能将可检索视为质量通过。")
        candidates = self.candidates(actor, contract_id)
        unchecked = {c["id"] for c in candidates} - set(
            link["confirmation"].get("candidate_ids", [])
        )
        if unchecked:
            checks.append(
                f"尚有 {len(unchecked)} 项金额/日期候选未人工标记核对。候选不自动写入台账。"
            )
        checks.append("请核实制度适用范围、签署权限及未命中的条款；没有提示不代表合同合规。")
        model_text = None
        if self.knowledge.model:
            evidence = tuple(
                KnowledgeCitation(
                    KnowledgeHit(
                        UUID(c["document_id"]),
                        c["file_name"],
                        UUID(c["version_id"]),
                        c["version_number"],
                        c["file_name"],
                        c["file_hash"],
                        UUID(c["chunk_id"]),
                        c["sequence"],
                        c["page_number"],
                        tuple(c["heading_path"]),
                        c["excerpt"],
                        c["content_hash"],
                        1,
                        document_id=UUID(c["document_id"]),
                        base_id=UUID(c["base_id"]),
                        source_pages=tuple(c["source_pages"]),
                    ),
                    c["excerpt"],
                    c["excerpt_start"],
                    c["excerpt_end"],
                )
                for c in citations
            )
            model_text = self.knowledge.model.answer(
                "请依据合同和制度片段整理摘要与待人工核实的语义疑点，不判断审批结论。", evidence, ()
            )
        if self.fingerprint(actor, contract_id, base_id) != before:
            raise conflict("生成期间资料已变化，请重新生成。")
        payload = dict(
            summary=(
                f"合同：{contract.title}；对方：{contract.counterparty_name or '待确认'}；"
                f"金额：{contract.amount if contract.amount is not None else '待确认'} "
                f"{contract.currency or ''}"
            ),
            mode="RULES_WITH_MODEL" if model_text else "RULES_AND_EVIDENCE",
            checks=checks,
            semantic_suggestions=model_text,
            pairs=pairs,
            citations=citations,
            policy_versions=[str(p.id) for p in policies],
            confirmation=link["confirmation"],
            contract_version_id=str(contract.current_version_id),
            disclaimer="辅助预审，不是法律意见、合规证明或自动审批。制度范围由用户选择；模型无业务写权限。",
        )
        identifier, now = uuid4(), datetime.now(UTC)
        with self.store._transaction(actor) as connection:
            connection.execute(
                insert(reports).values(
                    id=identifier,
                    tenant_id=actor.tenant_id,
                    contract_id=contract_id,
                    document_id=document.id,
                    policy_base_id=base_id,
                    fingerprint=before,
                    payload=payload,
                    created_by=actor.user_id,
                    created_at=now,
                )
            )
        return self.report(actor, contract_id, identifier)

    def report(
        self, actor: ActorContext, contract_id: UUID, report_id: UUID | None = None
    ) -> dict[str, Any]:
        self.contracts.get_contract(actor, contract_id)
        with self.store._transaction(actor) as connection:
            statement = select(reports).where(
                reports.c.tenant_id == actor.tenant_id, reports.c.contract_id == contract_id
            )
            if report_id:
                statement = statement.where(reports.c.id == report_id)
            row = (
                connection.execute(
                    statement.order_by(reports.c.created_at.desc(), reports.c.id).limit(1)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ContractOpsError(
                code="review_report_not_found", message="尚无预审报告。", status_code=404
            )
        self.management.repository.get_document(actor, row["document_id"])
        for identifier in row["payload"]["policy_versions"]:
            self.management.repository.get_document(actor, UUID(identifier))
        current = self.fingerprint(actor, contract_id, row["policy_base_id"])
        return dict(
            id=str(row["id"]),
            created_at=_utc(row["created_at"]),
            status="CURRENT" if current == row["fingerprint"] else "STALE",
            policy_base_id=str(row["policy_base_id"]),
            **row["payload"],
        )

    def history(
        self, actor: ActorContext, *, handled: bool = False, contract_id: UUID | None = None
    ) -> list[Any]:
        self.knowledge._require_reader(actor)
        if self.local:
            with self.store._transaction(actor) as connection:
                instances = self.local.instances(connection, actor)
            return sorted(
                (
                    i
                    for i in instances
                    if (contract_id is None or i.contract_id == contract_id)
                    and (not handled or any(s.decided_by == actor.user_id for s in i.steps))
                ),
                key=lambda i: (i.updated_at, i.id),
                reverse=True,
            )[:100]
        identifiers: Any
        with self.store._transaction(actor) as connection:
            identifiers = (
                connection.execute(
                    text("""
                SELECT a.id FROM approval_instances a JOIN contracts c
                  ON c.id=a.contract_id AND c.tenant_id=a.tenant_id
                WHERE a.tenant_id=:tenant AND (:tenant_scope OR c.created_by=:user OR
                    (:department_scope AND c.department_id=ANY(:departments)))
                AND (NOT :handled OR EXISTS(SELECT 1 FROM approval_workflow_steps s
                    WHERE s.approval_instance_id=a.id AND s.decided_by=:user))
                AND (CAST(:contract_id AS uuid) IS NULL OR a.contract_id=CAST(:contract_id AS uuid))
                ORDER BY a.updated_at DESC,a.id LIMIT 100
            """),
                    {
                        "tenant": actor.tenant_id,
                        "user": actor.user_id,
                        "tenant_scope": actor.data_scope is DataScope.TENANT,
                        "department_scope": actor.data_scope is DataScope.DEPARTMENT,
                        "departments": list(actor.department_ids),
                        "handled": handled,
                        "contract_id": contract_id,
                    },
                )
                .scalars()
                .all()
            )
        return [
            self.approvals.get_instance(actor, UUID(str(identifier))) for identifier in identifiers
        ]

    def diff(self, actor: ActorContext, contract_id: UUID) -> dict[str, Any]:
        contract = self.contracts.get_contract(actor, contract_id)
        if len(contract.versions) < 2:
            return {"entries": [], "message": "至少关联两个合同版本后才能比较。"}
        old, new = contract.versions[-2:]
        left, right = self.link(actor, contract_id, old.id), self.link(actor, contract_id, new.id)
        if not left or not right:
            raise conflict("该比较包含未关联知识库的旧业务版本，请使用业务版本对比接口。")
        before, after = (
            self.blocks(actor, left["document_id"]),
            self.blocks(actor, right["document_id"]),
        )
        old_hashes, new_hashes = {b.content_hash for b in before}, {b.content_hash for b in after}
        entries = [("REMOVED", left, b) for b in before if b.content_hash not in new_hashes]
        entries += [("ADDED", right, b) for b in after if b.content_hash not in old_hashes]
        return dict(
            from_version_id=str(old.id),
            to_version_id=str(new.id),
            total_changes=len(entries),
            truncated=len(entries) > 100,
            entries=[
                dict(
                    change=change,
                    source=self.citation(
                        self.management.repository.get_document(actor, link["document_id"]), block
                    ),
                )
                for change, link, block in entries[:100]
            ],
        )
