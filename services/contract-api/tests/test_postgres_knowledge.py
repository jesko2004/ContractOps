from __future__ import annotations

import hashlib
import io
import os
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import select

from contractops.application.knowledge import KnowledgeService
from contractops.application.knowledge_management import KnowledgeManagementService
from contractops.context import ActorContext, DataScope, Role
from contractops.errors import ContractOpsError
from contractops.infrastructure.knowledge_schema import bases, documents
from contractops.infrastructure.knowledge_store import KnowledgeStore
from contractops.infrastructure.postgres.database import Database
from contractops.infrastructure.postgres.knowledge import PostgresKnowledgeRepository

RUNTIME_URL = os.getenv("CONTRACTOPS_INTEGRATION_DATABASE_URL")
ADMIN_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RUNTIME_URL or not ADMIN_URL, reason="PostgreSQL required")


@pytest.fixture()
def database() -> Iterator[Database]:
    assert RUNTIME_URL
    value = Database(RUNTIME_URL)
    try:
        yield value
    finally:
        value.dispose()


def _tenant() -> UUID:
    assert ADMIN_URL
    tenant = uuid4()
    with psycopg.connect(ADMIN_URL) as connection:
        connection.execute("INSERT INTO tenants(id, name) VALUES (%s, 'knowledge')", (tenant,))
    return tenant


def _seed(
    tenant: UUID,
    owner: UUID,
    department: UUID,
    *,
    contract: UUID | None = None,
    number: int = 1,
    status: str = "READY",
) -> tuple[UUID, UUID, UUID]:
    assert ADMIN_URL
    contract_id, version_id, chunk_id = contract or uuid4(), uuid4(), uuid4()
    content = f"Payment due in {number * 30} days. 付款期限{number * 30}天。"
    with psycopg.connect(ADMIN_URL) as connection:
        if contract is None:
            connection.execute(
                """INSERT INTO contracts(id, tenant_id, title, contract_type,
                                         department_id, created_by)
                   VALUES (%s, %s, 'knowledge', 'SERVICE', %s, %s)""",
                (contract_id, tenant, department, owner),
            )
        connection.execute(
            """INSERT INTO contract_versions(id, tenant_id, contract_id, version_number,
                   status, object_key, file_name, media_type, size_bytes, content_hash, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, 'contract.pdf', 'application/pdf', 1, %s, %s)""",
            (
                version_id,
                tenant,
                contract_id,
                number,
                status,
                str(version_id),
                hashlib.sha256(str(version_id).encode()).hexdigest(),
                owner,
            ),
        )
        connection.execute(
            """INSERT INTO contract_chunks(id, tenant_id, contract_version_id, sequence,
                   page_number, content, content_hash)
               VALUES (%s, %s, %s, 0, 2, %s, %s)""",
            (chunk_id, tenant, version_id, content, hashlib.sha256(content.encode()).hexdigest()),
        )
        connection.execute(
            "UPDATE contracts SET current_version_id = %s WHERE id = %s", (version_id, contract_id)
        )
    return contract_id, version_id, chunk_id


@pytest.mark.parametrize("scope", [DataScope.OWN, DataScope.DEPARTMENT, DataScope.TENANT])
def test_database_filters_tenant_and_data_scope_before_top_k(
    database: Database, scope: DataScope
) -> None:
    tenant, department, user = _tenant(), uuid4(), uuid4()
    actor = ActorContext(
        tenant, user, frozenset({Role.CONTRACT_OWNER}), frozenset({department}), scope
    )
    mine, _, _ = _seed(tenant, user, uuid4())  # Own document is visible outside current department.
    peer, _, _ = _seed(tenant, uuid4(), department)
    other_department, _, _ = _seed(tenant, uuid4(), uuid4())
    foreign, _, _ = _seed(_tenant(), user, department)
    service = KnowledgeService(PostgresKnowledgeRepository(database))
    expected = {mine}
    if scope in {DataScope.DEPARTMENT, DataScope.TENANT}:
        expected.add(peer)
    if scope is DataScope.TENANT:
        expected.add(other_department)
    assert {item.contract_id for item in service.list_documents(actor)} == expected
    assert {item.source.contract_id for item in service.search(actor, "付款 payment")} == expected
    assert service.search(actor, "付款", limit=1)[0].source.contract_id in expected
    assert service.search(actor, "付款", contract_id=foreign) == ()
    for hidden in {peer, other_department} - expected:
        assert service.search(actor, "付款", contract_id=hidden) == ()
    assert service.search(
        replace(actor, department_ids=frozenset(), data_scope=DataScope.OWN), "付款"
    )


