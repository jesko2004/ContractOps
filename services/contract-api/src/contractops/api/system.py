from fastapi import APIRouter, Depends
from pydantic import BaseModel

from contractops.config import Settings, get_settings


router = APIRouter(prefix="/system", tags=["system"])


class SystemInfo(BaseModel):
    name: str
    version: str
    environment: str


@router.get("/info", response_model=SystemInfo)
async def system_info(settings: Settings = Depends(get_settings)) -> SystemInfo:
    return SystemInfo(
        name=settings.app_name,
        version=settings.version,
        environment=settings.environment,
    )
