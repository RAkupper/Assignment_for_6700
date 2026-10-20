"""Test the Docker health check"""

from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

from assignment_1.healthcheck import check_health, main


class FakeResponse(BytesIO):
    """Act like a successful HTTP response"""

    status = 200


def test_healthy_response() -> None:
    response = FakeResponse(b'{"status": "ok"}')

    with patch("assignment_1.healthcheck.urlopen", return_value=response):
        assert check_health() is True


def test_unhealthy_response() -> None:
    response = FakeResponse(b'{"status": "error"}')

    with patch("assignment_1.healthcheck.urlopen", return_value=response):
        assert check_health() is False


def test_invalid_json() -> None:
    response = FakeResponse(b"not JSON")

    with patch("assignment_1.healthcheck.urlopen", return_value=response):
        assert check_health() is False


def test_connection_failure() -> None:
    error = URLError("Server unavailable")

    with patch("assignment_1.healthcheck.urlopen", side_effect=error):
        assert check_health() is False


def test_success_exit_code() -> None:
    with patch("assignment_1.healthcheck.check_health", return_value=True):
        assert main() == 0


def test_failure_exit_code() -> None:
    with patch("assignment_1.healthcheck.check_health", return_value=False):
        assert main() == 1
