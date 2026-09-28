from __future__ import annotations

import os
from uuid import UUID

import psycopg

TENANT_ID = UUID("00000000-0000-0000-0000-00000000f001")


def main() -> None:
    database_url = os.environ["CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL"]
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO tenants (id, name)
            VALUES (%s, 'ContractOps M7 performance tenant')
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            """,
            (TENANT_ID,),
        )
    print(f"seeded performance tenant {TENANT_ID}")


if __name__ == "__main__":
    main()
