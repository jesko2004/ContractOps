"""Fail-closed Compose release gate. Dry-run by default; never provisions infrastructure."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

REPOSITORY = Path(__file__).resolve().parents[1].parent.parent
COMPOSE_FILE = REPOSITORY / "deploy/contractops/docker-compose.prod.yml"
RUNTIMES = ("contract-api", "contract-worker", "ingestion-worker", "obligation-scheduler")
PREFLIGHTS = ("api", "event-worker", "ingestion-worker", "scheduler")
SERVICES = (*RUNTIMES, "migrate", "preflight-migration", *(f"preflight-{r}" for r in PREFLIGHTS))


class ReleaseError(Exception):
    """Messages contain stage names only, never raw dependency errors or configuration."""


def validate_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme == "https" and parts.hostname and parts.port != 0
            and not parts.username and not parts.password
            and parts.path in ("", "/") and not parts.query and not parts.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ReleaseError("API URL must be an HTTPS origin without credentials, path or query")
    return value.rstrip("/")


def validate_config(config: dict[str, Any], app_role: str, worker_role: str) -> str:
    services = config.get("services", {})
    images = {services.get(name, {}).get("image", "") for name in SERVICES}
    if len(images) != 1 or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", next(iter(images))):
        raise ReleaseError("All release services must use the same immutable SHA-256 image")
    for name in SERVICES:
        if services[name].get("environment", {}).get("CONTRACTOPS_ENVIRONMENT") != "production":
            raise ReleaseError("All release services must use production configuration")
    identities = []
    for service, variable in (
        ("contract-api", "CONTRACTOPS_DATABASE_URL"),
        ("contract-worker", "CONTRACTOPS_WORKER_DATABASE_URL"),
        ("migrate", "CONTRACTOPS_MIGRATION_DATABASE_URL"),
    ):
        try:
            identity = urlsplit(services[service]["environment"][variable]).username
        except (KeyError, ValueError, TypeError):
            raise ReleaseError("Database role configuration is missing or invalid") from None
        if not identity:
            raise ReleaseError("Database connection strings must name explicit roles")
        identities.append(unquote(identity))
    if len(set(identities)) != 3 or identities[:2] != [app_role, worker_role]:
        raise ReleaseError("Three distinct database roles must match the requested grants")
    return next(iter(images))


def release_steps(app_role: str, worker_role: str) -> list[tuple[str, list[str]]]:
    def once(service: str, *command: str) -> list[str]:
        return ["run", "--rm", "--no-deps", service, *command]

    steps = [
        ("pull-image", ["pull", "contract-api"]),
        ("preflight-migration", once("preflight-migration")),
        ("migrate", once("migrate")),
        ("runtime-grants", once(
            "migrate", "python", "scripts/provision_production_database.py",
            "--app-role", app_role, "--worker-role", worker_role,
        )),
        *((f"preflight-{role}", once(f"preflight-{role}")) for role in PREFLIGHTS),
        ("start-runtime", [
            "up", "--detach", "--no-deps", "--wait", "--wait-timeout", "120", *RUNTIMES,
        ]),
    ]
    for service, port in (
        ("contract-worker", 9101), ("ingestion-worker", 9103), ("obligation-scheduler", 9102),
    ):
        probe = (
            "import urllib.request; "
            f"r=urllib.request.urlopen('http://127.0.0.1:{port}/metrics',timeout=5); "
            "assert r.status == 200; assert b'python_info' in r.read()"
        )
        steps.append((f"metrics-{service}", ["exec", "-T", service, "python", "-c", probe]))
    return steps


def run_command(prefix: list[str], args: list[str], *, stage: str) -> str:
    # Compose file interpolation must come from the selected file, not ambient app settings.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("CONTRACTOPS_")}
    try:
        result = subprocess.run(
            [*prefix, *args], cwd=REPOSITORY, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=600, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError(f"{stage} could not run or exceeded 600 seconds") from None
    if result.returncode:
        # Docker/Pydantic errors may include passwords, DSNs or interpolated secrets.
        raise ReleaseError(f"{stage} failed (exit {result.returncode}); output withheld")
    return result.stdout


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise ReleaseError("HTTPS health endpoint unexpectedly redirected")


def check_public_health(origin: str) -> None:
    opener = build_opener(NoRedirect())
    for endpoint, expected in (("live", "ok"), ("ready", "ready")):
        try:
            with opener.open(f"{origin}/health/{endpoint}", timeout=15) as response:
                body = json.loads(response.read(4096))
                if response.status != 200 or body != {"status": expected}:
                    raise ReleaseError(f"public-health-{endpoint} returned an invalid response")
        except ReleaseError:
            raise
        except Exception:
            raise ReleaseError(f"public-health-{endpoint} failed; details withheld") from None
        print(f"PASS public-health-{endpoint}", flush=True)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--api-url", required=True, help="Public HTTPS origin")
    parser.add_argument("--app-role", required=True)
    parser.add_argument("--worker-role", required=True)
    parser.add_argument("--project-name", default="contractops-preproduction")
    parser.add_argument("--apply", action="store_true", help="Run migrations and start services")
    args = parser.parse_args(arguments)
    try:
        env_file = args.env_file.resolve()
        if not env_file.is_file() or env_file.is_relative_to(REPOSITORY):
            raise ReleaseError("Environment file must exist outside the repository")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project_name):
            raise ReleaseError("Invalid Compose project name")
        origin = validate_url(args.api_url)
        prefix = [
            "docker", "compose", "--project-name", args.project_name,
            "--env-file", str(env_file), "--file", str(COMPOSE_FILE),
        ]
        raw = run_command(prefix, ["config", "--format", "json"], stage="compose-config")
        try:
            config = json.loads(raw)
            validate_config(config, args.app_role, args.worker_role)
        except (ValueError, TypeError, AttributeError):
            raise ReleaseError("Invalid resolved Compose configuration; output withheld") from None
        print("PASS configuration (image digest and database role identities)", flush=True)
        steps = release_steps(args.app_role, args.worker_role)
        if not args.apply:
            print("DRY RUN: " + " -> ".join(name for name, _ in steps) + " -> public-health")
            print("No containers started. Use --apply only after backup and target approval.")
            return 0
        for stage, command in steps:
            print(f"RUN {stage}", flush=True)
            run_command(prefix, command, stage=stage)
            print(f"PASS {stage}", flush=True)
        check_public_health(origin)
        print("Release gate passed; business, fault-recovery and capacity checks still required.")
        return 0
    except ReleaseError as exc:
        print(f"STOP: {exc}. No automatic rollback or data deletion performed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
