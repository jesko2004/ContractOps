from sqlalchemy import text
from sqlalchemy.engine import Connection

from contractops.infrastructure.postgres.database import WorkerDatabase


def expire_key(connection: Connection, operation: str, key: str) -> None:
    """Called under the tenant/key advisory lock and tenant RLS transaction."""
    connection.execute(
        text(
            "DELETE FROM idempotency_records WHERE operation = :operation "
            "AND idempotency_key = :key AND expires_at <= now()"
        ),
        {"operation": operation, "key": key},
    )


def purge_expired(database: WorkerDatabase, *, batch_size: int = 1000) -> int:
    if not 1 <= batch_size <= 10000:
        raise ValueError("cleanup batch_size must be between 1 and 10000")
    with database.transaction() as connection:
        rows = connection.execute(
            text(
                """
            WITH expired AS (
                SELECT id FROM idempotency_records WHERE expires_at <= now()
                ORDER BY expires_at, id LIMIT :limit FOR UPDATE SKIP LOCKED
            )
            DELETE FROM idempotency_records AS record USING expired
            WHERE record.id = expired.id RETURNING record.id
            """
            ),
            {"limit": batch_size},
        ).all()
        return len(rows)
