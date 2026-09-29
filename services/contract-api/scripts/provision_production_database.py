"""Grant runtime access to pre-created roles; never creates credentials or superusers."""

from __future__ import annotations

import argparse
import os

import psycopg
from psycopg import sql

from contractops.infrastructure.postgres.permissions import API_PRIVILEGES, WORKER_PRIVILEGES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-role", required=True)
    parser.add_argument("--worker-role", required=True)
    args = parser.parse_args()
    if args.app_role == args.worker_role:
        raise ValueError("API and worker roles must be distinct")
    with psycopg.connect(os.environ["CONTRACTOPS_MIGRATION_DATABASE_URL"]) as connection:
        for name, worker in ((args.app_role, False), (args.worker_role, True)):
            identity = connection.execute(
                "SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname=%s",
                (name,),
            ).fetchone()
            if identity is None or identity[0] or identity[1] or identity[2] != worker:
                raise ValueError(f"{name} must exist, be non-privileged, and BYPASSRLS={worker}")
            role = sql.Identifier(name)
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(connection.info.dbname), role
                )
            )
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
            connection.execute(sql.SQL("GRANT SELECT ON public.alembic_version TO {}").format(role))
            maps = WORKER_PRIVILEGES.values() if worker else [API_PRIVILEGES]
            for privileges in maps:
                for table, grants in privileges.items():
                    connection.execute(
                        sql.SQL("GRANT {} ON public.{} TO {}").format(
                            sql.SQL(", ").join(sql.SQL(grant) for grant in grants),
                            sql.Identifier(table),
                            role,
                        )
                    )
    print("Runtime grants applied; run role-specific dependency preflights before startup.")


if __name__ == "__main__":
    main()
