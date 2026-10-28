import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from myapp.ingest.catalog import connect, migrate
from myapp.ingest.ecfr import records
from myapp.ingest.ecfr_parse import parse_part
from myapp.ingest.fetch import store_raw
from myapp.ingest.schema import RawDocument, Record
from myapp.ingest.versions import OutOfOrderError, ParseSummary, parse, reset

URL = "https://www.ecfr.gov/api/versioner/v1/full/{day}/title-8.xml?part=214"
BASELINE = "2020-01-01"


class EcfrParser:
    name = "ecfr"

    def parse(self, raw: RawDocument) -> Iterator[Record]:
        return records(raw, parse_part(raw.body, "8"))


def xml(*paragraphs: str) -> bytes:
    body = "".join(f"<P>{p}</P>" for p in paragraphs)
    return (
        f'<DIV5 N="214"><DIV8 N="214.2"><HEAD>§ 214.2 Rules.</HEAD>{body}</DIV8></DIV5>'
    ).encode()


@pytest.fixture
def con(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    migrate(tmp_path / "catalog.sqlite", tmp_path / "backups")
    with closing(connect(tmp_path / "catalog.sqlite")) as c:
        c.execute(
            "INSERT INTO runs (run_id, source, command, started_at, status) "
            "VALUES (1, 'ecfr', 'fetch', 'now', 'succeeded')"
        )
        c.commit()
        yield c


def snapshot(con: sqlite3.Connection, data: Path, day: str, body: bytes) -> None:
    raw = RawDocument(
        source="ecfr",
        url=URL.format(day=day),  # type: ignore[arg-type]
        retrieved_at=datetime(2026, 10, 27, tzinfo=UTC),
        content_type="application/xml",
        body=body,
        source_version=day,
    )
    path, sha = store_raw(con, data, raw)
    con.execute(
        "INSERT INTO fetches (run_id, url, fetched_at, content_type, sha256, "
        "source_version, raw_path, accepted) VALUES (1, ?, ?, ?, ?, ?, ?, 1)",
        (str(raw.url), raw.retrieved_at.isoformat(), raw.content_type, sha, day, path),
    )
    con.commit()


def run(con: sqlite3.Connection, data: Path) -> ParseSummary:
    return parse(
        con, data, EcfrParser(), "title-8.xml?part=214", "8 CFR 214.", BASELINE
    )


def versions(con: sqlite3.Connection, pid: str) -> list[tuple[object, ...]]:
    rows = con.execute(
        "SELECT version, valid_from, valid_to, status, text FROM provision_versions "
        "WHERE provision_id = ? ORDER BY version",
        (pid,),
    )
    return [tuple(r) for r in rows]


def test_baseline_has_no_start_date_and_no_changes(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    snapshot(con, tmp_path, BASELINE, xml("(a) Old rule."))
    run(con, tmp_path)
    assert versions(con, "8 CFR 214.2(a)") == [(1, None, None, "in_force", "Old rule.")]
    assert con.execute("SELECT count(*) FROM changes").fetchone()[0] == 0


def test_changed_text_closes_the_old_version(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    snapshot(con, tmp_path, BASELINE, xml("(a) Old rule.", "(b) Same."))
    snapshot(con, tmp_path, "2022-03-01", xml("(a) New rule.", "(b) Same."))
    summary = run(con, tmp_path)
    assert versions(con, "8 CFR 214.2(a)") == [
        (1, None, "2022-03-01", "superseded", "Old rule."),
        (2, "2022-03-01", None, "in_force", "New rule."),
    ]
    assert len(versions(con, "8 CFR 214.2(b)")) == 1  # unchanged text, no new version
    kinds = con.execute("SELECT provision_id, kind, effective FROM changes").fetchall()
    assert [tuple(k) for k in kinds] == [("8 CFR 214.2(a)", "modified", "2022-03-01")]
    assert (summary.snapshots, summary.modified) == (2, 1)


def test_removed_then_restored(con: sqlite3.Connection, tmp_path: Path) -> None:
    snapshot(con, tmp_path, BASELINE, xml("(a) A.", "(b) B."))
    snapshot(con, tmp_path, "2021-01-01", xml("(a) A."))
    snapshot(con, tmp_path, "2023-01-01", xml("(a) A.", "(b) B is back."))
    run(con, tmp_path)
    assert versions(con, "8 CFR 214.2(b)") == [
        (1, None, "2021-01-01", "superseded", "B."),
        (2, "2023-01-01", None, "in_force", "B is back."),
    ]
    kinds = [r[0] for r in con.execute("SELECT kind FROM changes ORDER BY change_id")]
    assert kinds == ["removed", "added"]


def test_rerun_applies_nothing(con: sqlite3.Connection, tmp_path: Path) -> None:
    snapshot(con, tmp_path, BASELINE, xml("(a) A."))
    run(con, tmp_path)
    assert run(con, tmp_path).snapshots == 0
    snapshot(con, tmp_path, "2024-05-01", xml("(a) A2."))
    assert run(con, tmp_path).modified == 1  # only the new snapshot


def test_failed_run_writes_nothing(con: sqlite3.Connection, tmp_path: Path) -> None:
    snapshot(con, tmp_path, BASELINE, xml("(a) A."))
    snapshot(con, tmp_path, "2024-05-01", b"<DIV5 N='214'><DIV8")  # corrupt
    with pytest.raises(Exception, match="(?i)unclosed|no element|syntax"):
        run(con, tmp_path)
    assert con.execute("SELECT count(*) FROM provision_versions").fetchone()[0] == 0
    unparsed = "SELECT count(*) FROM fetches WHERE parsed_by_run IS NULL"
    assert con.execute(unparsed).fetchone()[0] == 2
    status = con.execute("SELECT status FROM runs WHERE command = 'parse'").fetchone()
    assert status[0] == "failed"


def test_older_snapshot_after_newer_is_refused(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    snapshot(con, tmp_path, "2024-05-01", xml("(a) A."))
    run(con, tmp_path)
    snapshot(con, tmp_path, "2022-01-01", xml("(a) Older."))
    with pytest.raises(OutOfOrderError, match="rebuild"):
        run(con, tmp_path)


def test_validity_intervals_never_overlap_on_real_history(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    import gzip

    fixtures = Path(__file__).with_name("fixtures")
    for day in ("2020-01-01", "2026-09-30"):
        body = gzip.open(fixtures / f"ecfr_8cfr214_{day}.xml.gz").read()
        snapshot(con, tmp_path, day, body)
    summary = run(con, tmp_path)
    assert summary.modified > 100
    overlaps = con.execute(
        "SELECT count(*) FROM provision_versions a JOIN provision_versions b "
        "ON a.provision_id = b.provision_id AND a.version < b.version "
        "WHERE coalesce(a.valid_to, '9999') > coalesce(b.valid_from, '0000')"
    ).fetchone()[0]
    assert overlaps == 0
    still_open = "SELECT count(*) FROM provision_versions WHERE valid_to IS NULL"
    assert con.execute(still_open).fetchone()[0] > 2500


def test_rebuild_reapplies_everything_from_raw(
    con: sqlite3.Connection, tmp_path: Path
) -> None:
    snapshot(con, tmp_path, BASELINE, xml("(a) Old rule."))
    snapshot(con, tmp_path, "2022-03-01", xml("(a) New rule."))
    run(con, tmp_path)
    before = versions(con, "8 CFR 214.2(a)")
    assert reset(con, "title-8.xml?part=214", "8 CFR 214.") == 2
    assert con.execute("SELECT count(*) FROM provisions").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM fetches").fetchone()[0] == 2  # raw kept
    assert run(con, tmp_path).snapshots == 2
    assert versions(con, "8 CFR 214.2(a)") == before
