from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    config = Config(SERVICE_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(SERVICE_ROOT / "migrations"))
    return config


def main() -> None:
    if not os.getenv("CONTRACTOPS_MIGRATION_DATABASE_URL"):
        raise SystemExit("CONTRACTOPS_MIGRATION_DATABASE_URL is required")

    config = alembic_config()
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.current(config, check_heads=True)
    print("ContractOps migration verification passed")


if __name__ == "__main__":
    main()
