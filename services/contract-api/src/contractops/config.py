from functools import lru_cache

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
    version: str = "0.1.0"
    api_prefix: str = "/v1"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql://contractops_app:contractops-app-dev@localhost:55432/contractops"
    )
    redis_url: str = "redis://localhost:56379/0"
    object_store_endpoint: str = "http://localhost:59000"
    object_store_access_key: str = "contractops"
    object_store_secret_key: str = Field(default="contractops-dev-secret", repr=False)
    object_store_bucket: str = "contractops"
    model_base_url: str = "http://localhost:4000/v1"


@lru_cache
def get_settings() -> Settings:
    return Settings()
