from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from contractops.errors import ContractOpsError
from contractops.infrastructure.postgres import Database

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "ready"]


@router.get("/live", response_model=HealthResponse)
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=HealthResponse)
def readiness(request: Request) -> HealthResponse:
    database = getattr(request.app.state, "database", None)
    if isinstance(database, Database):
        try:
            database.check()
        except Exception as exc:
            raise ContractOpsError(
                code="dependency_unavailable",
                message="a required service is unavailable",
                status_code=503,
            ) from exc
    return HealthResponse(status="ready")
