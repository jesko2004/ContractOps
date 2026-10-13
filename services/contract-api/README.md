# ContractOps API

FastAPI service for the ContractOps contract approval and obligation-risk bounded context.

The service currently includes the M0 engineering baseline through M7 production readiness:

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
- transactional Outbox publishing to a Redis Streams consumer group;
- leased publisher and notification deliveries that recover after a worker crash;
- per-event/channel/destination delivery idempotency, bounded retry, and dead letters;
- metadata-only logging and signed Webhook notification adapters;
- tenant-admin dead-letter query and manual replay operations.
- leased obligation scheduling, unique reminder windows, risk events, and cancellation on termination;
- append-only operation, business, and security audit events correlated by request and trace IDs;
- OpenTelemetry traces, allowlist-only structured logs, Prometheus metrics, and alert rules;
- auditor/admin audit search and consolidated runtime-failure query APIs.
- time-limited MinIO upload tickets with size and SHA-256 verification before parsing;
- leased PDF/DOCX ingestion with page/heading-aware immutable evidence chunks;
- deterministic amount/date findings and fail-closed validation for optional model suggestions;
- evidence-preserving version Diff responses that retain both old and new source references.
- deterministic Worker, Redis, notification, document-bomb, SSRF, and secret-leakage gates;
- a real FastAPI/PostgreSQL Locust profile with CI-enforced QPS, P95, and failure thresholds;
- a generated M7 evidence artifact, incident runbook, and repeatable final demonstration script.

Model assistance is disabled by default. Enabling it still cannot create a risk event or obligation:
validated model output is stored only as a draft document finding for later human confirmation.

## Development

### Local knowledge-base MVP (no Docker or model required)

From the repository root on Windows:

```powershell
& .\services\contract-api\.venv\Scripts\python.exe .\services\contract-api\scripts\knowledge_demo.py
```

Open `http://127.0.0.1:8081` and paste the temporary bearer token printed in the terminal.
Create a named knowledge base, upload text PDF/DOCX documents, filter by category or processing
status, browse actual persisted text fragments, retry failures, and archive documents.
The detail dialog includes full parsed text, source chunks, immutable revision/history selection,
and PDF source-page preview when the optional Inspector service is configured. New revisions
replace the current searchable version atomically without deleting old evidence.
Knowledge Q&A searches the same authorized records and returns cited source excerpts.
The ignored `data/knowledge-demo.sqlite3` database now retains bounded original files as well as
text and metadata so retries are reproducible. Legacy demo tables are imported once without deletion.

The launcher and `contractops.main:app` use the **same application factory, workspace HTML and
management/retrieval services**, with explicit SQLite/PostgreSQL storage adapters. Both serve the
workspace at `/`, with authenticated `/v1/knowledge/bases`, base-scoped document upload/list,
document metadata/text-fragment reads, retry/archive actions, search, and extractive Q&A.
Local SQLite mode now supports persisted contracts, existing-document links, manual amount/date
checks, version comparisons, advisory pre-review and human approval using the existing services.
Owner/reviewer switching is explicitly a local demonstration, not enterprise authentication.
The repository's original OpenMAIC Next.js frontend is unchanged.

PostgreSQL requires migration `20261009_0011` and updated API runtime-role grants. The new managed
knowledge tables have forced tenant RLS; explicit owner/department/tenant filters run before
ranking or pagination. Existing contract evidence remains in the original tables and participates
in all-source retrieval only when the current contract version is `READY`; no copying is required.
Answers default to `EXTRACTIVE`. Opt-in `CONTRACTOPS_KNOWLEDGE_MODEL_ENABLED=true` enables a bounded
compatible chat-completions client using `CONTRACTOPS_MODEL_BASE_URL`, `CONTRACTOPS_DOCUMENT_MODEL_NAME`,
optional backend-only `CONTRACTOPS_KNOWLEDGE_MODEL_API_KEY` and a bounded timeout. Each model claim
must contain a verified source quote; failures explicitly fall back to excerpts. No model is
configured on this machine, and vector retrieval is not implemented.