def test_current_unready_version_never_falls_back_to_old_evidence(database: Database) -> None:
    assert ADMIN_URL
    tenant, department, owner = _tenant(), uuid4(), uuid4()
    actor = ActorContext(
        tenant, owner, frozenset({Role.CONTRACT_OWNER}), frozenset({department}), DataScope.OWN
    )
    contract, old, _ = _seed(tenant, owner, department)
    _, current, chunk = _seed(
        tenant, owner, department, contract=contract, number=2, status="PARSING"
    )
    service = KnowledgeService(PostgresKnowledgeRepository(database))
    assert service.ask(actor, "付款").status == "INSUFFICIENT_EVIDENCE"
    documents = service.list_documents(actor)
    assert documents[0].version_id == current and documents[0].status == "PARSING"
    with psycopg.connect(ADMIN_URL) as connection:
        connection.execute(
            "UPDATE contract_versions SET status = 'READY' WHERE id = %s", (current,)
        )
    citation = service.ask(actor, "付款").citations[0]
    assert citation.source.version_id == current and citation.source.version_id != old
    assert citation.source.chunk_id == chunk and citation.source.page_number == 2
    with psycopg.connect(ADMIN_URL) as connection:
        connection.execute(
            "UPDATE contract_versions SET status = 'ARCHIVED' WHERE id = %s", (current,)
        )
    assert service.search(actor, "付款") == ()


def test_user_query_is_bound_as_literal_not_executable_sql(database: Database) -> None:
    tenant, department, owner = _tenant(), uuid4(), uuid4()
    actor = ActorContext(
        tenant, owner, frozenset({Role.CONTRACT_OWNER}), frozenset({department}), DataScope.OWN
    )
    contract, _, _ = _seed(tenant, owner, department)
    service = KnowledgeService(PostgresKnowledgeRepository(database))
    hits = service.search(actor, "payment%' OR '1'='1'; --")
    assert {hit.source.contract_id for hit in hits} == {contract}
    assert service.list_documents(actor)[0].contract_id == contract


def test_managed_knowledge_lifecycle_and_rls(database: Database) -> None:
    tenant, department, owner = _tenant(), uuid4(), uuid4()
    actor = ActorContext(
        tenant, owner, frozenset({Role.CONTRACT_OWNER}), frozenset({department}), DataScope.OWN
    )
    store = KnowledgeStore(database)
    service = KnowledgeManagementService(store)
    base = service.create_base(actor, "Managed knowledge", "PostgreSQL lifecycle")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Payment due in 30 days.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
    document, replayed = service.upload(
        actor,
        base.id,
        "policy.docx",
        "POLICY",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content.getvalue(),
    )
    assert not replayed and document.status == "READY" and document.chunk_count == 1
    duplicate, replayed = service.upload(
        actor, base.id, "duplicate.docx", "POLICY", document.media_type, content.getvalue()
    )
    assert replayed and duplicate.id == document.id
    assert KnowledgeService(store).ask(actor, "Payment", base_id=base.id).citations
    foreign = replace(actor, tenant_id=_tenant())
    assert service.list_bases(foreign) == ()
    with database.transaction(foreign) as connection:
        # No explicit WHERE: verify actual runtime-role RLS, not only application filters.
        assert connection.execute(select(bases.c.id).where(bases.c.id == base.id)).first() is None
        assert (
            connection.execute(select(documents.c.id).where(documents.c.id == document.id)).first()
            is None
        )
    peer = replace(actor, user_id=uuid4())
    with pytest.raises(ContractOpsError) as error:
        service.documents(peer, base.id)
    assert error.value.status_code == 404
    archived = service.archive(actor, document.id, document.state_version)
    assert archived.status == "ARCHIVED"
    assert KnowledgeService(store).search(actor, "Payment", base_id=base.id) == ()
