from functools import lru_cache
from typing import cast

from fastapi import Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTRACTOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ContractOps API"
    environment: str = "development"
    version: str = "0.3.0"
    api_prefix: str = "/v1"
    log_level: str = "INFO"

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
    notification_webhook_url: str | None = None
    notification_webhook_secret: str | None = Field(default=None, repr=False)
    object_store_endpoint: str = "http://localhost:59000"
    object_store_access_key: str = "contractops"
    object_store_secret_key: str = Field(default="contractops-dev-secret", repr=False)
    object_store_bucket: str = "contractops"
    model_base_url: str = "http://localhost:4000/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_request_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)
