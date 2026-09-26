from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Role(StrEnum):
    CONTRACT_OWNER = "CONTRACT_OWNER"
    APPROVER = "APPROVER"
    LEGAL_ADMIN = "LEGAL_ADMIN"
    AUDITOR = "AUDITOR"
    TENANT_ADMIN = "TENANT_ADMIN"


class DataScope(StrEnum):
    OWN = "OWN"
    DEPARTMENT = "DEPARTMENT"
    TENANT = "TENANT"


@dataclass(frozen=True, slots=True)
class ActorContext:
    tenant_id: UUID
    user_id: UUID
    roles: frozenset[Role]
    department_ids: frozenset[UUID]
    data_scope: DataScope

    def has_any_role(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))

    def can_access_department(self, department_id: UUID) -> bool:
        return self.data_scope is DataScope.TENANT or department_id in self.department_ids


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Server-derived metadata that follows one request through the application."""

    request_id: str


_request_context: ContextVar[RequestContext | None] = ContextVar(
    "contractops_request_context",
    default=None,
)
_actor_context: ContextVar[ActorContext | None] = ContextVar(
    "contractops_actor_context",
    default=None,
)


def bind_request_context(context: RequestContext) -> Token[RequestContext | None]:
    return _request_context.set(context)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    _request_context.reset(token)


def get_request_context() -> RequestContext:
    context = _request_context.get()
    if context is None:
        raise RuntimeError("request context is not available outside an HTTP request")
    return context


def bind_actor_context(context: ActorContext) -> Token[ActorContext | None]:
    return _actor_context.set(context)


def reset_actor_context(token: Token[ActorContext | None]) -> None:
    _actor_context.reset(token)


def get_actor_context() -> ActorContext:
    context = _actor_context.get()
    if context is None:
        raise RuntimeError("actor context is not available outside an authenticated request")
    return context
