import os
from uuid import uuid4

import psycopg
import pytest

from contractops.infrastructure.postgres.database import WorkerDatabase
from contractops.infrastructure.postgres.idempotency import purge_expired

ADMIN_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")
WORKER_URL = os.getenv("CONTRACTOPS_INTEGRATION_WORKER_DATABASE_URL")


@pytest.mark.skipif(not ADMIN_URL or not WORKER_URL, reason="PostgreSQL required")
def test_cleanup_is_bounded_and_preserves_live_records() -> None:
    assert ADMIN_URL and WORKER_URL
    tenant = uuid4()
    with psycopg.connect(ADMIN_URL) as connection:
        connection.execute("INSERT INTO tenants (id, name) VALUES (%s, 'expiry')", (tenant,))
        for key, expiry in (
            ("expired-1", "1970-01-01"),
            ("expired-2", "1970-01-01"),
            ("active", "2099-01-01"),
        ):
            connection.execute(
                "INSERT INTO idempotency_records "
                "(tenant_id, operation, idempotency_key, request_hash, expires_at) "
                "VALUES (%s, 'cleanup-test', %s, 'hash', %s)",
                (tenant, key, expiry),
            )
    database = WorkerDatabase(WORKER_URL)
    try:
        assert purge_expired(database, batch_size=1) == 1
        assert purge_expired(database, batch_size=1) == 1
        with psycopg.connect(ADMIN_URL) as connection:
            rows = connection.execute(
                "SELECT idempotency_key FROM idempotency_records WHERE tenant_id = %s", (tenant,)
            ).fetchall()
            assert rows == [("active",)]
    finally:
        database.dispose()
