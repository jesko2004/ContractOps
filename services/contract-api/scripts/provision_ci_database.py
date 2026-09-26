from __future__ import annotations

import os

import psycopg
from psycopg import sql


def main() -> None:
    admin_url = os.environ["CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL"]
    with psycopg.connect(admin_url, autocommit=True) as connection:
        database_name = connection.info.dbname
        connection.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_app') THEN
                    CREATE ROLE contractops_app LOGIN PASSWORD 'contractops-app-test'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
                ELSE
                    ALTER ROLE contractops_app LOGIN PASSWORD 'contractops-app-test'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
                END IF;
            END $$;
            """
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO contractops_app").format(
                sql.Identifier(database_name)
            )
        )
        connection.execute("GRANT USAGE ON SCHEMA public TO contractops_app")
        connection.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO contractops_app"
        )
        connection.execute(
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO contractops_app"
        )


if __name__ == "__main__":
    main()
