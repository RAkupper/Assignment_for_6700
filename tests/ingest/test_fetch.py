import json
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import httpx2
import pytest

from myapp.config import IngestSettings
from myapp.ingest.catalog import connect, migrate
from myapp.ingest.ecfr import EcfrSource
from myapp.ingest.fetch import already_fetched, fetch, read_raw
from myapp.ingest.http import PoliteClient

PAD = "<P>" + "x" * 1200 + "</P>"


def snapshot(day: str, part: str = "214") -> bytes:
    return f'<?xml version="1.0"?><DIV5 N="{part}"><P>{day}</P>{PAD}</DIV5>'.encode()


VERSIONS = {
    "content_versions": [
        {"date": "2019-06-01"},
        {"date": "2021-03-02"},
        {"date": "2021-03-02"},
        {"date": "2024-07-15"},
    ]
}

Handler = Callable[[httpx2.Request], httpx2.Response]


def ecfr(bodies: dict[str, httpx2.Response] | None = None) -> Handler:
    """Fake eCFR: the versions listing plus one snapshot per date."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if "/versions/" in request.url.path:
            return httpx2.Response(200, json=VERSIONS)
        day = request.url.path.split("/full/")[1].split("/")[0]
        if bodies and day in bodies:
            return bodies[day]
        xml = {"Content-Type": "application/xml"}
        return httpx2.Response(200, content=snapshot(day), headers=xml)

    return handle


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(tmp_path: Path) -> IngestSettings:
    return IngestSettings(data_dir=tmp_path / "data")


def client(settings: IngestSettings, handler: Handler, clock: Clock) -> PoliteClient:
    return PoliteClient(
        settings, httpx2.MockTransport(handler), sleep=clock.sleep, clock=clock
    )


@pytest.fixture
def catalog(settings: IngestSettings) -> Path:
    path = settings.data_dir / "catalog.sqlite"
    migrate(path, settings.data_dir / "backups")
    return path


def run(settings: IngestSettings, catalog: Path, handler: Handler) -> tuple[int, int]:
    with closing(connect(catalog)) as con:
        source = EcfrSource(
            settings,
            client(settings, handler, Clock()),
            "8 CFR 214",
            lambda url: already_fetched(con, url),
        )
        summary = fetch(con, settings.data_dir, source)
    return summary.accepted, summary.rejected


def test_fetches_window_start_and_each_later_amendment_once(
    settings: IngestSettings, catalog: Path
) -> None:
    assert run(settings, catalog, ecfr()) == (3, 0)
    with closing(connect(catalog)) as con:
        days = [r[0] for r in con.execute("SELECT source_version FROM fetches")]
    assert days == ["2020-01-01", "2021-03-02", "2024-07-15"]


def test_rerun_fetches_nothing_new(settings: IngestSettings, catalog: Path) -> None:
    run(settings, catalog, ecfr())
    assert run(settings, catalog, ecfr()) == (0, 0)


def test_raw_files_are_gzipped_originals(
    settings: IngestSettings, catalog: Path
) -> None:
    run(settings, catalog, ecfr())
    with closing(connect(catalog)) as con:
        path = con.execute(
            "SELECT raw_path FROM fetches WHERE source_version = '2024-07-15'"
        ).fetchone()[0]
    assert path.endswith(".xml.gz")
    assert read_raw(settings.data_dir, path) == snapshot("2024-07-15")


def test_identical_bytes_are_stored_once(
    settings: IngestSettings, catalog: Path
) -> None:
    same = httpx2.Response(
        200, content=snapshot("same"), headers={"Content-Type": "application/xml"}
    )
    run(settings, catalog, ecfr({"2021-03-02": same, "2024-07-15": same}))
    files = list((settings.data_dir / "raw").rglob("*.gz"))
    assert len(files) == 2


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx2.Response(404), "HTTP 404"),
        (
            httpx2.Response(
                200,
                content=b"<html><body>Access Denied" + b" " * 2000 + b"</body></html>",
                headers={"Content-Type": "text/html"},
            ),
            "content type",
        ),
        (
            httpx2.Response(
                200,
                content=b"<html>captcha</html>" + b" " * 2000,
                headers={"Content-Type": "application/xml"},
            ),
            "bot-check",
        ),
        (
            httpx2.Response(
                200,
                content=snapshot("x")[:-20],
                headers={"Content-Type": "application/xml"},
            ),
            "well-formed",
        ),
        (
            httpx2.Response(
                200,
                content=snapshot("x", part="274a"),
                headers={"Content-Type": "application/xml"},
            ),
            "expected part 214",
        ),
    ],
)
def test_bad_responses_are_kept_but_rejected(
    settings: IngestSettings, catalog: Path, response: httpx2.Response, reason: str
) -> None:
    assert run(settings, catalog, ecfr({"2024-07-15": response})) == (2, 1)
    with closing(connect(catalog)) as con:
        row = con.execute(
            "SELECT accepted, reject_reason, raw_path FROM fetches "
            "WHERE source_version = '2024-07-15'"
        ).fetchone()
    assert row["accepted"] == 0 and reason in row["reject_reason"]
    assert (settings.data_dir / row["raw_path"]).exists()  # never lose what was sent


def test_rejected_snapshot_is_retried_next_run(
    settings: IngestSettings, catalog: Path
) -> None:
    run(settings, catalog, ecfr({"2024-07-15": httpx2.Response(404)}))
    assert run(settings, catalog, ecfr()) == (1, 0)


def test_failed_run_is_marked_and_keeps_what_landed(
    settings: IngestSettings, catalog: Path
) -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        if "2024-07-15" in request.url.path:
            raise httpx2.ConnectError("down")
        return ecfr()(request)

    with pytest.raises(httpx2.ConnectError):
        run(settings, catalog, broken)
    with closing(connect(catalog)) as con:
        assert con.execute("SELECT status FROM runs").fetchone()[0] == "failed"
        assert con.execute("SELECT count(*) FROM fetches").fetchone()[0] == 2


def test_client_waits_between_requests_and_identifies_itself(
    settings: IngestSettings,
) -> None:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200)

    clock = Clock()
    polite = client(settings, record, clock)
    polite.get("https://www.ecfr.gov/a")
    polite.get("https://www.ecfr.gov/b")
    assert clock.sleeps == [settings.min_request_interval_s]
    assert seen[0].headers["User-Agent"] == settings.user_agent
    assert "gzip" in seen[0].headers["Accept-Encoding"]


def test_client_retries_then_returns_the_last_answer(
    settings: IngestSettings,
) -> None:
    answers = iter(
        [httpx2.Response(503), httpx2.Response(429, headers={"Retry-After": "7"})]
        + [httpx2.Response(200, json={"ok": True})]
    )
    clock = Clock()
    polite = client(settings, lambda r: next(answers), clock)
    assert json.loads(polite.get("https://www.ecfr.gov/a").content) == {"ok": True}
    assert 7.0 in clock.sleeps  # honored Retry-After

    clock = Clock()
    always = client(settings, lambda r: httpx2.Response(503), clock)
    assert always.get("https://www.ecfr.gov/a").status_code == 503
