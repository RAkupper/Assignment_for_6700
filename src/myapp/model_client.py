"""Model client interface shared by the service and its tests."""

import random
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import httpx2
from loguru import logger

from myapp.config import ModelSettings, Settings
from myapp.observability import ANSWER_TOKENS, MODEL_FAILURES, MODEL_SECONDS


@runtime_checkable
class ModelClient(Protocol):
    """Anything that turns a prompt into generated text."""

    def generate(self, prompt: str) -> str:
        """Return the model's completion for a prompt."""
        ...

    def ping(self) -> None:
        """Return if the configured model is advertised; raise if unavailable."""
        ...

    def close(self) -> None:
        """Release connections or other resources held by the client."""
        ...


RETRYABLE = {429, 502, 503}  # rejected before generating: llama.cpp loading or full


class ModelClientError(RuntimeError):
    """The model backend could not produce an answer."""


class ModelTimeoutError(ModelClientError):
    """The model backend did not answer within the configured timeout."""


class ModelUnavailableError(ModelClientError):
    """The model backend could not be reached or returned an error."""


class EchoModelClient:
    """Placeholder client that returns the prompt; needs no model server."""

    def generate(self, prompt: str) -> str:
        """Return the prompt unchanged."""
        return prompt

    def ping(self) -> None:
        """Nothing to reach, so always ready."""

    def close(self) -> None:
        """Nothing to release."""


