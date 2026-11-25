"""Tests for JSON logging, the request id and the per-request log line."""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from myapp.app import app
from myapp.config import ModelSettings
from myapp.log_config import (
    REQUEST_ID_HEADER,
    configure_logging,
    new_request_id,
    start_request,
)
from myapp.model_client import OpenAICompatibleClient
from tests.fakes import FakeModelClient

client = TestClient(app)


@pytest.fixture
def records() -> Iterator[list[dict[str, Any]]]:
    """Collect every loguru record, flattened to message, level and extra fields."""
    seen: list[dict[str, Any]] = []
    sink_id = logger.add(
        lambda m: seen.append(
            {
                "message": m.record["message"],
                "level": m.record["level"].name,
                **m.record["extra"],
            }
        ),
        level="DEBUG",
    )
    yield seen
    logger.remove(sink_id)


def request_lines(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the one-per-request summary lines."""
    return [r for r in records if r["message"] == "request"]


def test_response_carries_a_new_request_id() -> None:
    response = client.get("/health")
    request_id = response.headers[REQUEST_ID_HEADER]
    assert len(request_id) == 32
    assert client.get("/health").headers[REQUEST_ID_HEADER] != request_id


def test_incoming_request_id_is_reused() -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "abc-123"


@pytest.mark.parametrize("incoming", [None, "", "x" * 65, "bad\nid"])
def test_unusable_request_id_is_replaced(incoming: str | None) -> None:
    assert len(new_request_id(incoming)) == 32


def test_request_line_has_endpoint_status_duration_and_id(
    records: list[dict[str, Any]], fake_model_client: FakeModelClient
) -> None:
    response = client.get("/ready")
    (line,) = request_lines(records)
    assert line["path"] == "/ready"
    assert line["method"] == "GET"
    assert line["status"] == 200
    assert line["duration_ms"] >= 0
    assert line["request_id"] == response.headers[REQUEST_ID_HEADER]
    assert line["level"] == "INFO"


def test_health_probes_log_at_debug(records: list[dict[str, Any]]) -> None:
    client.get("/health")
    assert request_lines(records)[0]["level"] == "DEBUG"


def test_question_text_is_never_logged(records: list[dict[str, Any]]) -> None:
    question = "Can I work off campus on my F-1 visa with a secret detail?"
    client.post("/ask", json={"question": question})
    assert request_lines(records)
    assert "secret detail" not in json.dumps(records, default=str)


def test_model_call_adds_model_and_tokens_to_the_request() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 40, "total_tokens": 52},
            },
        )

    settings = ModelSettings(_env_file=None, provider="openai", model="m1")
    model = OpenAICompatibleClient(settings, transport=httpx2.MockTransport(handler))
    context = start_request("r1")
    assert model.generate("a prompt") == "hi"
    assert context["model"] == "m1"
    assert context["tokens"] == 52
    assert context["model_seconds"] >= 0
    assert "a prompt" not in json.dumps(context)


def test_standard_library_logs_become_json_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    monkeypatch.setattr("sys.stderr", out)
    configure_logging("INFO")
    try:
        start_request("req-7")
        logging.getLogger("some.library").warning("disk %d%% full", 91)
    finally:
        configure_logging("INFO")  # back to the real stderr for later tests
    line = json.loads(out.getvalue().strip().splitlines()[-1])
    assert line["message"] == "disk 91% full"
    assert line["level"] == "WARNING"
    assert line["logger"] == "some.library"
    assert line["request_id"] == "req-7"