Optional Inspector integration uses `CONTRACTOPS_INSPECTOR_URL`, `CONTRACTOPS_INSPECTOR_API_KEY`,
`CONTRACTOPS_INSPECTOR_PROFILE_ID` (default `manual_query`), and `CONTRACTOPS_INSPECTOR_TIMEOUT_SECONDS`.
The local launcher accepts `--inspector-url` and `--inspector-key-file`. The companion
`scripts/inspector_local.py --repo <inspector-checkout>` must run in an already configured Inspector
Python environment; it does not install or modify the other project. Runtime files and the service
key remain under ignored `data/inspector-local`; OCR is disabled by default.

Managed PDFs get durable version-bound task associations, stable idempotency keys, progress sync,
bounded result mapping, full source-page lists, table evidence, and explicit `NEEDS_REVIEW` quality.
The browser only calls the authenticated main backend, including proxied page PNGs. Inspector does
the asynchronous processing; authorized page polling/manual sync persists completion in ContractOps.
There is no new always-running task worker, and closing the page may delay local result persistence.
DOCX keeps its existing parser. The workbench can link these managed documents to a contract without
re-uploading originals; the original business ingestion Worker is not wired to this adapter.
Inspector quality readiness is not an approval or compliance conclusion. Pre-review version
fingerprints invalidate changed evidence; final decisions remain human, with atomic local contract
and approval state updates. This is not a production PostgreSQL validation claim.

See [the MVP guide](../../architecture/CONTRACTOPS-KNOWLEDGE-MVP.md) for limits, API examples,
verification, and the boundary between the local demo and the main service.
See [the workbench MVP guide](../../architecture/CONTRACTOPS-WORKBENCH-MVP.md) for items 5–8,
model configuration, interview demonstration steps and verification limits.

### Full backend development

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

## Production deployment

Production settings fail closed before any runtime starts: development/short secrets, localhost or
development database credentials, plaintext PostgreSQL/Redis/object storage/OTLP endpoints, and
unsigned Webhooks are rejected. Validate each process role with the packaged preflight command:

```bash
contractops-preflight --role api --check-dependencies
contractops-preflight --role event-worker --check-dependencies
contractops-preflight --role ingestion-worker --check-dependencies
contractops-preflight --role scheduler --check-dependencies
contractops-preflight --role migration --check-dependencies
```

The hardened application-only deployment is
`deploy/contractops/docker-compose.prod.yml`; it requires an immutable image reference and external
TLS PostgreSQL, Redis, object storage, and OTLP services. See
`architecture/CONTRACTOPS-PRODUCTION.md` for build, preflight, rollout, backup, recovery, capacity,
and rollback requirements. The development Compose file is not a production deployment template.

Run the reliable event worker with its dedicated cross-tenant PostgreSQL role:

```bash
set CONTRACTOPS_WORKER_DATABASE_URL=postgresql://contractops_worker:contractops-worker-dev@localhost:55432/contractops
.venv/Scripts/contractops-worker
```

The API runtime role remains subject to tenant RLS. The worker role has `BYPASSRLS`, but its grants
are restricted to event-delivery and obligation-scheduling tables. Configure a Webhook only
through trusted deployment settings. Webhook requests include a stable `Idempotency-Key` and an
optional `X-ContractOps-Signature` HMAC-SHA256 header.

Run the lease-based obligation scheduler with the same restricted worker identity:

```bash
set CONTRACTOPS_WORKER_DATABASE_URL=postgresql://contractops_worker:contractops-worker-dev@localhost:55432/contractops
.venv/Scripts/contractops-scheduler
```

The scheduler uses `FOR UPDATE SKIP LOCKED`, persistent leases, and a reminder unique key. A
terminated contract atomically cancels active obligations so later scans cannot emit reminders.

## Document upload and evidence

The document path is intentionally two-phase:

1. `POST /v1/contracts/{contract_id}/uploads` registers an immutable version and returns a
   15-minute presigned upload URL.
2. The client uploads directly to MinIO, then calls
   `POST /v1/contract-versions/{version_id}:complete-upload`.
