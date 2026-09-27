"""Add M3 reliable event delivery, notification idempotency, and dead letters.

Revision ID: 20260926_0004
Revises: 20260926_0003
Create Date: 2026-09-26
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260926_0004"
down_revision: str | None = "20260926_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQL_DIRECTORY = Path(__file__).resolve().parents[1] / "sql"


def _execute_sql_file(name: str) -> None:
    sql = (_SQL_DIRECTORY / name).read_text(encoding="utf-8")
    driver_connection = op.get_bind().connection.driver_connection
    with driver_connection.cursor() as cursor:
        try:
            cursor.execute(sql, prepare=False)
        except TypeError:
            cursor.execute(sql)


def upgrade() -> None:
    _execute_sql_file("0004_m3_reliable_events.up.sql")


def downgrade() -> None:
    _execute_sql_file("0004_m3_reliable_events.down.sql")
