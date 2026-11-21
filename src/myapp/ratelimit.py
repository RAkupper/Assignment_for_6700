"""Per-client request limits over a sliding one-minute window."""

import math
import time
from collections import deque
from collections.abc import Callable
from threading import Lock

WINDOW_S = 60.0
MAX_CLIENTS = 10_000  # past this, forget clients idle for a whole window


class SlidingWindowLimiter:
    """Remember when each client sent its requests in the last minute.

    State lives in memory, so it is per process and starts empty after a restart.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._seen: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, client: str, limit: int) -> int:
        """Count one request from ``client`` if it is under ``limit``.

        Returns:
            int: 0 when the request is allowed, otherwise the whole seconds until
            the oldest request in the window expires, for ``Retry-After``.
        """
        now = self._clock()
        with self._lock:
            if len(self._seen) > MAX_CLIENTS:
                self._forget_idle(now)
            times = self._seen.setdefault(client, deque())
            while times and now - times[0] >= WINDOW_S:
                times.popleft()
            if len(times) >= limit:
                return max(1, math.ceil(WINDOW_S - (now - times[0])))
            times.append(now)
            return 0

    def clear(self) -> None:
        """Forget every client."""
        with self._lock:
            self._seen.clear()

    def _forget_idle(self, now: float) -> None:
        idle = [c for c, t in self._seen.items() if not t or now - t[-1] >= WINDOW_S]
        for client in idle:
            del self._seen[client]
