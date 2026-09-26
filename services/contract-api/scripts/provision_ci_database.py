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
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contractops_worker') THEN
                    CREATE ROLE contractops_worker LOGIN PASSWORD 'contractops-worker-test'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
                ELSE
                    ALTER ROLE contractops_worker LOGIN PASSWORD 'contractops-worker-test'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
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
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO contractops_worker").format(
                sql.Identifier(database_name)
            )
        )
        connection.execute("GRANT USAGE ON SCHEMA public TO contractops_worker")
        connection.execute("GRANT SELECT, UPDATE ON outbox_events TO contractops_worker")
        connection.execute(
            "GRANT SELECT, INSERT, UPDATE ON notification_deliveries TO contractops_worker"
        )
        connection.execute(
            "GRANT SELECT, INSERT, UPDATE ON event_dead_letters TO contractops_worker"
        )


if __name__ == "__main__":
    main()
