from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from contractops.context import ActorContext, Role
from contractops.domain.audit import AuditCategory, AuditEvent, RuntimeFailure
from contractops.errors import ContractOpsError


class AuditRepository(Protocol):
    def list_events(
        self,
        actor: ActorContext,
        *,
        request_id: str | None,
        trace_id: str | None,
        category: AuditCategory | None,
        limit: int,
    ) -> Sequence[AuditEvent]: ...

    def list_failures(self, actor: ActorContext, *, limit: int) -> Sequence[RuntimeFailure]: ...


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    @staticmethod
    def _authorize(actor: ActorContext) -> None:
        if not actor.has_any_role(Role.AUDITOR, Role.TENANT_ADMIN):
            raise ContractOpsError(
                code="audit_forbidden",
                message="auditor or tenant administrator role is required",
                status_code=403,
            )

    def list_events(
        self,
        actor: ActorContext,
        *,
        request_id: str | None = None,
        trace_id: str | None = None,
        category: AuditCategory | None = None,
        limit: int = 100,
    ) -> Sequence[AuditEvent]:
        self._authorize(actor)
        if request_id is not None and len(request_id) > 100:
            raise ContractOpsError(code="audit_filter_invalid", message="request_id is too long")
        if trace_id is not None and (len(trace_id) != 32 or not _is_hex(trace_id)):
            raise ContractOpsError(
                code="audit_filter_invalid",
                message="trace_id must be 32 hexadecimal characters",
            )
        return self._repository.list_events(
            actor,
            request_id=request_id,
            trace_id=trace_id,
            category=category,
            limit=min(max(limit, 1), 200),
        )

    def list_failures(
        self, actor: ActorContext, *, limit: int = 100
    ) -> Sequence[RuntimeFailure]:
        self._authorize(actor)
        return self._repository.list_failures(actor, limit=min(max(limit, 1), 200))


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
