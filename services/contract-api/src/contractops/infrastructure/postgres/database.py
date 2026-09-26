from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from contractops.context import ActorContext


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Database:
    def __init__(self, url: str) -> None:
        self._engine: Engine = create_engine(
            normalize_database_url(url),
            pool_pre_ping=True,
        )

    @contextmanager
    def transaction(self, actor: ActorContext) -> Iterator[Connection]:
        with self._engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :value, true)"),
                {"value": str(actor.tenant_id)},
            )
            connection.execute(
                text("SELECT set_config('app.user_id', :value, true)"),
                {"value": str(actor.user_id)},
            )
            connection.execute(
                text("SELECT set_config('app.data_scope', :value, true)"),
                {"value": actor.data_scope.value},
            )
            yield connection

    def check(self) -> None:
        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def dispose(self) -> None:
        self._engine.dispose()