3. Completion verifies object metadata and enqueues a leased ingestion job. The worker downloads
   the object, verifies its SHA-256 digest, parses PDF pages or DOCX heading paths, and atomically
   publishes evidence chunks and rule findings.

Read evidence with `GET /v1/contract-versions/{version_id}/chunks` and
`GET /v1/contract-versions/{version_id}/findings`. Compare two immutable versions with
`GET /v1/contracts/{contract_id}/version-diff?from_version_id=...&to_version_id=...`.

Run the ingestion worker with its restricted cross-tenant role:

```bash
set CONTRACTOPS_WORKER_DATABASE_URL=postgresql://contractops_worker:contractops-worker-dev@localhost:55432/contractops
.venv/Scripts/contractops-ingestion-worker
```

## Audit and observability

Every HTTP response includes `X-Request-ID` and `X-Trace-ID`. Approval decisions, Outbox
publication, and notification delivery retain those identifiers, allowing an auditor to reconstruct
the complete path through `GET /v1/audit-events?request_id=...` or `?trace_id=...`. Only callers with
`AUDITOR` or `TENANT_ADMIN` can read audit events and
`GET /v1/admin/runtime/failures`. Audit rows are append-only at the database layer.

The API exposes Prometheus metrics at `GET /metrics`; worker and scheduler processes expose their
own registries on ports 9101 and 9102. Logs use a fixed field allowlist and never serialize request
bodies, authorization headers, contract text, event payloads, or webhook secrets.

To start Prometheus, the OpenTelemetry Collector, and Grafana locally:

```bash
set CONTRACTOPS_OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
docker compose -f deploy/contractops/docker-compose.yml --profile observability up --build
```

Prometheus is available on port 59090 and Grafana on port 53000. The checked-in alert rules cover
API server-error rate, worker and scheduler failures, and security-denial spikes. The local
collector intentionally uses the debug exporter; connect a durable trace backend in production.

## M7 production-readiness evidence

Install the performance extra and run the same profile used by CI:

```bash
.venv/Scripts/pip install -e ".[dev,performance]"
locust -f performance/locustfile.py --headless --users 20 --spawn-rate 5 \
  --run-time 30s --host http://127.0.0.1:8080 --csv artifacts/contractops
python scripts/generate_performance_report.py artifacts/contractops_stats.csv \
  --json-output artifacts/m7-evidence.json \
  --markdown-output artifacts/m7-evidence.md
```

The GitHub `ContractOps API / M7 production-readiness evidence` job runs against PostgreSQL 16,
Redis, the non-superuser RLS application role, and a real Uvicorn process. It uploads raw Locust
CSV/HTML, API logs, machine-readable JSON, and a Markdown report. The default gate requires at
least 10 requests/s, P95 no greater than 1,000 ms, and an error rate no greater than 1%.

Webhook notifications reject credentials, redirects, and destinations resolving to private,
loopback, link-local, or otherwise non-public addresses by default. A private destination must be
an explicit deployment choice using `CONTRACTOPS_NOTIFICATION_ALLOW_PRIVATE_NETWORKS=true` and
should still be protected by an egress proxy. DOCX parsing rejects encrypted, oversized,
over-populated, and suspiciously compressed packages before XML parsing.

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

## Reliable events and notification operations

The Outbox publisher leases unpublished rows with `FOR UPDATE SKIP LOCKED`, writes their IDs to a
Redis Stream, and marks them published only after Redis accepts the message. The consumer first
reclaims stale pending messages, then reads new messages from its consumer group. A unique
`(tenant_id, event_id, channel, destination)` delivery record prevents normal duplicate Stream
delivery from sending the same notification twice. Webhook receivers should also honor the stable
idempotency key because no distributed system can make the network gap after a successful remote
send exactly-once.

Tenant administrators can operate dead letters through:

- `GET /v1/event-dead-letters` to list unresolved publication or delivery failures;
- `POST /v1/event-dead-letters/{dead_letter_id}:replay` to reset the failed item for retry.
