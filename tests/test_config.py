# Imports
import pytest
from pydantic import ValidationError

from myapp.app import app, settings
from myapp.config import Settings


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read environment variables into typed settings"""
    monkeypatch.setenv("APP_NAME", "example-service")
    monkeypatch.setenv("APP_DEBUG", "true")
    settings = Settings(_env_file=None)
    assert settings.name == "example-service"
    assert settings.debug is True


def test_settings_reject_debug_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject debug mode in production"""
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_DEBUG", "true")
    with pytest.raises(ValidationError, match="APP_DEBUG must be false"):
        Settings(_env_file=None)


def test_app_is_wired_to_settings() -> None:
    """The running FastAPI app takes its title and debug flag from Settings."""
    assert app.title == settings.name
    assert app.debug == settings.debug


def test_admin_key_defaults_to_unset() -> None:
    """No key means the admin routes are off"""
    assert Settings(_env_file=None).admin_api_key is None


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_admin_key_is_unset(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """APP_ADMIN_API_KEY= left empty in .env turns admin off instead of failing"""
    monkeypatch.setenv("APP_ADMIN_API_KEY", value)
    assert Settings(_env_file=None).admin_api_key is None


def test_short_admin_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guessable key stops the app at startup"""
    monkeypatch.setenv("APP_ADMIN_API_KEY", "password123")
    with pytest.raises(ValidationError, match="at least 32 characters") as error:
        Settings(_env_file=None)
    assert "password123" not in str(error.value)


def test_admin_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key never shows up when settings are printed or logged"""
    key = "k" * 43
    monkeypatch.setenv("APP_ADMIN_API_KEY", key)
    settings = Settings(_env_file=None)
    assert settings.admin_api_key is not None
    assert settings.admin_api_key.get_secret_value() == key
    assert key not in repr(settings)


@pytest.mark.parametrize(
    "name", ["APP_RATE_LIMIT_ASK_PER_MINUTE", "APP_RATE_LIMIT_RETRIEVE_PER_MINUTE"]
)
def test_rate_limits_must_allow_a_request(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A limit of 0 would refuse everyone, so the app refuses to start"""
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
