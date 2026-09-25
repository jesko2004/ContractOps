from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from contractops.api import health, system
from contractops.config import Settings, get_settings
from contractops.errors import install_error_handlers
from contractops.middleware.request_context import RequestContextMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Database, Redis, and object-store clients will be initialized here in M1.
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description="Contract review, approval, and obligation management service.",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.add_middleware(RequestContextMiddleware)
    install_error_handlers(application)
    application.include_router(health.router)
    application.include_router(system.router, prefix=settings.api_prefix)
    return application


app = create_app()
