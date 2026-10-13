from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from contractops.application.approvals import ApprovalService
from contractops.application.audits import AuditService
from contractops.application.contracts import ContractService
from contractops.application.documents import DocumentService
from contractops.application.events import EventAdminService
from contractops.application.knowledge import KnowledgeService
from contractops.application.knowledge_management import KnowledgeManagementService
from contractops.application.obligations import ObligationService
from contractops.auth import JWTDecoder
from contractops.context import (
    ActorContext,
    bind_actor_context,
    reset_actor_context,
)
from contractops.errors import ContractOpsError

_bearer = HTTPBearer(auto_error=False)


async def authenticated_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AsyncIterator[ActorContext]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise ContractOpsError(
            code="authentication_required",
            message="a bearer access token is required",
            status_code=401,
        )
    decoder = cast(JWTDecoder, request.app.state.jwt_decoder)
    actor = decoder.decode(credentials.credentials)
    request.state.actor = actor
    token = bind_actor_context(actor)
    try:
        yield actor
    finally:
        reset_actor_context(token)


def get_contract_service(request: Request) -> ContractService:
    service = getattr(request.app.state, "contract_service", None)
    if not isinstance(service, ContractService):
        raise ContractOpsError(
            code="contract_service_unavailable",
            message="合同业务需要 PostgreSQL 模式。",
            status_code=503,
        )
    return service


def get_document_service(request: Request) -> DocumentService:
    service = getattr(request.app.state, "document_service", None)
    if not isinstance(service, DocumentService):
        raise ContractOpsError(
            code="document_service_unavailable",
            message="document processing is not configured",
            status_code=503,
        )
    return service


def get_knowledge_service(request: Request) -> KnowledgeService:
    service = getattr(request.app.state, "knowledge_service", None)
    if not isinstance(service, KnowledgeService):
        raise ContractOpsError(
            code="knowledge_service_unavailable",
            message="contract knowledge is not configured",
            status_code=503,
        )
    return service


def get_knowledge_management_service(request: Request) -> KnowledgeManagementService:
    service = getattr(request.app.state, "knowledge_management_service", None)
    if not isinstance(service, KnowledgeManagementService):
        raise ContractOpsError(
            code="knowledge_management_unavailable",
            message="knowledge management is not configured",
            status_code=503,
        )
    return service


def get_approval_service(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    if not isinstance(service, ApprovalService):
        raise ContractOpsError(
            code="approval_service_unavailable",
            message="审批业务需要 PostgreSQL 模式。",
            status_code=503,
        )
    return service


def get_event_admin_service(request: Request) -> EventAdminService:
    service = getattr(request.app.state, "event_admin_service", None)
    if not isinstance(service, EventAdminService):
        raise ContractOpsError(
            code="event_admin_unavailable",
            message="event administration is not configured",
            status_code=503,
        )
    return service


def get_obligation_service(request: Request) -> ObligationService:
    service = getattr(request.app.state, "obligation_service", None)
    if not isinstance(service, ObligationService):
        raise ContractOpsError(
            code="obligation_service_unavailable",
            message="obligation management is not configured",
            status_code=503,
        )
    return service


def get_audit_service(request: Request) -> AuditService:
    service = getattr(request.app.state, "audit_service", None)
    if not isinstance(service, AuditService):
        raise ContractOpsError(
            code="audit_service_unavailable",
            message="audit query service is not configured",
            status_code=503,
        )
    return service
