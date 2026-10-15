"""Tests for the health endpoint."""

from fastapi.testclient import TestClient

from myapp.app import HealthResponse, app

client = TestClient(app)


def test_health_returns_ok() -> None:
    """The endpoint answers 200 with a payload matching the response model."""
    response = client.get("/health")

    assert response.status_code == 200
    assert HealthResponse.model_validate(response.json()).status == "ok"


def test_health_reports_app_version() -> None:
    """The payload carries the running application version."""
    assert client.get("/health").json()["version"] == app.version
