# ContractOps API

FastAPI service for the ContractOps contract approval and obligation-risk bounded context.

M0 engineering baseline includes:

- an injectable application factory, request context, and stable error envelope;
- liveness, readiness, and system endpoints;
- the contract lifecycle state machine from the implementation plan;
- Alembic-managed PostgreSQL/pgvector migrations with rollback verification;
- isolated PostgreSQL, Redis, MinIO, API, and quality-check Compose services;
- a path-filtered CI gate for Ruff, mypy, pytest, and empty-database migrations.

Business repositories, authentication, approval workflows, workers, and model calls are added in
later milestones. They are not stubbed as successful behavior.

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

Run the isolated M0 stack and checks from the repository root:

```bash
docker compose -f deploy/contractops/docker-compose.test.yml up \
  --build --abort-on-container-exit --exit-code-from checks checks
```

The migration verification performs `upgrade head` twice, rolls back to `base`, upgrades again,
and verifies that the database is at the current head.
