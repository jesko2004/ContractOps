from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from contractops.application.approvals import ApprovalService
from contractops.application.contracts import ContractService
from contractops.application.events import EventAdminService
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
    return cast(ContractService, request.app.state.contract_service)


def get_approval_service(request: Request) -> ApprovalService:
    return cast(ApprovalService, request.app.state.approval_service)


def get_event_admin_service(request: Request) -> EventAdminService:
    service = getattr(request.app.state, "event_admin_service", None)
    if not isinstance(service, EventAdminService):
        raise ContractOpsError(
            code="event_admin_unavailable",
            message="event administration is not configured",
            status_code=503,
        )
    return service
