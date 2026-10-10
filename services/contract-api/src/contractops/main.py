import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from contractops.api import (
    approvals,
    audits,
    contracts,
    documents,
    events,
    health,
    knowledge,
    knowledge_management,
    obligations,
    system,
    workbench,
    workspace,
)
from contractops.application.approvals import ApprovalService, ApprovalWorkflow
from contractops.application.audits import AuditRepository, AuditService
from contractops.application.contracts import ContractLedger, ContractService
from contractops.application.documents import DocumentRepository, DocumentService, ObjectStore
from contractops.application.events import EventAdminRepository, EventAdminService
from contractops.application.knowledge import KnowledgeRepository, KnowledgeService
from contractops.application.knowledge_management import KnowledgeManagementService
from contractops.application.obligations import ObligationRepository, ObligationService
from contractops.auth import JWTDecoder
from contractops.config import Settings, get_settings
from contractops.context import ActorContext, DataScope, Role
from contractops.errors import install_error_handlers
from contractops.infrastructure.combined_knowledge import CombinedKnowledgeRepository
from contractops.infrastructure.contract_workbench import ContractWorkbench
from contractops.infrastructure.grounded_model import GroundedModel
from contractops.infrastructure.knowledge_inspector import KnowledgeInspector
from contractops.infrastructure.knowledge_store import KnowledgeStore, SQLiteKnowledgeRepository
from contractops.infrastructure.local_auth import LocalTokenDecoder
from contractops.infrastructure.local_business import SQLiteBusiness
from contractops.infrastructure.object_store import MinioObjectStore
from contractops.infrastructure.pdf_inspector import PDFInspectorClient
from contractops.infrastructure.postgres import (
    Database,
    PostgresApprovalWorkflow,
    PostgresAuditRepository,
    PostgresContractLedger,
    PostgresDocumentRepository,
    PostgresEventAdminRepository,
    PostgresObligationRepository,
    PostgresSecurityAuditRecorder,
)
from contractops.infrastructure.postgres.knowledge import PostgresKnowledgeRepository
from contractops.middleware.request_context import RequestContextMiddleware
from contractops.observability import configure_logging, configure_tracing
from contractops.preflight import validate_runtime_role


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        database = getattr(application.state, "database", None)
        if isinstance(database, Database):
            database.dispose()


