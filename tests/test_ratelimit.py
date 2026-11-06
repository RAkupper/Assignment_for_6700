"""Tests for per-client rate limits and the no-store header on private routes."""

import pytest
from fastapi.testclient import TestClient

from myapp import app as app_module
from myapp.app import app
from myapp.ratelimit import MAX_CLIENTS, WINDOW_S, SlidingWindowLimiter


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_allows_up_to_the_limit_then_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(clock)

    assert [limiter.check("a", 3) for _ in range(3)] == [0, 0, 0]
    clock.now += 20
    assert limiter.check("a", 3) == 40  # the first request expires 40 s from now


def test_limiter_window_slides() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(clock)
    limiter.check("a", 2)
    clock.now += 30
    limiter.check("a", 2)

    clock.now += WINDOW_S - 30  # the first request just left the window
    assert limiter.check("a", 2) == 0
    assert limiter.check("a", 2) > 0


def test_limiter_counts_clients_separately() -> None:
    limiter = SlidingWindowLimiter(Clock())
    limiter.check("a", 1)

    assert limiter.check("a", 1) > 0
    assert limiter.check("b", 1) == 0


def test_rejected_requests_do_not_extend_the_wait() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(clock)
    limiter.check("a", 1)
    for _ in range(5):
        limiter.check("a", 1)

    clock.now += WINDOW_S
    assert limiter.check("a", 1) == 0


def test_limiter_forgets_idle_clients_once_it_holds_many() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(clock)
    for n in range(MAX_CLIENTS + 1):
        limiter.check(f"scanner-{n}", 5)
    clock.now += WINDOW_S

    limiter.check("new", 5)

    assert set(limiter._seen) == {"new"}


def test_retrieve_answers_429_with_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit runs before the route, so it applies even while generation is off."""
    monkeypatch.setattr(app_module.settings, "rate_limit_retrieve_per_minute", 2)
    client = TestClient(app)

    statuses = [
        client.post("/retrieve", json={"question": "OPT"}).status_code for _ in range(2)
    ]
    limited = client.post("/retrieve", json={"question": "OPT"})

    assert statuses == [503, 503]
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= WINDOW_S
    assert limited.json() == {"detail": "Too many requests: at most 2 a minute"}


def test_ask_limit_is_per_client_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.settings, "rate_limit_ask_per_minute", 1)
    first = TestClient(app, client=("203.0.113.1", 50000))
    second = TestClient(app, client=("203.0.113.2", 50000))

    first.post("/ask", json={"question": "OPT"})

    assert first.post("/ask", json={"question": "OPT"}).status_code == 429
    assert second.post("/ask", json={"question": "OPT"}).status_code == 503


def test_ask_and_retrieve_have_separate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module.settings, "rate_limit_ask_per_minute", 1)
    client = TestClient(app)
    client.post("/ask", json={"question": "OPT"})

    assert client.post("/retrieve", json={"question": "OPT"}).status_code == 503


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/ask", {"question": "Can I work on campus?"}),
        ("/retrieve", {"question": "Can I work on campus?"}),
        ("/ask", {"question": ""}),  # a 422 echoes the input back
    ],
)
def test_private_routes_are_never_cached(path: str, body: dict[str, str]) -> None:
    response = TestClient(app).post(path, json=body)

    assert response.headers["Cache-Control"] == "no-store"


def test_public_routes_keep_default_caching() -> None:
    assert "Cache-Control" not in TestClient(app).get("/health").headers
