import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from myapp.ingest.schema import Record, RecordMetadata

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def make_metadata(**overrides: object) -> RecordMetadata:
    fields: dict[str, object] = {
        "source": "ecfr",
        "citation": "8 CFR 214.2(f)(10)",
        "title": "Practical training",
        "url": "https://www.ecfr.gov/current/title-8/section-214.2#p-214.2(f)(10)",
        "effective_date": date(2026, 1, 1),
        "retrieved_at": NOW,
        "visa_types": {"F-1"},
    }
    return RecordMetadata.model_validate(fields | overrides)


def test_id_is_stable_across_text_changes() -> None:
    meta = make_metadata()
    old = Record(metadata=meta, text="old text")
    new = Record(metadata=meta, text="new text")
    assert old.metadata.id == new.metadata.id == "ecfr:8 CFR 214.2(f)(10)"
    assert old.content_hash != new.content_hash


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_metadata(agency="DHS")


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_metadata(retrieved_at=datetime(2026, 10, 22))


def test_unknown_visa_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_metadata(visa_types={"H-1B"})


def test_empty_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Record(metadata=make_metadata(), text="")


def test_json_round_trip_is_deterministic() -> None:
    record = Record(metadata=make_metadata(visa_types={"J-1", "F-1"}), text="x")
    line = record.model_dump_json()
    data = json.loads(line)
    assert data["metadata"]["visa_types"] == ["F-1", "J-1"]
    assert data["metadata"]["schema_version"] == 1
    assert "id" in data["metadata"] and "content_hash" in data
    data["metadata"].pop("id")
    data.pop("content_hash")  # computed, so not accepted back as input
    assert Record.model_validate(data) == record
