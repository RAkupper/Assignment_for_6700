from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from myapp.config import IngestSettings


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here, so only the environment is read


def test_defaults_cover_baseline_parts() -> None:
    settings = IngestSettings()
    assert settings.ecfr_parts == ["8 CFR 214", "8 CFR 274a", "22 CFR 41", "22 CFR 62"]
    assert settings.data_dir == Path("data")


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_DATA_DIR", "/data")
    monkeypatch.setenv("INGEST_ECFR_PARTS", '["8 CFR 214"]')
    monkeypatch.setenv("INGEST_FEDERAL_REGISTER_SINCE", "2024-06-01")
    settings = IngestSettings()
    assert settings.data_dir == Path("/data")
    assert settings.ecfr_parts == ["8 CFR 214"]
    assert settings.federal_register_since == date(2024, 6, 1)


def test_dotenv_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("INGEST_MAX_RETRIES=5\n")
    assert IngestSettings().max_retries == 5


@pytest.mark.parametrize("value", ['["8 CFR"]', '["CFR 214"]', '["8 cfr 214"]'])
def test_malformed_part_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("INGEST_ECFR_PARTS", value)
    with pytest.raises(ValidationError):
        IngestSettings()


def test_duplicate_parts_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_ECFR_PARTS", '["8 CFR 214", "8 CFR 214"]')
    with pytest.raises(ValidationError, match="duplicates"):
        IngestSettings()


def test_empty_parts_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_ECFR_PARTS", "[]")
    with pytest.raises(ValidationError):
        IngestSettings()


def test_negative_interval_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INGEST_MIN_REQUEST_INTERVAL_S", "-1")
    with pytest.raises(ValidationError):
        IngestSettings()
