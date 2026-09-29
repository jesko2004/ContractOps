from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import cast

from redis import Redis

from contractops.config import RuntimeRole, Settings, get_settings
from contractops.infrastructure.object_store import MinioObjectStore
from contractops.infrastructure.postgres import Database

RUNTIME_ROLES: tuple[RuntimeRole, ...] = (
    "api",
    "event-worker",
    "ingestion-worker",
    "scheduler",
    "migration",
)


def validate_role(settings: Settings, role: RuntimeRole) -> tuple[str, ...]:
    if settings.environment.strip().lower() not in {"prod", "production"}:
        raise ValueError("CONTRACTOPS_ENVIRONMENT must be production")
    if settings.runtime_role != role:
        raise ValueError(
            f"CONTRACTOPS_RUNTIME_ROLE must be {role} for this process, got {settings.runtime_role}"
        )

    required: list[tuple[str, str | None]] = []
    if role == "api":
        required.append(("CONTRACTOPS_DATABASE_URL", settings.database_url))
    elif role in {"event-worker", "ingestion-worker", "scheduler"}:
        required.append(("CONTRACTOPS_WORKER_DATABASE_URL", settings.worker_database_url))
    elif role == "migration":
        required.append(("CONTRACTOPS_MIGRATION_DATABASE_URL", settings.migration_database_url))

    missing = [name for name, value in required if not value]
    if missing:
        raise ValueError("missing required production settings: " + ", ".join(missing))

    checks = [name for name, _ in required]
    if role == "event-worker":
        checks.append("CONTRACTOPS_REDIS_URL")
    if role in {"api", "ingestion-worker"}:
        checks.extend(
            (
                "CONTRACTOPS_OBJECT_STORE_ENDPOINT",
                "CONTRACTOPS_OBJECT_STORE_ACCESS_KEY",
                "CONTRACTOPS_OBJECT_STORE_SECRET_KEY",
                "CONTRACTOPS_OBJECT_STORE_BUCKET",
            )
        )
    if role == "ingestion-worker" and settings.document_model_assistance_enabled:
        checks.append("CONTRACTOPS_MODEL_BASE_URL")
    if role != "migration":
        checks.append("CONTRACTOPS_OTEL_EXPORTER_OTLP_ENDPOINT")
    return tuple(checks)


def validate_runtime_role(settings: Settings, role: RuntimeRole) -> tuple[str, ...]:
    if settings.environment.strip().lower() not in {"prod", "production"}:
        return ()
    return validate_role(settings, role)


def check_dependencies(settings: Settings, role: RuntimeRole) -> tuple[str, ...]:
    checked: list[str] = []
    database_url = (
        settings.database_url
        if role == "api"
        else settings.migration_database_url
        if role == "migration"
        else settings.worker_database_url
    )
    if database_url:
        database = Database(database_url)
        try:
            database.check_permissions(role)
        finally:
            database.dispose()
        checked.append("postgresql")

    if role == "event-worker":
        redis_client: Redis = Redis.from_url(settings.redis_url)
        try:
            redis_client.ping()
        finally:
            redis_client.close()
        checked.append("redis")

    if role in {"api", "ingestion-worker"}:
        object_store = MinioObjectStore(
            settings.object_store_endpoint,
            settings.object_store_access_key,
            settings.object_store_secret_key,
            settings.object_store_bucket,
            secure=settings.object_store_secure,
        )
        object_store.check()
        checked.append("object-store")
    return tuple(checked)


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a ContractOps production runtime role")
    parser.add_argument("--role", required=True, choices=RUNTIME_ROLES)
    parser.add_argument(
        "--check-dependencies",
        action="store_true",
        help="connect to PostgreSQL, Redis, and object storage when used by the selected role",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
    options = _parse_args(arguments)
    role = cast(RuntimeRole, options.role)
    settings = get_settings()
    configuration = validate_role(settings, role)
    dependencies = check_dependencies(settings, role) if options.check_dependencies else ()
    print(
        f"production preflight passed role={role} "
        f"configuration_checks={len(configuration)} dependency_checks={len(dependencies)}"
    )


if __name__ == "__main__":
    main()
