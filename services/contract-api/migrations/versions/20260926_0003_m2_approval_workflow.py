"""Add M2 versioned approval policies and workflow tasks.

Revision ID: 20260926_0003
Revises: 20260926_0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20260926_0003"
down_revision: str | None = "20260926_0002"
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
    _execute_sql_file("0003_m2_approval_workflow.up.sql")


def downgrade() -> None:
    _execute_sql_file("0003_m2_approval_workflow.down.sql")
