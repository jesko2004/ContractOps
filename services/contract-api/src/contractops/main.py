from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from contractops.api import (
    approvals,
    audits,
    contracts,
    documents,
    events,
    health,
    obligations,
    system,
)
from contractops.application.approvals import ApprovalService, ApprovalWorkflow
from contractops.application.audits import AuditRepository, AuditService
from contractops.application.contracts import ContractLedger, ContractService
from contractops.application.documents import DocumentRepository, DocumentService, ObjectStore
from contractops.application.events import EventAdminRepository, EventAdminService
from contractops.application.obligations import ObligationRepository, ObligationService
from contractops.auth import JWTDecoder
from contractops.config import Settings, get_settings
from contractops.errors import install_error_handlers
from contractops.infrastructure.object_store import MinioObjectStore
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
from contractops.middleware.request_context import RequestContextMiddleware
from contractops.observability import configure_logging, configure_tracing


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
) -> FastAPI:
    settings = settings or get_settings()
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
    application.state.document_service = DocumentService(
        document_repository,
        object_store,
        contract_service,
        max_size_bytes=settings.document_max_size_bytes,
        upload_expiry_seconds=settings.upload_url_expiry_seconds,
    )
    application.add_middleware(RequestContextMiddleware)
    install_error_handlers(application)
    application.include_router(health.router)
    application.include_router(system.metrics_router)
    application.include_router(system.router, prefix=settings.api_prefix)
    application.include_router(contracts.router, prefix=settings.api_prefix)
    application.include_router(documents.router, prefix=settings.api_prefix)
    application.include_router(approvals.router, prefix=settings.api_prefix)
    application.include_router(events.router, prefix=settings.api_prefix)
    application.include_router(obligations.router, prefix=settings.api_prefix)
    application.include_router(audits.router, prefix=settings.api_prefix)
    return application


app = create_app()
