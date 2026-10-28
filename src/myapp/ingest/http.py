"""A polite HTTP client for federal sites: rate limited, retried, identified."""

import time
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx2

from myapp.config import IngestSettings

RETRYABLE = {429, 500, 502, 503, 504}


class PoliteClient:
    """Sequential GETs with a minimum interval per host and backoff on 429 and 5xx.

    Returns the final response even when it is an error, so the caller can record
    the status code. Raises only when the network itself keeps failing.
    """

    def __init__(
        self,
        settings: IngestSettings,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = settings.min_request_interval_s
        self.retries = settings.max_retries
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._http = httpx2.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "gzip"},
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            transport=transport,
        )

    def _wait_turn(self, host: str) -> None:
        if host in self._last:
            remaining = self.interval - (self._clock() - self._last[host])
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()

    def get(self, url: str) -> httpx2.Response:
        host = urlsplit(url).netloc
        for attempt in range(self.retries + 1):
            self._wait_turn(host)
            try:
                response = self._http.get(url)
            except httpx2.TransportError:
                if attempt == self.retries:
                    raise
                self._sleep(2.0**attempt)
                continue
            if response.status_code not in RETRYABLE or attempt == self.retries:
                return response
            self._sleep(_retry_after(response) or 2.0**attempt)
        raise AssertionError("unreachable")  # the loop always returns or raises

    def close(self) -> None:
        self._http.close()


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else None
