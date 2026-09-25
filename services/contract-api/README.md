# ContractOps API

FastAPI skeleton for the ContractOps bounded context.

Implemented now:

- application factory;
- liveness/readiness/system endpoints;
- request ID middleware;
- review state machine;
- initial PostgreSQL/pgvector migration.

Business CRUD, persistence adapters, authentication, workers, and model calls are intentionally
not stubbed as successful behavior. They will be added milestone by milestone.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/pytest
.venv/Scripts/uvicorn contractops.main:app --reload --port 8080
```
