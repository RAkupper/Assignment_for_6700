"""Model client interface shared by the service and its tests."""

from typing import Protocol, runtime_checkable

from assignment_1.config import Settings


@runtime_checkable
class ModelClient(Protocol):
    """Anything that turns a prompt into generated text."""

    def generate(self, prompt: str) -> str:
        """Return the model's completion for a prompt."""
        ...

    def close(self) -> None:
        """Release connections or other resources held by the client."""
        ...


class EchoModelClient:
    """Placeholder client that returns the prompt until the real model client lands."""

    def generate(self, prompt: str) -> str:
        """Return the prompt unchanged."""
        return prompt

    def close(self) -> None:
        """Nothing to release."""


def build_model_client(settings: Settings) -> ModelClient:
    """Build the model client the service uses, from settings.

    Args:
        settings: service configuration; the real client reads its model settings here.

    Returns:
        ModelClient: the client created once at startup.
    """
    return EchoModelClient()
