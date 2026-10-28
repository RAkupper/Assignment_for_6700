import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from myapp.ingest.schema import Record, RecordMetadata

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def make_metadata(**overrides: object) -> RecordMetadata:
    fields: dict[str, object] = {
        "source": "ecfr",
        "provision_id": "8 CFR 214.2(f)(10)",
        "parent_id": "8 CFR 214.2(f)",
        "heading": "Practical training",
        "heading_path": "Part 214 > 214.2 > (f)",
        "authority": "regulation",
        "status": "in_force",
        "valid_from": date(2026, 1, 1),
        "url": "https://www.ecfr.gov/current/title-8/section-214.2#p-214.2(f)(10)",
        "retrieved_at": NOW,
        "visa_types": {"F-1"},
        "visa_types_rule": "8 CFR 214.2(f)",
    }
    return RecordMetadata.model_validate(fields | overrides)


def test_identity_is_stable_across_text_changes() -> None:
    meta = make_metadata()
    old = Record(metadata=meta, text="old text")
    new = Record(metadata=meta, text="new text")
    assert old.metadata.provision_id == new.metadata.provision_id
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


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_metadata(status="draft")


def test_authority_must_match_source() -> None:
    with pytest.raises(ValidationError, match="authority"):
        make_metadata(authority="guidance")  # eCFR is binding regulation


def test_proposed_rule_from_federal_register_is_accepted() -> None:
    meta = make_metadata(
        source="federal_register",
        provision_id="FR 2026-01234 sec. II",
        parent_id=None,
        authority="rulemaking",
        status="proposed",
        valid_from=None,
        url="https://www.federalregister.gov/d/2026-01234",
    )
    assert meta.status == "proposed"


def test_visa_types_need_the_rule_that_set_them() -> None:
    with pytest.raises(ValidationError, match="visa_types_rule"):
        make_metadata(visa_types_rule="")


def test_empty_visa_types_means_all_and_needs_no_rule() -> None:
    meta = make_metadata(visa_types=set(), visa_types_rule="")
    assert meta.visa_types == frozenset()


def test_provision_cannot_be_its_own_parent() -> None:
    with pytest.raises(ValidationError, match="parent"):
        make_metadata(parent_id="8 CFR 214.2(f)(10)")


def test_empty_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Record(metadata=make_metadata(), text="")


def test_json_round_trip_is_deterministic() -> None:
    record = Record(metadata=make_metadata(visa_types={"J-1", "F-1"}), text="x")
    data = json.loads(record.model_dump_json())
    assert data["metadata"]["visa_types"] == ["F-1", "J-1"]
    assert data["metadata"]["schema_version"] == 1
    assert "content_hash" in data
    data.pop("content_hash")  # computed, so not accepted back as input
    assert Record.model_validate(data) == record
