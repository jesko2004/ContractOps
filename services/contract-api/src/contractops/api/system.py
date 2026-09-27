from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from contractops.config import Settings, get_request_settings
from contractops.observability import prometheus_payload

router = APIRouter(prefix="/system", tags=["system"])
metrics_router = APIRouter(tags=["metrics"])


class SystemInfo(BaseModel):
    name: str
    version: str
    environment: str


@router.get("/info", response_model=SystemInfo)
async def system_info(settings: Annotated[Settings, Depends(get_request_settings)]) -> SystemInfo:
    return SystemInfo(
        name=settings.app_name,
        version=settings.version,
        environment=settings.environment,
    )


@metrics_router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    payload, content_type = prometheus_payload()
    return Response(content=payload, media_type=content_type)
