from datetime import date
from hashlib import sha256
from typing import Final, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    field_serializer,
    model_validator,
)

SCHEMA_VERSION: Final = 1  # bump on any breaking change to Record

SourceName = Literal["ecfr", "federal_register", "uscis_policy_manual", "sevp"]
VisaType = Literal["F-1", "F-2", "J-1", "J-2", "M-1", "M-2"]
Authority = Literal["regulation", "agency_policy", "guidance", "rulemaking"]
Status = Literal["in_force", "pending", "proposed", "superseded", "withdrawn"]

AUTHORITY_BY_SOURCE: Final[dict[SourceName, Authority]] = {  # docs/ingestion.md
    "ecfr": "regulation",
    "uscis_policy_manual": "agency_policy",
    "sevp": "guidance",
    "federal_register": "rulemaking",
}


class RawDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName
    url: HttpUrl
    retrieved_at: AwareDatetime
    content_type: str  # e.g. application/xml, text/html
    body: bytes
    status_code: int = 200
    source_version: str | None = None  # eCFR: the date the snapshot represents


class RecordMetadata(BaseModel):
    """One citable provision. Its identity, not its text, so it is stable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    source: SourceName
    provision_id: str = Field(min_length=1)  # the citation: 8 CFR 214.2(f)(10)(ii)
    parent_id: str | None = None  # enclosing provision; None at the top
    heading: str
    heading_path: str = ""  # "Part 214 > 214.2 > (f)"; kept out of the text
    authority: Authority
    status: Status
    valid_from: date | None  # None when the source gives no date
    url: HttpUrl
    retrieved_at: AwareDatetime
    visa_types: frozenset[VisaType] = frozenset()  # empty means applies to all
    visa_types_rule: str = ""  # which citation rule assigned visa_types

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.authority != AUTHORITY_BY_SOURCE[self.source]:
            raise ValueError(
                f"{self.source} records must have authority "
                f"{AUTHORITY_BY_SOURCE[self.source]}"
            )
        if self.visa_types and not self.visa_types_rule:
            raise ValueError("visa_types needs the visa_types_rule that set it")
        if self.parent_id == self.provision_id:
            raise ValueError("a provision cannot be its own parent")
        return self

    @field_serializer("visa_types")
    def _sorted(self, value: frozenset[VisaType]) -> list[VisaType]:
        return sorted(value)  # deterministic JSONL, clean diffs


class Record(BaseModel):
    """What parse() yields. The catalog assigns version and valid_to on write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: RecordMetadata
    text: str = Field(min_length=1)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_hash(self) -> str:
        return sha256(self.text.encode()).hexdigest()  # new version when it changes
