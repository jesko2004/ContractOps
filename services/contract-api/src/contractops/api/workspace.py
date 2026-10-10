from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from contractops.api.dependencies import authenticated_actor
from contractops.context import ActorContext
from contractops.errors import ContractOpsError

router = APIRouter(tags=["workspace"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> HTMLResponse:
    return HTMLResponse(
        (Path(__file__).resolve().parents[1] / "web" / "workspace.html").read_text(
            encoding="utf-8"
        ),
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; frame-src blob:; "
            "object-src 'none'; img-src 'self' blob:",
        },
    )


@router.get("/workspace-business.js", include_in_schema=False)
def business_script() -> Response:
    return Response(
        (Path(__file__).resolve().parents[1] / "web/workspace-business.js").read_text(
            encoding="utf-8"
        ),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/workspace/config", include_in_schema=False)
def config(request: Request) -> dict[str, str]:
    return {"api_prefix": request.app.state.settings.api_prefix}


@router.get("/workspace/session")
def session(
    request: Request, actor: Annotated[ActorContext, Depends(authenticated_actor)]
) -> dict[str, object]:
    local = getattr(request.app.state, "local_knowledge_mode", False)
    return {
        "mode": "LOCAL_SQLITE" if local else "POSTGRES",
        "user_id": str(actor.user_id),
        "tenant_id": str(actor.tenant_id),
        "roles": sorted(role.value for role in actor.roles),
        "data_scope": actor.data_scope.value,
        "inspector_configured": bool(request.app.state.settings.inspector_url),
        "model_configured": bool(request.app.state.settings.knowledge_model_enabled),
        "department_ids": [str(identifier) for identifier in actor.department_ids],
        "local_identity": ("owner" if actor.user_id.int == 2 else "reviewer") if local else None,
        "capabilities": {"knowledge": True, "contracts": True, "approvals": True},
        "message": "本地 SQLite · 真实持久化合同/审批；演示身份可切换，不是企业认证或生产 RLS。"
        if local
        else "主应用 PostgreSQL · 知识库与合同业务共用后端。",
    }


class LocalIdentityRequest(BaseModel):
    identity: Literal["owner", "reviewer"]


@router.post("/workspace/local-identity")
def local_identity(
    payload: LocalIdentityRequest,
    request: Request,
    response: Response,
    actor: Annotated[ActorContext, Depends(authenticated_actor)],
) -> dict[str, str]:
    if not getattr(request.app.state, "local_knowledge_mode", False):
        raise ContractOpsError(
            code="local_identity_unavailable",
            message="正式环境不提供演示身份切换。",
            status_code=404,
        )
    response.headers["Cache-Control"] = "no-store"
    return {
        "token": request.app.state.demo_token
        if payload.identity == "owner"
        else request.app.state.local_review_token
    }
