"""Tests for the model client lifecycle and dependency."""

import time
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from myapp import app as app_module
from myapp.app import app, get_model_client
from myapp.config import ModelSettings, Settings
from myapp.model_client import ModelClient, build_model_client
from tests.fakes import FakeModelClient


def test_lifespan_builds_client_once_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client is built once at startup, reused, and closed at shutdown."""
    built: list[FakeModelClient] = []

    def build(_settings: object) -> FakeModelClient:
        client = FakeModelClient()
        built.append(client)
        return client

    monkeypatch.setattr(app_module, "build_model_client", build)
    with TestClient(app) as client:
        client.get("/health")
        client.get("/health")
        assert app.state.model_client is built[0]
        assert not built[0].closed
    assert len(built) == 1
    assert built[0].closed


def test_dependency_override_swaps_in_fake() -> None:
    """A route that depends on get_model_client receives the override."""
    test_app = FastAPI()

    @test_app.get("/echo")
    def echo(
        model: Annotated[ModelClient, Depends(get_model_client)],
    ) -> dict[str, str]:
        return {"answer": model.generate("hi")}

    fake = FakeModelClient(answer="stubbed")
    test_app.dependency_overrides[get_model_client] = lambda: fake
    response = TestClient(test_app).get("/echo")

    assert response.json() == {"answer": "stubbed"}
    assert fake.prompts == ["hi"]


def test_fake_model_client_fixture_overrides_app(
    fake_model_client: FakeModelClient,
) -> None:
    """The shared fixture registers the fake on the real app."""
    override = app.dependency_overrides[get_model_client]
    assert override() is fake_model_client


def test_fake_model_client_simulates_slow_inference() -> None:
    """A delay makes generate block for at least that long."""
    fake = FakeModelClient(delay_s=0.05)
    start = time.perf_counter()
    fake.generate("slow")
    assert time.perf_counter() - start >= 0.05


def test_default_client_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default client implements ModelClient, whatever the local .env sets."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    defaults = Settings(_env_file=None, llm=ModelSettings(_env_file=None))
    client = build_model_client(defaults)
    assert isinstance(client, ModelClient)
    assert client.generate("echo me") == "echo me"
