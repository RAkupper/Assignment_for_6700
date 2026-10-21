"""Tests for the model settings and the OpenAI-compatible client."""

import json
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from assignment_1.app import model_error_handler
from assignment_1.config import ModelSettings, Settings
from assignment_1.model_client import (
    EchoModelClient,
    ModelClient,
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
    OpenAICompatibleClient,
    build_model_client,
)

Handler = Callable[[httpx2.Request], httpx2.Response]


def make_client(handler: Handler, **overrides: object) -> OpenAICompatibleClient:
    """Build a client whose HTTP calls go to an in-memory handler."""
    settings = ModelSettings(_env_file=None, provider="openai", **overrides)
    return OpenAICompatibleClient(settings, transport=httpx2.MockTransport(handler))


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
