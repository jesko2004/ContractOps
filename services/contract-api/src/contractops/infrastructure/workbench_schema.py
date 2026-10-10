from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

from contractops.infrastructure.knowledge_schema import metadata

links = Table(
    "workspace_contract_documents",
    metadata,
    Column("contract_version_id", Uuid, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("contract_id", Uuid, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("request_key", String(200), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("confirmation", JSON, nullable=False),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "contract_id", "request_key"),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"], ["knowledge_documents.tenant_id", "knowledge_documents.id"]
    ),
)
reports = Table(
    "workspace_review_reports",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Uuid, nullable=False),
    Column("contract_id", Uuid, nullable=False),
    Column("document_id", Uuid, nullable=False),
    Column("policy_base_id", Uuid, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_by", Uuid, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"], ["knowledge_documents.tenant_id", "knowledge_documents.id"]
    ),
    ForeignKeyConstraint(
        ["tenant_id", "policy_base_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"]
    ),
)
