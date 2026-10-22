from datetime import date
from hashlib import sha256
from typing import Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_serializer,
)

SCHEMA_VERSION: Final = 1  # bump on any breaking change to Record

SourceName = Literal["ecfr", "federal_register", "uscis_policy_manual", "sevp"]
VisaType = Literal["F-1", "F-2", "J-1", "J-2", "M-1", "M-2"]


class RawDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName
    url: HttpUrl
    retrieved_at: AwareDatetime
    content_type: str  # e.g. application/xml, text/html
    body: bytes


class RecordMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    source: SourceName
    citation: str = Field(min_length=1)  # e.g. 8 CFR 214.2(f)(10)
    title: str
    url: HttpUrl
    effective_date: date | None  # None when the source gives no date
    retrieved_at: AwareDatetime
    visa_types: frozenset[VisaType] = frozenset()  # empty means applies to all

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        return f"{self.source}:{self.citation}"  # stable across re-pulls

    @field_serializer("visa_types")
    def _sorted(self, value: frozenset[VisaType]) -> list[VisaType]:
        return sorted(value)  # deterministic JSONL, clean diffs


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: RecordMetadata
    text: str = Field(min_length=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        return sha256(self.text.encode()).hexdigest()  # detects changed text