class OpenAICompatibleClient:
    """Client for any server that speaks the OpenAI ``/chat/completions`` API.

    This covers Ollama and vLLM running in their own container as well as hosted
    providers, so where the model runs is decided by ``LLM_BASE_URL`` alone. The
    client is synchronous on purpose: it matches the ``ModelClient`` protocol, and
    routes run it off the event loop (``asyncio.to_thread`` or a sync handler).
    """

    def __init__(
        self,
        settings: ModelSettings,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        headers = {"Content-Type": "application/json"}
        if settings.api_key is not None:
            headers["Authorization"] = f"Bearer {settings.api_key.get_secret_value()}"
        self._http = httpx2.Client(
            base_url=str(settings.base_url),
            headers=headers,
            timeout=httpx2.Timeout(
                settings.timeout_s, connect=settings.connect_timeout_s
            ),
            transport=transport,
        )

    def generate(self, prompt: str) -> str:
        """Send one user message and return the model's reply text.

        Records the call's time and token count in the metrics and logs one line
        with the model, duration and tokens, never the prompt or the reply.

        This is the one place model calls are retried. A connection that was refused
        or never opened, or a 429/502/503, means nothing was generated, so it is
        retried up to ``LLM_MAX_RETRIES`` times with exponential backoff and full
        jitter, honoring ``Retry-After``. A read timeout is not retried: the model may
        still be working, and a second request would double the load.
        ``LLM_TIMEOUT_S`` bounds the whole call, retries included.

        Raises:
            ModelTimeoutError: no answer within ``LLM_TIMEOUT_S``.
            ModelUnavailableError: the server was unreachable or returned an error.
            ModelClientError: the response did not have the expected shape.
        """
        started = self._clock()
        try:
            text, tokens = self._post(prompt)
        except ModelClientError as exc:
            MODEL_FAILURES.labels(_failure_kind(exc)).inc()
            raise
        seconds = self._clock() - started
        MODEL_SECONDS.observe(seconds)
        if tokens is not None:
            ANSWER_TOKENS.observe(tokens)
        logger.bind(
            model=self.settings.model,
            duration_ms=round(seconds * 1000),
            completion_tokens=tokens,
        ).info("model call")
        return text

    def _post(self, prompt: str) -> tuple[str, int | None]:
        """Call the server with retries; return the reply and its completion tokens."""
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
            "stream": False,
        }
        deadline = self._clock() + self.settings.timeout_s
        for attempt in range(self.settings.max_retries + 1):
            remaining = deadline - self._clock()
            timeout = httpx2.Timeout(
                remaining, connect=min(self.settings.connect_timeout_s, remaining)
            )
            response: httpx2.Response | None = None
            try:
                response = self._http.post(
                    "chat/completions", json=payload, timeout=timeout
                )
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:  # nothing sent
                failure: ModelClientError = ModelUnavailableError(
                    f"could not reach model server at {self.settings.base_url}"
                )
                cause: Exception | None = exc
            except httpx2.TimeoutException as exc:
                raise ModelTimeoutError(
                    f"model server timed out after {self.settings.timeout_s}s"
                ) from exc
            except httpx2.RequestError as exc:
                raise ModelUnavailableError(
                    f"could not reach model server at {self.settings.base_url}"
                ) from exc
            else:
                if not response.is_error:
                    return self._content(response)
                failure = ModelUnavailableError(
                    f"model server returned HTTP {response.status_code}"
                )
                cause = None  # an HTTP status, not an exception
                if response.status_code not in RETRYABLE:
                    raise failure
            wait = self._backoff(attempt, response, deadline)
            if wait is None:
                raise failure from cause
            logger.warning("{}; retry {} in {:.1f}s", failure, attempt + 1, wait)
            self._sleep(wait)
        raise AssertionError("unreachable")  # the last attempt returns or raises

    def _backoff(
        self, attempt: int, response: httpx2.Response | None, deadline: float
    ) -> float | None:
        """Seconds to wait before the next try, or None to give up now."""
        if attempt >= self.settings.max_retries:
            return None
        cap = self.settings.retry_max_wait_s
        asked = response.headers.get("Retry-After", "") if response else ""
        if asked.isdigit():
            wait = float(asked)  # the server said when; over the cap means give up
        else:
            wait = self._jitter(0.0, min(cap, 2.0**attempt))  # full jitter
        if wait > cap or self._clock() + wait + 1.0 >= deadline:
            return None  # no time left for a useful try
        return wait

    def _content(self, response: httpx2.Response) -> tuple[str, int | None]:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelClientError("unexpected response from model server") from exc
        if not isinstance(content, str):
            raise ModelClientError("model server returned non-text content")
        usage = body.get("usage") if isinstance(body, dict) else None
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        return content, tokens if isinstance(tokens, int) else None

    def ping(self) -> None:
        """Check the configured model is present in the cheap ``/models`` listing.

        The request uses the short connect-timeout setting instead of the longer
        generation timeout. It does not perform model inference.

        Raises:
            ModelTimeoutError: no answer within ``LLM_CONNECT_TIMEOUT_S``.
            ModelUnavailableError: the server was unreachable or returned an error.
        """
        timeout = self.settings.connect_timeout_s
        try:
            response = self._http.get("models", timeout=timeout)
            response.raise_for_status()
        except httpx2.TimeoutException as exc:
            raise ModelTimeoutError(
                f"model server did not answer within {timeout}s"
            ) from exc
        except httpx2.HTTPStatusError as exc:
            raise ModelUnavailableError(
                f"model server returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx2.RequestError as exc:
            raise ModelUnavailableError(
                f"could not reach model server at {self.settings.base_url}"
            ) from exc

        try:
            listed = response.json()["data"]
            if not isinstance(listed, list) or not all(
                isinstance(item, dict) and isinstance(item.get("id"), str)
                for item in listed
            ):
                raise ValueError("invalid model listing")
            names = {item["id"] for item in listed}
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelClientError(
                "unexpected model listing from model server"
            ) from exc
        if self.settings.model not in names:
            raise ModelUnavailableError(
                "configured generation model is not available on the model server"
            )

    def close(self) -> None:
        """Close the pooled HTTP connections."""
        self._http.close()


def build_model_client(settings: Settings) -> ModelClient:
    """Build the model client the service uses, from settings.

    Args:
        settings: service configuration; ``settings.llm`` selects the backend.

    Returns:
        ModelClient: the client created once at startup.
    """
    if settings.llm.provider == "openai":
        return OpenAICompatibleClient(settings.llm)
    return EchoModelClient()


def _failure_kind(exc: ModelClientError) -> str:
    """Label for the failure counter: timeout, unavailable or bad_response."""
    if isinstance(exc, ModelTimeoutError):
        return "timeout"
    if isinstance(exc, ModelUnavailableError):
        return "unavailable"
    return "bad_response"
