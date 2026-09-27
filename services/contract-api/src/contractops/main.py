from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from contractops.api import approvals, contracts, events, health, system
from contractops.application.approvals import ApprovalService, ApprovalWorkflow
from contractops.application.contracts import ContractLedger, ContractService
from contractops.application.events import EventAdminRepository, EventAdminService
from contractops.auth import JWTDecoder
from contractops.config import Settings, get_settings
from contractops.errors import install_error_handlers
from contractops.infrastructure.postgres import (
    Database,
    PostgresApprovalWorkflow,
    PostgresContractLedger,
    PostgresEventAdminRepository,
)
from contractops.middleware.request_context import RequestContextMiddleware


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
) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="Contract review, approval, and obligation management service.",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.jwt_decoder = JWTDecoder(settings)
    if contract_ledger is None or approval_workflow is None or event_admin_repository is None:
        database = Database(settings.database_url)
        application.state.database = database
        if contract_ledger is None:
            contract_ledger = PostgresContractLedger(database)
        if approval_workflow is None:
            approval_workflow = PostgresApprovalWorkflow(database)
        if event_admin_repository is None:
            event_admin_repository = PostgresEventAdminRepository(database)
    else:
        application.state.database = None
    contract_service = ContractService(contract_ledger)
    application.state.contract_service = contract_service
    application.state.approval_service = ApprovalService(approval_workflow, contract_service)
    application.state.event_admin_service = EventAdminService(event_admin_repository)
    application.add_middleware(RequestContextMiddleware)
    install_error_handlers(application)
    application.include_router(health.router)
    application.include_router(system.router, prefix=settings.api_prefix)
    application.include_router(contracts.router, prefix=settings.api_prefix)
    application.include_router(approvals.router, prefix=settings.api_prefix)
    application.include_router(events.router, prefix=settings.api_prefix)
    return application


app = create_app()
