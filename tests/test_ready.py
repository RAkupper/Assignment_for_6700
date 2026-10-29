"""Tests for the readiness probe and the client ping behind it."""

from collections.abc import Callable, Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient

from myapp.app import ReadyResponse, app, get_model_client
from myapp.config import ModelSettings
from myapp.model_client import (
    EchoModelClient,
    ModelClientError,
    ModelTimeoutError,
    ModelUnavailableError,
    OpenAICompatibleClient,
)
from tests.fakes import FakeModelClient

client = TestClient(app)

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def use_fake() -> Iterator[Callable[[FakeModelClient], FakeModelClient]]:
    """Install a fake model client for one test and remove it afterwards."""

    def install(fake: FakeModelClient) -> FakeModelClient:
        app.dependency_overrides[get_model_client] = lambda: fake
        return fake

    yield install
    app.dependency_overrides.clear()


def make_client(handler: Handler) -> OpenAICompatibleClient:
    """Build a client whose HTTP calls go to an in-memory handler."""
    settings = ModelSettings(_env_file=None, provider="openai")
    return OpenAICompatibleClient(settings, transport=httpx2.MockTransport(handler))


# Endpoint


def test_ready_reports_ready_when_model_answers(
    use_fake: Callable[[FakeModelClient], FakeModelClient],
) -> None:
    """A reachable model backend gives 200 and one probe."""
    fake = use_fake(FakeModelClient())

    response = client.get("/ready")

    assert response.status_code == 200
    assert ReadyResponse.model_validate(response.json()).status == "ready"
    assert fake.pings == 1


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ModelTimeoutError("timed out"), 504),
        (ModelUnavailableError("unreachable"), 503),
        (ModelClientError("bad reply"), 502),
    ],
)
def test_ready_maps_model_errors(
    use_fake: Callable[[FakeModelClient], FakeModelClient],
    error: Exception,
    status_code: int,
) -> None:
    """A failing backend is reported with the same status mapping as /generate."""
    use_fake(FakeModelClient(ping_error=error))

    response = client.get("/ready")

    assert response.status_code == status_code
    assert response.json() == {"detail": str(error)}


def test_health_stays_up_when_the_model_is_down(
    use_fake: Callable[[FakeModelClient], FakeModelClient],
) -> None:
    """Liveness does not depend on the model server, so a restart is not triggered."""
    use_fake(FakeModelClient(ping_error=ModelUnavailableError("unreachable")))

    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200


# Client ping


def test_ping_lists_models_on_the_server() -> None:
    """The probe is a cheap GET on /models, not a generation."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": [{"id": "qwen2.5:1.5b-instruct"}]})

    make_client(handler).ping()

    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/models")]


@pytest.mark.parametrize("listed", [[], [{"id": "some-other-model"}]])
def test_ping_rejects_missing_configured_model(listed: list[object]) -> None:
    with pytest.raises(ModelUnavailableError, match="configured generation model"):
        make_client(lambda request: httpx2.Response(200, json={"data": listed})).ping()


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": [{}]}, []])
def test_ping_rejects_malformed_model_list(body: object) -> None:
    with pytest.raises(ModelClientError, match="unexpected model listing"):
        make_client(lambda request: httpx2.Response(200, json=body)).ping()


def test_ping_rejects_non_json_reply() -> None:
    with pytest.raises(ModelClientError, match="unexpected model listing"):
        make_client(lambda request: httpx2.Response(200, text="not JSON")).ping()


def test_ping_maps_http_errors_to_unavailable() -> None:
    """An error status from the server means the backend is not ready."""
    with pytest.raises(ModelUnavailableError, match="HTTP 500"):
        make_client(lambda request: httpx2.Response(500)).ping()


def test_ping_maps_connection_failures_to_unavailable() -> None:
    """A refused connection means the backend is not ready."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(ModelUnavailableError, match="could not reach"):
        make_client(refuse).ping()


def test_ping_maps_timeouts_to_timeout_error() -> None:
    """A slow answer from the server is reported as a timeout."""

    def stall(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    with pytest.raises(ModelTimeoutError):
        make_client(stall).ping()


def test_echo_client_is_always_ready() -> None:
    """The placeholder client has no backend to probe."""
    EchoModelClient().ping()
