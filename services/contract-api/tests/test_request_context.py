from fastapi import APIRouter
from fastapi.testclient import TestClient

from contractops.context import get_request_context
from contractops.main import create_app


def test_request_context_is_available_to_endpoint() -> None:
    app = create_app()
    router = APIRouter()

    @router.get("/test/context")
    async def context_endpoint() -> dict[str, str]:
        return {"request_id": get_request_context().request_id}

    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/test/context")

    assert response.status_code == 200
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


def test_request_context_is_not_available_outside_request() -> None:
    try:
        get_request_context()
    except RuntimeError as exc:
        assert str(exc) == "request context is not available outside an HTTP request"
    else:
        raise AssertionError("request context leaked outside request scope")
