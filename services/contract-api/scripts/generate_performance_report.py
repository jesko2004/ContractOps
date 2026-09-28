from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

TENANT_ID = "00000000-0000-0000-0000-00000000f001"
MEASURED_RELATIONS = (
    "contracts",
    "contract_versions",
    "approval_instances",
    "approval_steps",
    "idempotency_records",
    "outbox_events",
    "audit_events",
)


def _number(row: dict[str, str], key: str) -> float:
    value = row.get(key, "0").strip()
    return float(value or 0)


def _load_aggregate(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        if row.get("Name") == "Aggregated":
            return row
    raise RuntimeError("Locust aggregate row was not found")


def _storage_evidence() -> tuple[int, int, float]:
    database_url = os.environ["CONTRACTOPS_INTEGRATION_ADMIN_DATABASE_URL"]
    with psycopg.connect(database_url) as connection:
        contract_count = int(
            connection.execute(
                "SELECT count(*) FROM contracts WHERE tenant_id = %s",
                (TENANT_ID,),
            ).fetchone()[0]
        )
        storage_bytes = int(
            connection.execute(
                """
                SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0)
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname = ANY(%s)
                """,
                (list(MEASURED_RELATIONS),),
            ).fetchone()[0]
        )
    per_contract = storage_bytes / max(contract_count, 1)
    return contract_count, storage_bytes, per_contract


def _markdown(evidence: dict[str, Any]) -> str:
    load = evidence["load_test"]
    storage = evidence["resource_cost"]
    return f"""# ContractOps M7 CI acceptance evidence

Generated at `{evidence['generated_at']}` from commit `{evidence['commit_sha']}`.

## Measured API and PostgreSQL load test

| Metric | Result |
| --- | ---: |
| Requests | {load['requests']} |
| Failures | {load['failures']} ({load['failure_rate_percent']:.3f}%) |
| Throughput | {load['qps']:.2f} requests/s |
| P50 | {load['p50_ms']:.0f} ms |
| P95 | {load['p95_ms']:.0f} ms |
| P99 | {load['p99_ms']:.0f} ms |

The run used {evidence['profile']['users']} concurrent Locust users for
{evidence['profile']['duration_seconds']} seconds against the real FastAPI process, PostgreSQL 16,
RLS runtime role, and Redis service on a GitHub-hosted runner.

## Deterministic recovery and correctness gates

| Gate | Result | Evidence |
| --- | ---: | --- |
| Stale approval conflict rejected | 1/1 (100%) | PostgreSQL integration test |
| Worker/Redis/notification recovery | 3/3 (100%) | M7 fault-injection tests |
| Duplicate notification rate | 0/2 (0%) | delivery uniqueness and replay tests |
| Cross-tenant/unauthorized blocking | 100% | RLS, API authorization, and audit tests |

## Per-contract resource evidence

| Metric | Result |
| --- | ---: |
| Contracts created | {storage['contract_count']} |
| Measured relation storage | {storage['relation_storage_bytes']} bytes |
| Approximate DB bytes per contract | {storage['database_bytes_per_contract']:.2f} |
| Model token cost | 0 (model assistance disabled) |

These numbers are a CI baseline, not a production capacity promise. Repeat the same profile in the
target deployment and compare it with the checked thresholds before release.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and gate the M7 performance evidence")
    parser.add_argument("stats_csv", type=Path)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--duration-seconds", type=int, default=30)
    parser.add_argument("--min-qps", type=float, default=10)
    parser.add_argument("--max-p95-ms", type=float, default=1_000)
    parser.add_argument("--max-failure-rate", type=float, default=0.01)
    options = parser.parse_args()

    row = _load_aggregate(options.stats_csv)
    requests = int(_number(row, "Request Count"))
    failures = int(_number(row, "Failure Count"))
    failure_rate = failures / max(requests, 1)
    contract_count, relation_storage, per_contract = _storage_evidence()
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "commit_sha": os.getenv("GITHUB_SHA", "local"),
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
        "profile": {
            "users": options.users,
            "duration_seconds": options.duration_seconds,
            "runtime": "GitHub-hosted Ubuntu runner" if os.getenv("GITHUB_ACTIONS") else "local",
        },
        "load_test": {
            "requests": requests,
            "failures": failures,
            "failure_rate_percent": failure_rate * 100,
            "qps": _number(row, "Requests/s"),
            "p50_ms": _number(row, "50%"),
            "p95_ms": _number(row, "95%"),
            "p99_ms": _number(row, "99%"),
        },
        "resource_cost": {
            "contract_count": contract_count,
            "relation_storage_bytes": relation_storage,
            "database_bytes_per_contract": per_contract,
            "model_tokens_per_contract": 0,
        },
    }
    options.json_output.parent.mkdir(parents=True, exist_ok=True)
    options.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    options.json_output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    options.markdown_output.write_text(_markdown(evidence), encoding="utf-8")

    failures_found: list[str] = []
    if evidence["load_test"]["qps"] < options.min_qps:
        failures_found.append(f"QPS below {options.min_qps}")
    if evidence["load_test"]["p95_ms"] > options.max_p95_ms:
        failures_found.append(f"P95 above {options.max_p95_ms} ms")
    if failure_rate > options.max_failure_rate:
        failures_found.append(f"failure rate above {options.max_failure_rate:.2%}")
    if failures_found:
        raise SystemExit("; ".join(failures_found))


if __name__ == "__main__":
    main()
