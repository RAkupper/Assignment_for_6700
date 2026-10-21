"""Test doubles for the model client."""

import time


class FakeModelClient:
    """In-memory model client that records prompts and can simulate slow inference."""

    def __init__(
        self,
        answer: str = "fake answer",
        delay_s: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.delay_s = delay_s
        self.error = error
        self.prompts: list[str] = []
        self.closed = False

    def generate(self, prompt: str) -> str:
        """Record the prompt, wait like a slow model if asked, and return the answer.

        Raises:
            Exception: ``error``, when one was given, to simulate a failing backend.
        """
        self.prompts.append(prompt)
        if self.delay_s:
            time.sleep(self.delay_s)  # blocks the calling thread like a sync model call
        if self.error is not None:
            raise self.error
        return self.answer

    def close(self) -> None:
        """Mark the client closed."""
        self.closed = True
