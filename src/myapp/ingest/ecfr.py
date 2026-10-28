"""eCFR source: one full-part snapshot per amendment date since the window start."""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime

from myapp.config import IngestSettings
from myapp.ingest.ecfr_parse import ParseReport, parse_part, with_text
from myapp.ingest.http import PoliteClient
from myapp.ingest.schema import RawDocument, Record, RecordMetadata, SourceName

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
        return records(raw, parse_part(raw.body, self.title))


def provision_url(provision_id: str, day: str) -> str:
    """Link to the paragraph in the eCFR as it read on `day`."""
    match = re.match(r'(\d+) CFR ([\d.a-z-]+)(\([^ "#]*\))?', provision_id)
    if match is None:
        return f"https://www.ecfr.gov/on/{day}"
    title, section, paragraph = match.groups()
    anchor = f"#p-{section}{paragraph}" if paragraph else ""
    return f"https://www.ecfr.gov/on/{day}/title-{title}/section-{section}{anchor}"


def records(raw: RawDocument, report: ParseReport) -> Iterator[Record]:
    day = raw.source_version or raw.retrieved_at.date().isoformat()
    for p in with_text(report):
        yield Record(
            metadata=RecordMetadata(
                source="ecfr",
                provision_id=p.provision_id,
                parent_id=p.parent_id,
                heading=p.heading,
                heading_path=p.heading_path,
                authority="regulation",
                status="in_force",
                valid_from=date.fromisoformat(day),
                url=provision_url(p.provision_id, day),  # type: ignore[arg-type]
                retrieved_at=raw.retrieved_at,
                visa_types=frozenset(p.visa_types),  # type: ignore[arg-type]
                visa_types_rule=p.visa_types_rule,
            ),
            text=p.text,
        )
