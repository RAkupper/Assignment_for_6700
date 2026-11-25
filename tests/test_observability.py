"""Request IDs, JSON log lines and the Prometheus metrics endpoint."""

import json

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from myapp.app import app
from myapp.observability import new_request_id, route_label, setup_logging


def sample(name: str, **labels: str) -> float:
    """Current value of one metric sample, 0 when it has not been recorded yet."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def log_lines(err: str) -> list[dict[str, object]]:
    """The JSON objects among captured stderr lines."""
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


def test_each_response_carries_a_new_request_id() -> None:
    client = TestClient(app)
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]
    assert len(first) == 32 and first != second


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("abc-123", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("x y", False),
        ("", False),
    ],
)
def test_a_callers_request_id_is_kept_only_when_safe_to_log(
    sent: str, kept: bool
) -> None:
    assert (new_request_id(sent) == sent) is kept


def test_routes_are_labelled_by_template_not_raw_path() -> None:
    assert route_label("/ask", "/ask") == "/ask"
    assert route_label(None, "/assets/app.js") == "/assets"
    assert route_label(None, "/wp-login.php") == "unmatched"


def test_metrics_count_requests_by_route_and_status() -> None:
    client = TestClient(app)
    before = sample("app_requests_total", route="/health", status="200")
    missing = sample("app_requests_total", route="unmatched", status="404")
    client.get("/health")
    client.get("/no/such/path")
    body = client.get("/metrics").text
    assert sample("app_requests_total", route="/health", status="200") == before + 1
    assert sample("app_requests_total", route="unmatched", status="404") == missing + 1
    assert "app_request_duration_seconds_bucket" in body
    assert "app_ask_slo_events_total" in body


def test_each_request_logs_one_json_line_with_its_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(json_lines=True)
    capsys.readouterr()
    response = TestClient(app).get("/", headers={"X-Request-ID": "trace-me"})
    lines = [x for x in log_lines(capsys.readouterr().err) if x["message"] == "request"]
    assert response.headers["X-Request-ID"] == "trace-me"
    assert len(lines) == 1
    assert lines[0]["request_id"] == "trace-me"
    assert lines[0]["route"] == "/" and lines[0]["status"] == 200
    assert isinstance(lines[0]["duration_ms"], int)


def test_health_and_metrics_are_counted_but_not_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(json_lines=True)
    capsys.readouterr()
    client = TestClient(app)
    client.get("/health")
    client.get("/metrics")
    assert not [
        x for x in log_lines(capsys.readouterr().err) if x["message"] == "request"
    ]
