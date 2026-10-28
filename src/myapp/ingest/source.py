from collections.abc import Iterator
from typing import Protocol

from myapp.ingest.schema import RawDocument, Record, SourceName


class Source(Protocol):
    name: SourceName

    def fetch(self) -> Iterator[RawDocument]: ...  # network only, one at a time

    def validate(self, raw: RawDocument) -> str | None: ...  # reason to reject

    def parse(self, raw: RawDocument) -> Iterator[Record]: ...  # pure, no network
