import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from contractops.infrastructure.postgres.database import Database
from contractops.infrastructure.postgres.permissions import (
    SCHEMA_REVISION,
    check_database_permissions,
)

ADMIN_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")
API_URL = os.getenv("CONTRACTOPS_INTEGRATION_DATABASE_URL")
WORKER_URL = os.getenv("CONTRACTOPS_INTEGRATION_WORKER_DATABASE_URL")


def test_preflight_schema_matches_migration_head() -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    assert ScriptDirectory.from_config(config).get_current_head() == SCHEMA_REVISION


@pytest.mark.skipif(not ADMIN_URL or not API_URL or not WORKER_URL, reason="PostgreSQL required")
def test_preflight_validates_runtime_roles_and_rejects_privileged_api() -> None:
    assert ADMIN_URL and API_URL and WORKER_URL
    for url, roles in (
        (API_URL, ["api"]),
        (WORKER_URL, ["event-worker", "ingestion-worker", "scheduler"]),
        (ADMIN_URL, ["migration"]),
    ):
        database = Database(url)
        try:
            for role in roles:
                database.check_permissions(role)
        finally:
            database.dispose()
    database = Database(ADMIN_URL)
    try:
        with pytest.raises(ValueError, match="superuser"):
            database.check_permissions("api")
    finally:
        database.dispose()


@pytest.mark.skipif(not ADMIN_URL, reason="PostgreSQL required")
def test_select_one_is_insufficient_for_runtime_permissions() -> None:
    assert ADMIN_URL
    database = Database(ADMIN_URL)
    role = "preflight_" + uuid4().hex
    # Role creation and grants are rolled back with the diagnostic transaction.
    with database._engine.connect() as connection, connection.begin():
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOBYPASSRLS'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        connection.execute(text(f'GRANT SELECT ON alembic_version TO "{role}"'))
        connection.execute(text(f'SET LOCAL ROLE "{role}"'))
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
        with pytest.raises((ValueError, DBAPIError), match="SELECT on contracts|permission denied"):
            check_database_permissions(connection, "api")
        connection.rollback()
    database.dispose()
