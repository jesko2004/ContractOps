from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from contractops.config import Settings
from contractops.preflight import validate_role


def _production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "jwt_secret": "prod-JWT-9x!Q2m#L7v@T4k$R8p%C6s&N3d*F5h",
        "database_url": (
            "postgresql://contractops_app:runtime-password@db.example/contractops"
            "?sslmode=verify-full"
        ),
        "migration_database_url": (
            "postgresql://contractops_admin:migration-password@db.example/contractops"
            "?sslmode=verify-full"
        ),
        "worker_database_url": (
            "postgresql://contractops_worker:worker-password@db.example/contractops"
            "?sslmode=verify-full"
        ),
        "redis_url": "rediss://redis.example/0",
        "otel_exporter_otlp_endpoint": "https://otel.example/v1/traces",
        "object_store_endpoint": "https://objects.example",
        "object_store_access_key": "contractops-runtime",
        "object_store_secret_key": "prod-Object-9x!Q2m#L7v@T4k$R8p",
        "object_store_bucket": "contractops-production",
        "object_store_secure": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_defaults_fail_closed() -> None:
    with pytest.raises(ValidationError) as captured:
        Settings(environment="production")

    message = str(captured.value)
    assert "unsafe production configuration" in message
    assert "CONTRACTOPS_JWT_SECRET" in message
    assert "CONTRACTOPS_DATABASE_URL" in message
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in message

    with pytest.raises(ValidationError) as event_worker_error:
        _production_settings(
            runtime_role="event-worker",
            worker_database_url=None,
            redis_url="redis://redis.example/0",
        )
    assert "CONTRACTOPS_REDIS_URL" in str(event_worker_error.value)


def test_production_settings_accept_tls_and_non_default_secrets() -> None:
    settings = _production_settings()

    assert settings.environment == "production"
    assert settings.object_store_secure is True


def test_production_role_does_not_require_unrelated_secrets() -> None:
    settings = _production_settings(
        runtime_role="event-worker",
        jwt_secret="contractops-dev-jwt-secret-change-me",
        database_url=(
            "postgresql://contractops_app:contractops-app-dev@localhost:55432/contractops"
        ),
        object_store_secret_key="contractops-dev-secret",
        object_store_endpoint="http://localhost:59000",
        object_store_secure=False,
    )

    assert settings.runtime_role == "event-worker"


def test_production_rejects_low_entropy_secrets() -> None:
    with pytest.raises(ValidationError, match="CONTRACTOPS_JWT_SECRET"):
        _production_settings(jwt_secret="x" * 64)

    with pytest.raises(ValidationError, match="CONTRACTOPS_OBJECT_STORE_SECRET_KEY"):
        _production_settings(object_store_secret_key="y" * 64)


def test_production_rejects_wildcard_host_allowlist() -> None:
    with pytest.raises(ValidationError, match="CONTRACTOPS_ALLOWED_HOSTS"):
        _production_settings(allowed_hosts="*")

    with pytest.raises(ValidationError, match="CONTRACTOPS_ALLOWED_HOSTS"):
        _production_settings(allowed_hosts="")


def test_insecure_dependencies_require_explicit_override() -> None:
    insecure = {
        "database_url": "postgresql://app:runtime-password@db.internal/contractops",
        "migration_database_url": "postgresql://admin:migration-password@db.internal/contractops",
        "worker_database_url": "postgresql://worker:worker-password@db.internal/contractops",
        "redis_url": "redis://redis.internal/0",
        "otel_exporter_otlp_endpoint": "http://otel.internal:4318",
        "object_store_endpoint": "http://objects.internal:9000",
        "object_store_secure": False,
    }
    with pytest.raises(ValidationError):
        _production_settings(**insecure)

    settings = _production_settings(allow_insecure_dependencies=True, **insecure)
    assert settings.allow_insecure_dependencies is True


def test_production_webhook_requires_https_and_signing_secret() -> None:
    with pytest.raises(ValidationError) as captured:
        _production_settings(
            runtime_role="event-worker",
            notification_webhook_url="http://hooks.example/contractops",
        )

    assert "NOTIFICATION_WEBHOOK_URL" in str(captured.value)
    assert "NOTIFICATION_WEBHOOK_SECRET" in str(captured.value)

    settings = _production_settings(
        runtime_role="event-worker",
        notification_webhook_url="https://hooks.example/contractops",
        notification_webhook_secret="prod-Webhook-9x!Q2m#L7v@T4k$R8p%C6s",
    )
    assert settings.notification_webhook_url is not None


def test_preflight_enforces_role_specific_credentials() -> None:
    settings = _production_settings(runtime_role="event-worker", worker_database_url=None)

    with pytest.raises(ValueError, match="CONTRACTOPS_WORKER_DATABASE_URL"):
        validate_role(settings, "event-worker")

    api_settings = _production_settings(worker_database_url=None)
    assert validate_role(api_settings, "api") == (
        "CONTRACTOPS_DATABASE_URL",
        "CONTRACTOPS_OBJECT_STORE_ENDPOINT",
        "CONTRACTOPS_OBJECT_STORE_ACCESS_KEY",
        "CONTRACTOPS_OBJECT_STORE_SECRET_KEY",
        "CONTRACTOPS_OBJECT_STORE_BUCKET",
        "CONTRACTOPS_OTEL_EXPORTER_OTLP_ENDPOINT",
    )


def test_preflight_rejects_non_production_environment() -> None:
    with pytest.raises(ValueError, match="CONTRACTOPS_ENVIRONMENT"):
        validate_role(Settings(), "api")
