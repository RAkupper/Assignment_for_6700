"""Tests for the model settings and the OpenAI-compatible client."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from myapp.app import model_error_handler
from myapp.config import ModelSettings, Settings
from myapp.model_client import (
    EchoModelClient,
    ModelClient,
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
    OpenAICompatibleClient,
    build_model_client,
)

Handler = Callable[[httpx2.Request], httpx2.Response]


class FakeTime:
    """A clock that only moves when the client sleeps; records each wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def make_client(
    handler: Handler, time: FakeTime | None = None, **overrides: object
) -> OpenAICompatibleClient:
    """Build a client whose HTTP calls go to an in-memory handler, with fake time."""
    time = time or FakeTime()
    settings = ModelSettings(_env_file=None, provider="openai", **overrides)
    return OpenAICompatibleClient(
        settings,
        transport=httpx2.MockTransport(handler),
        sleep=time.sleep,
        clock=time.clock,
        jitter=lambda low, high: high,  # the longest wait full jitter can pick
    )


def answers(*responses: httpx2.Response | type[Exception]) -> tuple[Handler, list[int]]:
    """A handler that plays back responses in order and counts the calls."""
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, type):
            raise item("failed", request=request)
        return item

    return handler, calls


def completion(content: object) -> httpx2.Response:
    """A minimal OpenAI-style chat completion response."""
    return httpx2.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


# Settings


def test_model_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every model setting comes from an LLM_ environment variable."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_TIMEOUT_S", "30")
    monkeypatch.setenv("LLM_DEVICE", "cuda")
    monkeypatch.setenv("LLM_CACHE_DIR", "/data/models")

    llm = ModelSettings(_env_file=None)

    assert llm.provider == "openai"
    assert str(llm.base_url) == "http://ollama:11434/v1"
    assert llm.model == "llama3.2:3b"
    assert llm.api_key is not None and llm.api_key.get_secret_value() == "sk-test"
    assert llm.timeout_s == 30
    assert llm.device == "cuda"
    assert llm.cache_dir == Path("/data/models")


def test_api_key_is_hidden_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credential never shows up in logs or tracebacks via repr()."""
    monkeypatch.setenv("LLM_API_KEY", "sk-very-secret")
    llm = ModelSettings(_env_file=None)
    assert "sk-very-secret" not in repr(llm)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("LLM_BASE_URL", "not a url"),
        ("LLM_TIMEOUT_S", "0"),
        ("LLM_TEMPERATURE", "3"),
        ("LLM_PROVIDER", "carrier-pigeon"),
        ("LLM_DEVICE", "tpu"),
    ],
)
def test_model_settings_reject_bad_values(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """Misconfiguration fails at startup, not on the first request."""
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError):
        ModelSettings(_env_file=None)


def test_settings_reject_echo_model_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production must point at a real model."""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("LLM_PROVIDER", "echo")
    with pytest.raises(ValidationError, match="LLM_PROVIDER must not be 'echo'"):
        Settings(_env_file=None, llm=ModelSettings(_env_file=None))


def test_build_model_client_picks_backend_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM_PROVIDER selects the client implementation."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    client = build_model_client(Settings(_env_file=None))
    assert isinstance(client, OpenAICompatibleClient)
    assert isinstance(client, ModelClient)
    client.close()

    monkeypatch.setenv("LLM_PROVIDER", "echo")
    assert isinstance(build_model_client(Settings(_env_file=None)), EchoModelClient)


# Client


def test_generate_sends_openai_request_and_returns_text() -> None:
    """The client posts a chat completion built from settings and reads the reply."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return completion("OPT can be requested up to 90 days before completion.")

    client = make_client(
        handler,
        base_url="http://ollama:11434/v1",
        model="qwen2.5:1.5b-instruct",
        api_key="sk-test",
        max_tokens=64,
    )
    answer = client.generate("When can I apply for OPT?")

    assert answer.startswith("OPT can be requested")
    request = seen[0]
    assert str(request.url) == "http://ollama:11434/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "qwen2.5:1.5b-instruct"
    assert body["messages"] == [
        {"role": "user", "content": "When can I apply for OPT?"}
    ]
    assert body["max_tokens"] == 64
    assert body["stream"] is False


def test_no_authorization_header_without_api_key() -> None:
    """Local servers such as Ollama get no credential."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return completion("ok")

    make_client(handler).generate("hi")
    assert "Authorization" not in seen[0].headers


def test_timeout_raises_model_timeout_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ModelTimeoutError):
        make_client(handler).generate("hi")


def test_connection_failure_raises_model_unavailable_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ModelUnavailableError, match="could not reach"):
        make_client(handler).generate("hi")


