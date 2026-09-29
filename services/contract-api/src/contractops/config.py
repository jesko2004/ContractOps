from functools import lru_cache
from typing import Literal, Self, cast
from urllib.parse import parse_qs, urlparse

from fastapi import Request
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RuntimeRole = Literal["api", "event-worker", "ingestion-worker", "scheduler", "migration"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTRACTOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ContractOps API"
    environment: str = "development"
    version: str = "0.6.0"
    api_prefix: str = "/v1"
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    log_level: str = "INFO"
    allow_insecure_dependencies: bool = False
    runtime_role: RuntimeRole = "api"

    jwt_secret: str = Field(default="contractops-dev-jwt-secret-change-me", repr=False)
    jwt_issuer: str = "contractops"
    jwt_audience: str = "contractops-api"
    jwt_leeway_seconds: int = 30

    database_url: str = (
        "postgresql://contractops_app:contractops-app-dev@localhost:55432/contractops"
    )
    migration_database_url: str | None = None
    worker_database_url: str | None = None
    redis_url: str = "redis://localhost:56379/0"
    event_stream_name: str = "contractops.events.v1"
    event_consumer_group: str = "contractops.notifications.v1"
    event_consumer_name: str | None = None
    event_batch_size: int = Field(default=20, ge=1, le=500)
    event_lease_seconds: int = Field(default=30, ge=5, le=3600)
    event_claim_idle_ms: int = Field(default=30_000, ge=1000, le=3_600_000)
    event_poll_interval_seconds: float = Field(default=0.5, ge=0.05, le=60)
    obligation_batch_size: int = Field(default=100, ge=1, le=1000)
    obligation_lease_seconds: int = Field(default=30, ge=5, le=3600)
    obligation_poll_interval_seconds: float = Field(default=5, ge=0.1, le=300)
    otel_service_name: str = "contractops-api"
    otel_exporter_otlp_endpoint: str | None = None
    worker_metrics_port: int = 9101
    scheduler_metrics_port: int = 9102
    ingestion_worker_metrics_port: int = 9103
    notification_webhook_url: str | None = None
    notification_webhook_secret: str | None = Field(default=None, repr=False)
    notification_allow_private_networks: bool = False
    object_store_endpoint: str = "http://localhost:59000"
    object_store_access_key: str = "contractops"
    object_store_secret_key: str = Field(default="contractops-dev-secret", repr=False)
    object_store_bucket: str = "contractops"
    object_store_secure: bool = False
    upload_url_expiry_seconds: int = Field(default=900, ge=60, le=86_400)
    document_max_size_bytes: int = Field(default=104_857_600, ge=1, le=5_000_000_000)
    ingestion_batch_size: int = Field(default=5, ge=1, le=100)
    ingestion_lease_seconds: int = Field(default=120, ge=10, le=3600)
    ingestion_poll_interval_seconds: float = Field(default=1, ge=0.1, le=60)
    document_model_assistance_enabled: bool = False
    document_model_name: str = "qwen-plus"
    model_base_url: str = "http://localhost:4000/v1"

    @model_validator(mode="after")
    def reject_unsafe_production_configuration(self) -> Self:
        if self.environment.strip().lower() not in {"prod", "production"}:
            return self

        errors: list[str] = []
        role_database = _role_database(self)
        if role_database is not None:
            name, value = role_database
            if value is not None and _looks_like_development_url(value):
                errors.append(f"{name} contains a development credential or localhost address")
            if (
                value is not None
                and not self.allow_insecure_dependencies
                and not _postgres_tls_required(value)
            ):
                errors.append(f"{name} must require TLS in production")

        if self.runtime_role == "api" and _is_weak_secret(self.jwt_secret, minimum_length=32):
            errors.append(
                "CONTRACTOPS_JWT_SECRET must be a strong non-default value of at least "
                "32 characters"
            )
        if self.runtime_role == "api":
            if not self.trusted_hosts:
                errors.append("CONTRACTOPS_ALLOWED_HOSTS is required in production")
            elif "*" in self.trusted_hosts:
                errors.append("CONTRACTOPS_ALLOWED_HOSTS cannot contain '*' in production")

        uses_object_store = self.runtime_role in {"api", "ingestion-worker"}
        if uses_object_store and _is_weak_secret(self.object_store_secret_key, minimum_length=16):
            errors.append(
                "CONTRACTOPS_OBJECT_STORE_SECRET_KEY must be a strong non-default value "
                "of at least 16 characters"
            )

        if not self.allow_insecure_dependencies:
            if self.runtime_role == "event-worker" and not self.redis_url.startswith("rediss://"):
                errors.append("CONTRACTOPS_REDIS_URL must use rediss:// in production")
            if uses_object_store and (
                not self.object_store_secure or not _is_https_url(self.object_store_endpoint)
            ):
                errors.append("Contract object storage must use HTTPS in production")
            if (
                self.runtime_role != "migration"
                and self.otel_exporter_otlp_endpoint
                and not _is_https_url(self.otel_exporter_otlp_endpoint)
            ):
                errors.append(
                    "CONTRACTOPS_OTEL_EXPORTER_OTLP_ENDPOINT must use HTTPS in production"
                )
            if (
                self.runtime_role == "ingestion-worker"
                and self.document_model_assistance_enabled
                and not _is_https_url(self.model_base_url)
            ):
                errors.append("CONTRACTOPS_MODEL_BASE_URL must use HTTPS in production")

        if self.runtime_role != "migration" and not self.otel_exporter_otlp_endpoint:
            errors.append("CONTRACTOPS_OTEL_EXPORTER_OTLP_ENDPOINT is required in production")
        if self.runtime_role == "event-worker" and self.notification_webhook_url:
            if not _is_https_url(self.notification_webhook_url):
                errors.append("CONTRACTOPS_NOTIFICATION_WEBHOOK_URL must use HTTPS in production")
            if not self.notification_webhook_secret or _is_weak_secret(
                self.notification_webhook_secret, minimum_length=32
            ):
                errors.append(
                    "CONTRACTOPS_NOTIFICATION_WEBHOOK_SECRET must be a strong non-default value "
                    "of at least 32 characters when a production webhook is configured"
                )

        if errors:
            raise ValueError("unsafe production configuration: " + "; ".join(errors))
        return self

    @property
    def trusted_hosts(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.allowed_hosts.split(",") if value.strip())


def _role_database(settings: Settings) -> tuple[str, str | None] | None:
    if settings.runtime_role == "api":
        return "CONTRACTOPS_DATABASE_URL", settings.database_url
    if settings.runtime_role == "migration":
        return "CONTRACTOPS_MIGRATION_DATABASE_URL", settings.migration_database_url
    if settings.runtime_role in {"event-worker", "ingestion-worker", "scheduler"}:
        return "CONTRACTOPS_WORKER_DATABASE_URL", settings.worker_database_url
    return None


def _looks_like_development_secret(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in ("change-me", "replace-with", "dev-secret"))


def _is_weak_secret(value: str, *, minimum_length: int) -> bool:
    return (
        len(value) < minimum_length or len(set(value)) < 8 or _looks_like_development_secret(value)
    )


def _looks_like_development_url(value: str) -> bool:
    parsed = urlparse(value.replace("postgresql+psycopg://", "postgresql://", 1))
    password = parsed.password or ""
    return (parsed.hostname or "").lower() in {"localhost", "127.0.0.1", "::1"} or any(
        marker in password.lower() for marker in ("-dev", "change-me", "replace-with")
    )


def _postgres_tls_required(value: str) -> bool:
    parsed = urlparse(value.replace("postgresql+psycopg://", "postgresql://", 1))
    sslmode = parse_qs(parsed.query).get("sslmode", [""])[0].lower()
    return sslmode in {"require", "verify-ca", "verify-full"}


def _is_https_url(value: str) -> bool:
    return urlparse(value).scheme.lower() == "https"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_request_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)
