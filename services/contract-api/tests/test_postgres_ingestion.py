from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import text

from contractops.infrastructure.document_parsing import ParsedBlock
from contractops.infrastructure.postgres.database import WorkerDatabase
from contractops.infrastructure.postgres.ingestion import PostgresIngestionStore

ADMIN_URL = os.getenv("CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL")
WORKER_URL = os.getenv("CONTRACTOPS_INTEGRATION_WORKER_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL or not WORKER_URL, reason="PostgreSQL required")


@pytest.mark.parametrize("same_worker", [False, True])
def test_reclaimed_job_rejects_stale_success_failure_and_renewal(same_worker: bool) -> None:
    assert ADMIN_URL and WORKER_URL
    tenant, contract, version, job_id = (uuid4() for _ in range(4))
    with psycopg.connect(ADMIN_URL) as connection:
        connection.execute("INSERT INTO tenants (id, name) VALUES (%s, 'lease-test')", (tenant,))
        connection.execute(
            """INSERT INTO contracts
            (id, tenant_id, contract_number, title, contract_type, counterparty_name,
             department_id, created_by)
            VALUES (%s, %s, %s, 'lease test', 'SERVICE', 'Supplier', %s, %s)""",
            (contract, tenant, str(contract), uuid4(), uuid4()),
        )
        connection.execute(
            """INSERT INTO contract_versions
            (id, tenant_id, contract_id, version_number, status, object_key,
             file_name, media_type, size_bytes, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'UPLOADED', 'lease-test.pdf',
                    'lease-test.pdf', 'application/pdf', 1, %s, %s)""",
            (version, tenant, contract, "a" * 64, uuid4()),
        )
        connection.execute(
            """INSERT INTO ingestion_jobs (id, tenant_id, contract_version_id, created_at)
            VALUES (%s, %s, %s, '1970-01-01')""",
            (job_id, tenant, version),
        )
    database = WorkerDatabase(WORKER_URL)
    store = PostgresIngestionStore(database)
    try:
        old = store.claim("worker-A", lease_seconds=120)
        assert old and old.id == job_id
        assert store.renew(old, lease_seconds=120)
        with psycopg.connect(ADMIN_URL) as connection:
            connection.execute(
                "UPDATE ingestion_jobs SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = %s",
                (job_id,),
            )
        # Even an expired lease that has not yet been reclaimed cannot commit.
        store.fail(old, code="document_parse_failed", message="stale failure")
        current = store.claim("worker-A" if same_worker else "worker-B", lease_seconds=120)
        assert current and current.id == job_id
        assert not store.renew(old, lease_seconds=120)
        store.succeed(old, (), ())
        store.fail(old, code="document_parse_failed", message="stale failure")
        with database.transaction() as connection:
            assert (
                connection.execute(
                    text("SELECT status FROM ingestion_jobs WHERE id=:id"), {"id": job_id}
                ).scalar_one()
                == "RUNNING"
            )
        store.succeed(current, (ParsedBlock(0, 1, (), "Payment due"),), ())
        store.fail(old, code="document_parse_failed", message="late failure")
        with database.transaction() as connection:
            assert (
                connection.execute(
                    text("SELECT status FROM contract_versions WHERE id=:id"), {"id": version}
                ).scalar_one()
                == "READY"
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM contract_chunks WHERE contract_version_id=:id"),
                    {"id": version},
                ).scalar_one()
                == 1
            )
    finally:
        database.dispose()
