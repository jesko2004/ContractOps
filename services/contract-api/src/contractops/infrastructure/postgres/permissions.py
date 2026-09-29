from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

SCHEMA_REVISION = "20260929_0008"

API_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "contracts": ("SELECT", "INSERT", "UPDATE"),
    "contract_versions": ("SELECT", "INSERT", "UPDATE"),
    "ingestion_jobs": ("SELECT", "INSERT"),
    "contract_chunks": ("SELECT",),
    "document_findings": ("SELECT",),
    "approval_policies": ("SELECT", "INSERT", "UPDATE"),
    "approval_instances": ("SELECT", "INSERT", "UPDATE"),
    "approval_workflow_steps": ("SELECT", "INSERT", "UPDATE"),
    "obligations": ("SELECT", "INSERT", "UPDATE"),
    "risk_events": ("SELECT", "UPDATE"),
    "obligation_reminders": ("SELECT",),
    "idempotency_records": ("SELECT", "INSERT", "DELETE"),
    "outbox_events": ("SELECT", "INSERT", "UPDATE"),
    "event_dead_letters": ("SELECT", "UPDATE"),
    "notification_deliveries": ("SELECT", "UPDATE"),
    "audit_events": ("SELECT", "INSERT"),
}

WORKER_PRIVILEGES: dict[str, dict[str, tuple[str, ...]]] = {
    "event-worker": {
        "outbox_events": ("SELECT", "UPDATE"),
        "notification_deliveries": ("SELECT", "INSERT", "UPDATE"),
        "event_dead_letters": ("SELECT", "INSERT", "UPDATE"),
        "audit_events": ("INSERT",),
    },
    "ingestion-worker": {
        "contract_versions": ("SELECT", "UPDATE"),
        "ingestion_jobs": ("SELECT", "UPDATE"),
        "contract_chunks": ("INSERT",),
        "document_findings": ("INSERT",),
        "outbox_events": ("INSERT",),
        "audit_events": ("INSERT",),
    },
    "scheduler": {
        "contracts": ("SELECT",),
        "obligations": ("SELECT", "UPDATE"),
        "risk_events": ("SELECT", "INSERT", "UPDATE"),
        "obligation_reminders": ("SELECT", "INSERT", "UPDATE"),
        "outbox_events": ("INSERT",),
        "audit_events": ("INSERT",),
        "idempotency_records": ("SELECT", "UPDATE", "DELETE"),
    },
}


def check_database_permissions(connection: Connection, role: str) -> None:
    identity = (
        connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
        .mappings()
        .one()
    )
    if role == "migration":
        if (
            connection.execute(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            ).scalar()
            is not True
        ):
            raise ValueError("migration role requires CREATE on schema public")
        unowned: Sequence[str] = (
            connection.execute(
                text(
                    "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace "
                    "AND relkind IN ('r', 'p') AND NOT pg_has_role(current_user, relowner, 'USAGE')"
                )
            )
            .scalars()
            .all()
        )
        if unowned:
            raise ValueError("migration role must own existing tables: " + ", ".join(unowned))
        return  # A fresh database must be allowed through before migrations run.

    if identity["rolsuper"] or identity["rolcreaterole"]:
        raise ValueError("runtime database role must not be superuser or CREATEROLE")
    if role == "api" and identity["rolbypassrls"]:
        raise ValueError("API database role must not have BYPASSRLS")
    if role != "api" and not identity["rolbypassrls"]:
        raise ValueError("cross-tenant worker database role requires BYPASSRLS")
    if connection.execute(text("SHOW row_security")).scalar_one() != "on":
        raise ValueError("runtime database connection requires row_security=on")
    versions: Sequence[str] = (
        connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
    )
    if versions != [SCHEMA_REVISION]:
        raise ValueError(f"database schema must be at {SCHEMA_REVISION}; run migrations first")
    privileges = API_PRIVILEGES if role == "api" else WORKER_PRIVILEGES[role]
    for table, required in privileges.items():
        relation = (
            connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = to_regclass(:table)"
                ),
                {"table": f"public.{table}"},
            )
            .mappings()
            .one_or_none()
        )
        if not relation or not relation["relrowsecurity"] or not relation["relforcerowsecurity"]:
            raise ValueError(f"{table} must exist with ENABLE and FORCE ROW LEVEL SECURITY")
        for privilege in required:
            allowed = connection.execute(
                text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                {"table": f"public.{table}", "privilege": privilege},
            ).scalar()
            if not allowed:
                raise ValueError(f"{role} database role requires {privilege} on {table}")
