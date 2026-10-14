"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest

from assignment_1.app import app, get_model_client
from tests.fakes import FakeModelClient


@pytest.fixture
def fake_model_client() -> Iterator[FakeModelClient]:
    """Route every get_model_client dependency in the app to a fake."""
    fake = FakeModelClient()
    app.dependency_overrides[get_model_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()
