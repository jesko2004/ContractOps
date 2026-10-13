"""Document version browsing and durable Inspector task association."""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "20261009_0010"
down_revision: str | None = "20261009_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _execute(name: str) -> None:
    sql = (Path(__file__).resolve().parents[1] / "sql" / name).read_text(encoding="utf-8")
    with op.get_bind().connection.driver_connection.cursor() as cursor:
        cursor.execute(sql, prepare=False)


def upgrade() -> None:
    _execute("0010_knowledge_details.up.sql")


def downgrade() -> None:
    _execute("0010_knowledge_details.down.sql")