def test_http_error_raises_model_unavailable_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"error": "model not loaded"})

    with pytest.raises(ModelUnavailableError, match="HTTP 500"):
        make_client(handler).generate("hi")


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, json={"choices": []}),
        httpx2.Response(200, text="not json"),
        completion(None),
    ],
)
def test_malformed_response_raises_model_client_error(
    response: httpx2.Response,
) -> None:
    with pytest.raises(ModelClientError):
        make_client(lambda request: response).generate("hi")


# App error handling


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ModelTimeoutError("timed out"), 504),
        (ModelUnavailableError("down"), 503),
        (ModelClientError("garbled"), 502),
    ],
)
def test_model_errors_map_to_http_status(
    error: ModelClientError, status_code: int
) -> None:
    """Model failures reach the caller as a typed 5xx, not a traceback."""
    test_app = FastAPI()
    test_app.add_exception_handler(ModelClientError, model_error_handler)

    @test_app.get("/boom")
    def boom() -> None:
        raise error

    response = TestClient(test_app).get("/boom")
    assert response.status_code == status_code
    assert response.json() == {"detail": str(error)}


# Retries


@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ConnectTimeout])
def test_connection_never_opened_then_up_is_retried_after_backoff(
    failure: type[Exception],
) -> None:
    handler, calls = answers(failure, completion("ok"))
    time = FakeTime()

    assert make_client(handler, time).generate("hi") == "ok"
    assert len(calls) == 2
    assert time.sleeps == [1.0]


@pytest.mark.parametrize("status", [429, 502, 503])
def test_server_not_ready_is_retried_with_growing_waits(status: int) -> None:
    handler, calls = answers(httpx2.Response(status), httpx2.Response(status))
    time = FakeTime()

    with pytest.raises(ModelUnavailableError, match=f"HTTP {status}"):
        make_client(handler, time).generate("hi")
    assert len(calls) == 4  # one try and LLM_MAX_RETRIES=3
    assert time.sleeps == [1.0, 2.0, 4.0]


def test_full_jitter_never_waits_longer_than_the_backoff() -> None:
    handler, _ = answers(httpx2.Response(503), completion("ok"))
    picked: list[tuple[float, float]] = []
    settings = ModelSettings(_env_file=None, provider="openai")
    client = OpenAICompatibleClient(
        settings,
        transport=httpx2.MockTransport(handler),
        sleep=lambda s: None,
        jitter=lambda low, high: picked.append((low, high)) or low,
    )

    client.generate("hi")

    assert picked == [(0.0, 1.0)]


def test_retry_after_is_honored() -> None:
    handler, calls = answers(
        httpx2.Response(429, headers={"Retry-After": "3"}), completion("ok")
    )
    time = FakeTime()

    assert make_client(handler, time).generate("hi") == "ok"
    assert time.sleeps == [3.0]


def test_retry_after_past_the_cap_gives_up_at_once() -> None:
    handler, calls = answers(httpx2.Response(503, headers={"Retry-After": "60"}))
    time = FakeTime()

    with pytest.raises(ModelUnavailableError):
        make_client(handler, time).generate("hi")
    assert len(calls) == 1
    assert time.sleeps == []


@pytest.mark.parametrize(
    "first",
    [httpx2.Response(500), httpx2.Response(400), httpx2.ReadTimeout],
)
def test_errors_after_generation_may_have_started_are_not_retried(
    first: httpx2.Response | type[Exception],
) -> None:
    handler, calls = answers(first, completion("ok"))

    with pytest.raises(ModelClientError):
        make_client(handler).generate("hi")
    assert len(calls) == 1


def test_no_retries_when_turned_off() -> None:
    handler, calls = answers(httpx2.Response(503), completion("ok"))

    with pytest.raises(ModelUnavailableError):
        make_client(handler, max_retries=0).generate("hi")
    assert len(calls) == 1


def test_retries_stop_when_the_time_budget_is_spent() -> None:
    """LLM_TIMEOUT_S bounds the whole call, so a retry never runs past it."""
    handler, calls = answers(httpx2.Response(503, headers={"Retry-After": "4"}))
    time = FakeTime()

    with pytest.raises(ModelUnavailableError):
        make_client(handler, time, timeout_s=6).generate("hi")
    assert time.sleeps == [4.0]  # a second 4 s wait would end past 6 s
    assert len(calls) == 2


def test_ping_is_not_retried() -> None:
    """/ready reports what it sees right now."""
    handler, calls = answers(httpx2.ConnectError)

    with pytest.raises(ModelUnavailableError):
        make_client(handler).ping()
    assert len(calls) == 1
