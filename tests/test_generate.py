"""Tests for the admin-only generate endpoint."""

import asyncio
import time

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from myapp import app as app_module
from myapp.app import GenerateResponse, app
from myapp.model_client import (
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from tests.fakes import FakeModelClient

ADMIN_KEY = "k" * 43
client = TestClient(app, headers={"X-Admin-Key": ADMIN_KEY})


@pytest.fixture(autouse=True)
def admin_key_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests run with a key set on the server and sent by the client."""
    monkeypatch.setattr(app_module.settings, "admin_api_key", SecretStr(ADMIN_KEY))


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on asyncio, the loop uvicorn uses."""
    return "asyncio"


def test_generate_returns_model_completion(
    fake_model_client: FakeModelClient,
) -> None:
    """The endpoint passes the prompt to the client and returns its text."""
    response = client.post("/admin/generate", json={"prompt": "  What is OPT?  "})

    assert response.status_code == 200
    assert GenerateResponse.model_validate(response.json()).completion == "fake answer"
    assert fake_model_client.prompts == ["What is OPT?"]


def test_generate_is_absent_without_a_server_key(
    monkeypatch: pytest.MonkeyPatch, fake_model_client: FakeModelClient
) -> None:
    """With APP_ADMIN_API_KEY unset the route answers 404, even with a key sent."""
    monkeypatch.setattr(app_module.settings, "admin_api_key", None)

    response = client.post("/admin/generate", json={"prompt": "hi"})

    assert response.status_code == 404
    assert fake_model_client.prompts == []


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Key": "k" * 42 + "x"}])
def test_generate_rejects_missing_or_wrong_key(
    fake_model_client: FakeModelClient, headers: dict[str, str]
) -> None:
    """A missing or wrong key is a 401 and never reaches the model."""
    anonymous = TestClient(app, headers=headers)

    response = anonymous.post("/admin/generate", json={"prompt": "hi"})

    assert response.status_code == 401
    assert fake_model_client.prompts == []


def test_old_generate_path_is_gone(fake_model_client: FakeModelClient) -> None:
    """Nothing still answers prompts at the old public path."""
    assert client.post("/generate", json={"prompt": "hi"}).status_code in {404, 405}
    assert fake_model_client.prompts == []


@pytest.mark.parametrize(
    "body",
    [{}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 5}, {"prompt": "x" * 4001}],
)
def test_generate_rejects_invalid_prompt(
    fake_model_client: FakeModelClient, body: dict[str, object]
) -> None:
    """Bad input is a 422 and never reaches the model."""
    response = client.post("/admin/generate", json=body)

    assert response.status_code == 422
    assert fake_model_client.prompts == []


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ModelTimeoutError("timed out"), 504),
        (ModelUnavailableError("unreachable"), 503),
        (ModelClientError("bad reply"), 502),
    ],
)
def test_generate_maps_model_errors(
    fake_model_client: FakeModelClient, error: Exception, status_code: int
) -> None:
    """A failing backend surfaces as a gateway status with a typed body."""
    fake_model_client.error = error

    response = client.post("/admin/generate", json={"prompt": "hi"})

    assert response.status_code == status_code
    assert response.json() == {"detail": str(error)}


@pytest.mark.anyio
async def test_health_answers_while_generate_is_slow(
    slow_model_client: FakeModelClient,
) -> None:
    """/health answers at once while a slow model call is still in flight."""
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Admin-Key": ADMIN_KEY},
    ) as http:
        pending = asyncio.create_task(
            http.post("/admin/generate", json={"prompt": "slow"})
        )
        while not slow_model_client.prompts:  # wait until the model call has started
            await asyncio.sleep(0.01)

        start = time.perf_counter()
        health = await http.get("/health")
        health_s = time.perf_counter() - start
        still_generating = not pending.done()

        generated = await pending

    assert health.status_code == 200
    assert still_generating
    assert health_s < slow_model_client.delay_s / 2
    assert generated.status_code == 200
