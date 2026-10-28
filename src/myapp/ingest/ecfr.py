"""eCFR source: one full-part snapshot per amendment date since the window start."""

import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from myapp.config import IngestSettings
from myapp.ingest.http import PoliteClient
from myapp.ingest.schema import RawDocument, Record, SourceName

MIN_BYTES = 1_000
BOT_MARKERS = (b"<html", b"captcha", b"access denied", b"request blocked")


def split_part(part: str) -> tuple[str, str]:
    """'8 CFR 274a' -> ('8', '274a')."""
    title, _, number = part.split(" ")
    return title, number


class EcfrSource:
    name: SourceName = "ecfr"

    def __init__(
        self,
        settings: IngestSettings,
        client: PoliteClient,
        part: str,
        already_fetched: Callable[[str], bool] = lambda url: False,
    ) -> None:
        self.base = str(settings.ecfr_base_url).rstrip("/")
        self.since = settings.ecfr_since.isoformat()
        self.client = client
        self.title, self.part = split_part(part)
        self.already_fetched = already_fetched

    def versions_url(self) -> str:
        return (
            f"{self.base}/api/versioner/v1/versions/title-{self.title}.json"
            f"?part={self.part}"
        )

    def snapshot_url(self, day: str) -> str:
        return (
            f"{self.base}/api/versioner/v1/full/{day}/title-{self.title}.xml"
            f"?part={self.part}"
        )

    def snapshot_dates(self) -> list[str]:
        """The window start plus every amendment date after it, oldest first."""
        response = self.client.get(self.versions_url())
        response.raise_for_status()
        dates = {v["date"] for v in response.json()["content_versions"]}
        return sorted({self.since} | {d for d in dates if d > self.since})

    def fetch(self) -> Iterator[RawDocument]:
        for day in self.snapshot_dates():
            url = self.snapshot_url(day)
            if self.already_fetched(url):
                continue
            response = self.client.get(url)
            yield RawDocument(
                source=self.name,
                url=url,  # type: ignore[arg-type]
                retrieved_at=datetime.now(UTC),
                content_type=response.headers.get("Content-Type", ""),
                body=response.content,
                status_code=response.status_code,
                source_version=day,
            )

    def validate(self, raw: RawDocument) -> str | None:
        if raw.status_code != 200:
            return f"HTTP {raw.status_code}"
        if "xml" not in raw.content_type:
            return f"content type {raw.content_type or 'missing'}"
        if len(raw.body) < MIN_BYTES:
            return f"only {len(raw.body)} bytes"
        head = raw.body[:2048].lower()
        if any(marker in head for marker in BOT_MARKERS):
            return "looks like a bot-check page"
        try:
            root = ET.fromstring(raw.body)
        except ET.ParseError:
            return "not well-formed XML (truncated?)"
        if root.tag != "DIV5" or root.get("N") != self.part:
            return f"root is {root.tag} N={root.get('N')}, expected part {self.part}"
        return None

    def parse(self, raw: RawDocument) -> Iterator[Record]:
        raise NotImplementedError("section 3")
