"""Test doubles for the model client."""

import time


class FakeModelClient:
    """In-memory model client that records prompts and can simulate slow inference."""

    def __init__(self, answer: str = "fake answer", delay_s: float = 0.0) -> None:
        self.answer = answer
        self.delay_s = delay_s
        self.prompts: list[str] = []
        self.closed = False

    def generate(self, prompt: str) -> str:
        """Record the prompt, wait like a slow model if asked, and return the answer."""
        self.prompts.append(prompt)
        if self.delay_s:
            time.sleep(self.delay_s)  # blocks the calling thread like a sync model call
        return self.answer

    def close(self) -> None:
        """Mark the client closed."""
        self.closed = True
