"""The interface every data source implements."""

from collections.abc import Iterator
from typing import Protocol

from myapp.ingest.schema import RawDocument, Record, SourceName


class Source(Protocol):
    """Fetch, validate and parse one kind of document."""

    name: SourceName

    def fetch(self) -> Iterator[RawDocument]:
        """Download raw responses, one at a time. Network only."""

    def validate(self, raw: RawDocument) -> str | None:
        """The reason to reject a response, or None to accept it."""

    def parse(self, raw: RawDocument) -> Iterator[Record]:
        """Turn one accepted response into records. Pure, no network."""
