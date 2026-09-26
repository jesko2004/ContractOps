from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", *arguments],
        cwd=SERVICE_ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the ContractOps API quality gate")
    parser.add_argument(
        "--with-migrations",
        action="store_true",
        help="also upgrade twice, roll back, and re-upgrade the configured database",
    )
    options = parser.parse_args()

    run("ruff", "check", "src", "tests", "scripts", "migrations")
    run("mypy", "src")
    run("pytest")
    if options.with_migrations:
        subprocess.run(
            [sys.executable, str(SERVICE_ROOT / "scripts" / "verify_migrations.py")],
            cwd=SERVICE_ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
