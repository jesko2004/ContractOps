from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from contractops.context import ActorContext
from contractops.domain.approval import ApprovalStep
from contractops.errors import ContractOpsError


@dataclass(frozen=True, slots=True)
class TaskCursor:
    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class ApprovalTaskPage:
    items: tuple[ApprovalStep, ...]
    next_cursor: str | None


def encode_cursor(actor: ActorContext, step: ApprovalStep) -> str:
    value = [1, str(actor.tenant_id), str(actor.user_id), step.created_at.isoformat(), str(step.id)]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_cursor(actor: ActorContext, cursor: str | None) -> TaskCursor | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 512:
            raise ValueError("invalid cursor length")
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, list)
            or len(value) != 5
            or value[:3] != [1, str(actor.tenant_id), str(actor.user_id)]
        ):
            raise ValueError("cursor scope mismatch")
        if not isinstance(value[3], str) or not isinstance(value[4], str):
            raise ValueError("cursor sort keys must be strings")
        created = datetime.fromisoformat(value[3])
        if created.tzinfo is None:
            raise ValueError("cursor timestamp requires a timezone")
        return TaskCursor(created, UUID(value[4]))
    except (ValueError, TypeError, binascii.Error, UnicodeError) as error:
        raise ContractOpsError(
            code="pagination_cursor_invalid",
            message="invalid approval task cursor",
            status_code=422,
        ) from error
