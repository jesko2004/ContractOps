from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AuditCategory(StrEnum):
    OPERATION = "OPERATION"
    BUSINESS = "BUSINESS"
    SECURITY = "SECURITY"


class AuditOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    category: AuditCategory
    action: str
    resource_type: str
    resource_id: UUID | None
    outcome: AuditOutcome
    request_id: str | None
    trace_id: str | None
    metadata: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RuntimeFailure:
    occurred_at: datetime
    failure_type: str
    resource_type: str
    resource_id: UUID | None
    request_id: str | None
    trace_id: str | None
    summary: str
