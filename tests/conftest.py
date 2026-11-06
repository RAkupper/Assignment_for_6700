"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest

from myapp import app as app_module
from myapp.app import app, get_model_client
from tests.fakes import FakeModelClient


@pytest.fixture(autouse=True)
def isolate_local_service_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's deployment .env must not turn unit tests into live inference."""
    monkeypatch.setenv("APP_ENVIRONMENT", "development")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("LLM_PROVIDER", "echo")
    monkeypatch.setenv("GENERATION_ENABLED", "false")
    monkeypatch.setattr(app_module.settings.llm, "provider", "echo")
    monkeypatch.setattr(app_module.settings, "admin_api_key", None)
    monkeypatch.setattr(app_module.generation_settings, "enabled", False)
    app_module.asks.clear()  # every test starts under the rate limits
    app_module.retrievals.clear()


@pytest.fixture
def fake_model_client() -> Iterator[FakeModelClient]:
    """Route every get_model_client dependency in the app to a fake."""
    fake = FakeModelClient()
    app.dependency_overrides[get_model_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def slow_model_client() -> Iterator[FakeModelClient]:
    """Route get_model_client to a fake that blocks for half a second per call."""
    fake = FakeModelClient(delay_s=0.5)
    app.dependency_overrides[get_model_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()
