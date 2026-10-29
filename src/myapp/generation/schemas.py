"""API request and answer shapes for retrieval-grounded generation."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from myapp.ingest.schema import SourceName, VisaType
from myapp.vectordb.search import Hit


class QuestionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    k: int | None = Field(default=None, ge=1, le=10)
    as_of: date | None = None
    visa_type: VisaType | None = None
    sources: list[SourceName] | None = None
    include_proposed: bool = False


class Answer(BaseModel):
    answer: str
    # Citation [1] corresponds to sources[0]; text is the excerpt sent to the LLM.
    sources: list[Hit]
    status: Literal["answered", "insufficient_evidence"] = "answered"
