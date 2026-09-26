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
    version: str = "0.2.0"
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
    redis_url: str = "redis://localhost:56379/0"
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
