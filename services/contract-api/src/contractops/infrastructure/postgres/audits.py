from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from contractops.context import ActorContext
from contractops.domain.audit import (
    AuditCategory,
    AuditEvent,
    AuditOutcome,
    RuntimeFailure,
)
from contractops.infrastructure.postgres.database import Database


def _metadata(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return cast(dict[str, Any], value)


def _audit_from_row(row: Mapping[str, Any] | RowMapping) -> AuditEvent:
    return AuditEvent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        actor_id=row["actor_id"],
        category=AuditCategory(row["category"]),
        action=cast(str, row["action"]),
        resource_type=cast(str, row["resource_type"]),
        resource_id=row["resource_id"],
        outcome=AuditOutcome(row["outcome"]),
        request_id=cast(str | None, row["request_id"]),
        trace_id=cast(str | None, row["trace_id"]),
        metadata=_metadata(row["metadata"]),
        occurred_at=row["occurred_at"],
    )


class PostgresAuditRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_events(
        self,
        actor: ActorContext,
        *,
        request_id: str | None,
        trace_id: str | None,
        category: AuditCategory | None,
        limit: int,
    ) -> Sequence[AuditEvent]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {"limit": limit}
        if request_id is not None:
            clauses.append("request_id = :request_id")
            parameters["request_id"] = request_id
        if trace_id is not None:
            clauses.append("trace_id = :trace_id")
            parameters["trace_id"] = trace_id
        if category is not None:
            clauses.append("category = :category")
            parameters["category"] = category.value
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        f"""
                        SELECT * FROM audit_events
                        {where}
                        ORDER BY occurred_at DESC, id DESC
                        LIMIT :limit
                        """
                    ),
                    parameters,
                )
                .mappings()
                .all()
            )
            return tuple(_audit_from_row(row) for row in rows)

    def list_failures(self, actor: ActorContext, *, limit: int) -> Sequence[RuntimeFailure]:
        with self._database.transaction(actor) as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT occurred_at, action AS failure_type, resource_type,
                               resource_id, request_id, trace_id,
                               COALESCE(metadata->>'error_code', outcome) AS summary
                        FROM audit_events
                        WHERE outcome = 'FAILED'
                        UNION ALL
                        SELECT failed_at AS occurred_at,
                               'event.dead_letter' AS failure_type,
                               'outbox_event' AS resource_type,
                               event_id AS resource_id,
                               payload->>'request_id' AS request_id,
                               payload->>'trace_id' AS trace_id,
                               error_code AS summary
                        FROM event_dead_letters
                        WHERE resolved_at IS NULL
                        ORDER BY occurred_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"limit": limit},
                )
                .mappings()
                .all()
            )
            return tuple(
                RuntimeFailure(
                    occurred_at=row["occurred_at"],
                    failure_type=cast(str, row["failure_type"]),
                    resource_type=cast(str, row["resource_type"]),
                    resource_id=row["resource_id"],
                    request_id=cast(str | None, row["request_id"]),
                    trace_id=cast(str | None, row["trace_id"]),
                    summary=cast(str, row["summary"]),
                )
                for row in rows
            )


class PostgresSecurityAuditRecorder:
    def __init__(self, database: Database) -> None:
        self._database = database

    def record_security(
        self,
        actor: ActorContext,
        *,
        request_id: str,
        trace_id: str,
        method: str,
        route: str,
        status_code: int,
    ) -> None:
        with self._database.transaction(actor) as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        tenant_id, actor_id, category, action, resource_type,
                        outcome, request_id, trace_id, metadata
                    ) VALUES (
                        :tenant_id, :actor_id, 'SECURITY', 'security.access.denied',
                        'http_request', 'DENIED', :request_id, :trace_id,
                        CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "tenant_id": actor.tenant_id,
                    "actor_id": actor.user_id,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "metadata": json.dumps(
                        {"method": method, "route": route, "status_code": status_code}
                    ),
                },
            )
