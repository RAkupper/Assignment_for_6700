"""The Qdrant client gets an explicit, validated time limit from QDRANT_TIMEOUT_S."""

from typing import Any

import pytest
from pydantic import ValidationError

from myapp.vectordb import collection
from myapp.vectordb.collection import QdrantSettings, get_client


def test_timeout_defaults_to_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing set, a request to Qdrant gives up after 5 s."""
    monkeypatch.delenv("QDRANT_TIMEOUT_S", raising=False)
    assert QdrantSettings(_env_file=None).timeout_s == 5


@pytest.mark.parametrize("value", ["0", "-1", "301", "five"])
def test_bad_timeout_stops_startup(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """A zero, negative, huge or non-numeric timeout fails at startup."""
    monkeypatch.setenv("QDRANT_TIMEOUT_S", value)
    with pytest.raises(ValidationError):
        QdrantSettings(_env_file=None)


def test_client_is_built_with_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_client hands the setting to QdrantClient, so every call is bounded."""
    seen: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(collection, "QdrantClient", fake_client)
    get_client(QdrantSettings(url="http://qdrant:6333", timeout_s=7, _env_file=None))
    assert seen["timeout"] == 7
    assert seen["url"] == "http://qdrant:6333/"
