from fastapi import APIRouter
from fastapi.testclient import TestClient

from contractops.errors import ContractOpsError
from contractops.main import create_app


def test_domain_error_uses_stable_envelope_and_request_id() -> None:
    app = create_app()
    router = APIRouter()

    @router.get("/test/error")
    async def raise_error() -> None:
        raise ContractOpsError(
            code="invalid_state_transition",
            message="the requested transition is not allowed",
            status_code=409,
            details={"current": "ACTIVE", "target": "DRAFT"},
        )

    app.include_router(router)

    with TestClient(app) as client:
        response = client.get("/test/error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "invalid_state_transition",
            "message": "the requested transition is not allowed",
            "request_id": response.headers["X-Request-ID"],
            "details": {"current": "ACTIVE", "target": "DRAFT"},
        }
    }


def test_not_found_uses_stable_error_envelope() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "http_error",
            "message": "Not Found",
            "request_id": response.headers["X-Request-ID"],
        }
    }
