"""Model client interface shared by the service and its tests."""

from typing import Any, Protocol, runtime_checkable

import httpx2

from assignment_1.config import ModelSettings, Settings


@runtime_checkable
class ModelClient(Protocol):
    """Anything that turns a prompt into generated text."""

    def generate(self, prompt: str) -> str:
        """Return the model's completion for a prompt."""
        ...

    def ping(self) -> None:
        """Return if the model backend answers; raise a ModelClientError if not."""
        ...

    def close(self) -> None:
        """Release connections or other resources held by the client."""
        ...


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
    ) -> None:
        self.settings = settings
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

        Raises:
            ModelTimeoutError: the server did not answer within ``LLM_TIMEOUT_S``.
            ModelUnavailableError: the server was unreachable or returned an error.
            ModelClientError: the response did not have the expected shape.
        """
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
            "stream": False,
        }
        try:
            response = self._http.post("chat/completions", json=payload)
            response.raise_for_status()
        except httpx2.TimeoutException as exc:
            raise ModelTimeoutError(
                f"model server timed out after {self.settings.timeout_s}s"
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
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelClientError("unexpected response from model server") from exc
        if not isinstance(content, str):
            raise ModelClientError("model server returned non-text content")
        return content

    def ping(self) -> None:
        """Check that the model server answers, using the cheap ``/models`` listing.

        The connect timeout bounds the whole call, so a readiness probe never waits
        as long as a generation does.

        Raises:
            ModelTimeoutError: no answer within ``LLM_CONNECT_TIMEOUT_S``.
            ModelUnavailableError: the server was unreachable or returned an error.
        """
        timeout = self.settings.connect_timeout_s
        try:
            self._http.get("models", timeout=timeout).raise_for_status()
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
