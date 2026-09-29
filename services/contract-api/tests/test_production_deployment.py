from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_COMPOSE = REPOSITORY_ROOT / "deploy" / "contractops" / "docker-compose.prod.yml"
PRODUCTION_ENV_EXAMPLE = REPOSITORY_ROOT / "deploy" / "contractops" / ".env.production.example"


def test_production_compose_is_fail_closed_and_hardened() -> None:
    content = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

    for required in (
        "CONTRACTOPS_IMAGE:?",
        "CONTRACTOPS_DATABASE_URL:?",
        "CONTRACTOPS_MIGRATION_DATABASE_URL:?",
        "CONTRACTOPS_WORKER_DATABASE_URL:?",
        "CONTRACTOPS_REDIS_URL:?",
        "CONTRACTOPS_JWT_SECRET:?",
        "CONTRACTOPS_ALLOWED_HOSTS:?",
        "CONTRACTOPS_OTEL_EXPORTER_OTLP_ENDPOINT:?",
        "CONTRACTOPS_RUNTIME_ROLE: api",
        "CONTRACTOPS_RUNTIME_ROLE: event-worker",
        "CONTRACTOPS_RUNTIME_ROLE: ingestion-worker",
        "CONTRACTOPS_RUNTIME_ROLE: scheduler",
        "CONTRACTOPS_RUNTIME_ROLE: migration",
        "read_only: true",
        "no-new-privileges:true",
        "cap_drop:",
        "pids_limit: 256",
        "preflight-api:",
        "preflight-event-worker:",
        "preflight-ingestion-worker:",
        "preflight-scheduler:",
    ):
        assert required in content

    lowered = content.lower()
    assert "contractops-dev" not in lowered
    assert "@localhost" not in lowered
    assert "redis://localhost" not in lowered
    assert "http://localhost:59000" not in lowered
    assert ":latest" not in lowered


def test_production_example_contains_no_usable_secret() -> None:
    content = PRODUCTION_ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "replace-with" in content
    assert "contractops-dev-secret" not in content
    assert "contractops-app-dev" not in content
