"""Backward-compatible launcher; all pages and APIs now come from the main factory."""

from pathlib import Path

from fastapi import FastAPI

from contractops.config import Settings
from contractops.infrastructure.knowledge_store import SQLiteKnowledgeRepository

SQLiteDemoKnowledgeRepository = SQLiteKnowledgeRepository


def create_demo_app(database_path: Path, *, token: str | None = None) -> FastAPI:
    from contractops.main import create_app

    return create_app(
        settings=Settings(environment="development"),
        local_knowledge_path=database_path,
        local_token=token,
    )
