# ContractOps API

FastAPI service for the ContractOps contract approval and obligation-risk bounded context.

The service currently includes the M0 engineering baseline, M1 multitenant contract ledger, and
M2 approval workflow:

- an injectable application factory, request context, and stable error envelope;
- liveness, readiness, and system endpoints;
- the contract lifecycle state machine from the implementation plan;
- Alembic-managed PostgreSQL/pgvector migrations with rollback verification;
- isolated PostgreSQL, Redis, MinIO, API, and quality-check Compose services;
- a path-filtered CI gate for Ruff, mypy, pytest, and empty-database migrations.
- HS256 JWT validation with tenant, user, role, department, and data-scope claims;
- application-layer RBAC and department/owner scope enforcement;
- PostgreSQL repositories with transaction-scoped RLS context;
- idempotent contract creation and immutable contract-version registration;
- tenant-prefixed object keys and database integration tests for isolation and replay.

Workers, object upload, and model calls are added in later milestones. They are not stubbed as
successful behavior.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python scripts/check.py
.venv/Scripts/uvicorn contractops.main:app --reload --port 8080
```

Run migrations with an administrator/migration role rather than the runtime application role:

```bash
set CONTRACTOPS_MIGRATION_DATABASE_URL=postgresql://contractops_admin:contractops-admin-dev@localhost:55432/contractops
.venv/Scripts/alembic upgrade head
.venv/Scripts/python scripts/verify_migrations.py
```

Run the isolated stack and checks from the repository root:

```bash
docker compose -f deploy/contractops/docker-compose.test.yml up \
  --build --abort-on-container-exit --exit-code-from checks checks
```

The migration verification performs `upgrade head` twice, rolls back to `base`, upgrades again,
and verifies that the database is at the current head.

## Authentication and contract ledger

Contract endpoints require `Authorization: Bearer <JWT>`. The token must use HS256 and include
`iss`, `aud`, `exp`, `tenant_id`, `sub`, `roles`, `department_ids`, and `data_scope` claims.
Supported data scopes are `OWN`, `DEPARTMENT`, and `TENANT`. Replace the example JWT secret in every
non-local environment and keep it outside source control.

Available M1 endpoints:

- `POST /v1/contracts` creates a tenant-scoped contract ledger entry;
- `GET /v1/contracts/{contract_id}` reads a contract within the caller's data scope;
- `POST /v1/contracts/{contract_id}/versions` registers an immutable file version.

Both write endpoints require an `Idempotency-Key` header. Replaying the same key and payload returns
the original resource; reusing the key with a different payload returns a stable conflict error.

## Approval workflow

Available M2 endpoints:

- `POST /v1/approval-policies` creates a new draft version;
- `POST /v1/approval-policies/{policy_id}:publish` publishes an immutable version;
- `POST /v1/contracts/{contract_id}:submit` selects a policy and stores its snapshot;
- `GET /v1/approval-tasks` returns the caller's actionable tasks;
- `GET /v1/approval-instances/{instance_id}` returns workflow state and steps;
- `POST /v1/approval-steps/{step_id}:claim` atomically claims a ready step;
- `POST /v1/approval-steps/{step_id}:decide` approves, rejects, or requests changes;
- `POST /v1/approval-steps/{step_id}:transfer` transfers a claimed step.

Mutating workflow actions except the naturally idempotent publish transition require an
`Idempotency-Key`. Step actions also require the caller's last observed `expected_version`, so only
one concurrent decision can succeed. A contract returned for changes must receive a new immutable
version before it becomes a draft that can be submitted again.
