"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest

from myapp.app import app, get_model_client
from tests.fakes import FakeModelClient


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