def create_app(
    settings: Settings | None = None,
    contract_ledger: ContractLedger | None = None,
    approval_workflow: ApprovalWorkflow | None = None,
    event_admin_repository: EventAdminRepository | None = None,
    obligation_repository: ObligationRepository | None = None,
    audit_repository: AuditRepository | None = None,
    document_repository: DocumentRepository | None = None,
    object_store: ObjectStore | None = None,
    knowledge_repository: KnowledgeRepository | None = None,
    local_knowledge_path: Path | None = None,
    local_token: str | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    validate_runtime_role(settings, "api")
    configure_logging(settings.log_level)
    configure_tracing(
        service_name=settings.otel_service_name,
        endpoint=settings.otel_exporter_otlp_endpoint,
    )
    application = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="Contract review, approval, and obligation management service.",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.jwt_decoder = JWTDecoder(settings)
    application.state.local_knowledge_mode = local_knowledge_path is not None
    application.state.knowledge_management_service = None
    application.state.contract_workbench = None
    if local_knowledge_path is not None:
        if settings.environment.lower() not in {"development", "test"}:
            raise ValueError("local SQLite knowledge mode is allowed only in development/test")
        local_repository = SQLiteKnowledgeRepository(local_knowledge_path)
        application.state.database = local_repository.database
        application.state.audit_recorder = None
        application.state.demo_token = local_token or secrets.token_urlsafe(32)
        application.state.local_review_token = hmac.new(
            application.state.demo_token.encode(), b"contractops-local-reviewer", hashlib.sha256
        ).hexdigest()
        application.state.jwt_decoder = LocalTokenDecoder(
            application.state.demo_token,
            ActorContext(
                tenant_id=UUID(int=1),
                user_id=UUID(int=2),
                roles=frozenset({Role.CONTRACT_OWNER}),
                department_ids=frozenset({UUID(int=3)}),
                data_scope=DataScope.TENANT,
            ),
            alternatives={
                application.state.local_review_token: ActorContext(
                    tenant_id=UUID(int=1),
                    user_id=UUID(int=4),
                    roles=frozenset({Role.APPROVER, Role.LEGAL_ADMIN}),
                    department_ids=frozenset({UUID(int=3)}),
                    data_scope=DataScope.TENANT,
                )
            },
        )
        application.state.knowledge_service = KnowledgeService(
            local_repository, _answer_model(settings)
        )
        application.state.knowledge_management_service = _knowledge_management(
            settings, local_repository
        )
        local_business = SQLiteBusiness(local_repository.database)
        local_business.ensure_default_policy(
            ActorContext(
                tenant_id=UUID(int=1),
                user_id=UUID(int=4),
                roles=frozenset({Role.LEGAL_ADMIN}),
                department_ids=frozenset({UUID(int=3)}),
                data_scope=DataScope.TENANT,
            )
        )
        application.state.contract_service = ContractService(local_business)
        application.state.approval_service = ApprovalService(
            local_business, application.state.contract_service
        )
        application.state.contract_workbench = ContractWorkbench(
            local_repository,
            application.state.knowledge_management_service,
            application.state.knowledge_service,
            application.state.contract_service,
            application.state.approval_service,
            local_business,
        )
        application.state.document_service = None
        return _mount_routes(application, settings)
    if (
        contract_ledger is None
        or approval_workflow is None
        or event_admin_repository is None
        or obligation_repository is None
        or audit_repository is None
        or document_repository is None
        or object_store is None
    ):
        database = Database(settings.database_url)
        application.state.database = database
        if contract_ledger is None:
            contract_ledger = PostgresContractLedger(database)
        if approval_workflow is None:
            approval_workflow = PostgresApprovalWorkflow(database)
        if event_admin_repository is None:
            event_admin_repository = PostgresEventAdminRepository(database)
        if obligation_repository is None:
            obligation_repository = PostgresObligationRepository(database)
        if audit_repository is None:
            audit_repository = PostgresAuditRepository(database)
        if document_repository is None:
            document_repository = PostgresDocumentRepository(database)
        if object_store is None:
            object_store = MinioObjectStore(
                settings.object_store_endpoint,
                settings.object_store_access_key,
                settings.object_store_secret_key,
                settings.object_store_bucket,
                secure=settings.object_store_secure,
            )
        if knowledge_repository is None:
            management_repository = KnowledgeStore(database)
            knowledge_repository = CombinedKnowledgeRepository(
                PostgresKnowledgeRepository(database), management_repository
            )
            application.state.knowledge_management_service = _knowledge_management(
                settings, management_repository
            )
        elif isinstance(knowledge_repository, KnowledgeStore):
            application.state.knowledge_management_service = _knowledge_management(
                settings, knowledge_repository
            )
        application.state.audit_recorder = PostgresSecurityAuditRecorder(database)
    else:
        application.state.database = None
        application.state.audit_recorder = None
    contract_service = ContractService(contract_ledger)
    application.state.contract_service = contract_service
    application.state.approval_service = ApprovalService(approval_workflow, contract_service)
    application.state.event_admin_service = EventAdminService(event_admin_repository)
    application.state.obligation_service = ObligationService(obligation_repository)
    application.state.audit_service = AuditService(audit_repository)
    application.state.knowledge_service = (
        None
        if knowledge_repository is None
        else KnowledgeService(knowledge_repository, _answer_model(settings))
    )
    if application.state.knowledge_management_service is not None:
        application.state.contract_workbench = ContractWorkbench(
            application.state.knowledge_management_service.repository,
            application.state.knowledge_management_service,
            application.state.knowledge_service,
            application.state.contract_service,
            application.state.approval_service,
        )
    application.state.document_service = DocumentService(
        document_repository,
        object_store,
        contract_service,
        max_size_bytes=settings.document_max_size_bytes,
        upload_expiry_seconds=settings.upload_url_expiry_seconds,
    )
    return _mount_routes(application, settings)


def _knowledge_management(
    settings: Settings, repository: KnowledgeStore
) -> KnowledgeManagementService:
    inspector = (
        None
        if not settings.inspector_url
        else KnowledgeInspector(
            repository,
            PDFInspectorClient(
                settings.inspector_url,
                settings.inspector_api_key,
                settings.inspector_profile_id,
                settings.inspector_timeout_seconds,
            ),
        )
    )
    return KnowledgeManagementService(repository, inspector)


def _answer_model(settings: Settings) -> GroundedModel | None:
    return (
        GroundedModel(
            settings.model_base_url,
            settings.document_model_name,
            settings.knowledge_model_api_key,
            settings.knowledge_model_timeout_seconds,
        )
        if settings.knowledge_model_enabled
        else None
    )


def _mount_routes(application: FastAPI, settings: Settings) -> FastAPI:
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))
    application.add_middleware(RequestContextMiddleware)
    install_error_handlers(application)
    application.include_router(health.router)
    application.include_router(system.metrics_router)
    application.include_router(system.router, prefix=settings.api_prefix)
    application.include_router(contracts.router, prefix=settings.api_prefix)
    application.include_router(documents.router, prefix=settings.api_prefix)
    application.include_router(knowledge.router, prefix=settings.api_prefix)
    application.include_router(knowledge_management.router, prefix=settings.api_prefix)
    application.include_router(workspace.router)
    application.include_router(workbench.router, prefix=settings.api_prefix)
    application.include_router(approvals.router, prefix=settings.api_prefix)
    application.include_router(events.router, prefix=settings.api_prefix)
    application.include_router(obligations.router, prefix=settings.api_prefix)
    application.include_router(audits.router, prefix=settings.api_prefix)
    return application


app = create_app()
