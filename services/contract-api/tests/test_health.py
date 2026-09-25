from fastapi.testclient import TestClient

from contractops.config import Settings
from contractops.main import create_app


def test_liveness_returns_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"].startswith("req_")


def test_system_info() -> None:
    settings = Settings(app_name="ContractOps Test API", version="9.9.9")
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/v1/system/info")

    assert response.status_code == 200
    assert response.json()["name"] == "ContractOps Test API"
    assert response.json()["version"] == "9.9.9"
