"""Parse step: turn stored snapshots into provision versions. No network.

Snapshots apply oldest first. A provision gets a new version only when its text
hash changes; the old one is closed with valid_to. One run is one transaction, so
the catalog never holds a half-applied run.
"""

import json
import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from myapp.ingest.fetch import read_raw
from myapp.ingest.schema import RawDocument, Record, SourceName

log = logging.getLogger(__name__)


class Parser(Protocol):
    name: SourceName

    def parse(self, raw: RawDocument) -> Iterator[Record]: ...


class OutOfOrderError(RuntimeError):
    """An unparsed snapshot is older than one already applied."""


@dataclass(frozen=True)
class ParseSummary:
    run_id: int
    snapshots: int
    added: int
    modified: int
    removed: int


@dataclass(frozen=True)
class Snapshot:
    fetch_id: int
    day: str
    raw: RawDocument


def _now() -> str:
    return datetime.now(UTC).isoformat()


def pending(con: sqlite3.Connection, data_dir: Path, url_part: str) -> list[Snapshot]:
    """Accepted, unparsed snapshots whose URL names this part, oldest first."""
    rows = con.execute(
        "SELECT * FROM fetches WHERE accepted = 1 AND parsed_by_run IS NULL "
        "AND url LIKE ? ORDER BY source_version",
        (f"%{url_part}",),
    ).fetchall()
    done = con.execute(
        "SELECT max(source_version) FROM fetches WHERE parsed_by_run IS NOT NULL "
        "AND url LIKE ?",
        (f"%{url_part}",),
    ).fetchone()[0]
    if rows and done and rows[0]["source_version"] <= done:
        raise OutOfOrderError(
            f"snapshot {rows[0]['source_version']} is not newer than {done}, which is "
            "already applied; rebuild the catalog to backfill older history"
        )
    return [
        Snapshot(
            fetch_id=row["fetch_id"],
            day=row["source_version"],
            raw=RawDocument(
                source="ecfr",
                url=row["url"],
                retrieved_at=datetime.fromisoformat(row["fetched_at"]),
                content_type=row["content_type"] or "",
                body=read_raw(data_dir, row["raw_path"]),
                source_version=row["source_version"],
            ),
        )
        for row in rows
    ]


def _open_versions(con: sqlite3.Connection, prefix: str) -> dict[str, tuple[int, str]]:
    rows = con.execute(
        "SELECT provision_id, version, content_hash FROM provision_versions "
        "WHERE valid_to IS NULL AND provision_id LIKE ?",
        (f"{prefix}%",),
    )
    return {r["provision_id"]: (r["version"], r["content_hash"]) for r in rows}


def _upsert_provision(con: sqlite3.Connection, rec: Record, ordinal: int) -> None:
    m = rec.metadata
    con.execute(
        "INSERT INTO provisions (provision_id, source, parent_id, ordinal, heading, "
        "heading_path, authority, visa_types, visa_types_rule) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (provision_id) DO UPDATE SET parent_id = excluded.parent_id, "
        "ordinal = excluded.ordinal, heading = excluded.heading, "
        "heading_path = excluded.heading_path, visa_types = excluded.visa_types, "
        "visa_types_rule = excluded.visa_types_rule",
        (
            m.provision_id,
            m.source,
            m.parent_id,
            ordinal,
            m.heading,
            m.heading_path,
            m.authority,
            json.dumps(sorted(m.visa_types)),
            m.visa_types_rule,
        ),
    )


def apply_snapshot(
    con: sqlite3.Connection,
    run_id: int,
    snap: Snapshot,
    records: list[Record],
    prefix: str,
    baseline: bool,
) -> tuple[int, int, int]:
    """Write one snapshot's differences. Returns (added, modified, removed)."""
    current = _open_versions(con, prefix)
    valid_from = None if baseline else snap.day  # in force before the window began
    added = modified = removed = 0
    seen: set[str] = set()

    def change(pid: str, kind: str, old: str | None, new: str | None) -> None:
        if not baseline:  # the baseline is where watching starts, not a change
            con.execute(
                "INSERT INTO changes (run_id, provision_id, kind, effective, old_hash, "
                "new_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, pid, kind, snap.day, old, new),
            )

    def close(pid: str, version: int) -> None:
        con.execute(
            "UPDATE provision_versions SET valid_to = ?, status = 'superseded' "
            "WHERE provision_id = ? AND version = ?",
            (snap.day, pid, version),
        )

    for ordinal, rec in enumerate(records):
        pid, new_hash = rec.metadata.provision_id, rec.content_hash
        seen.add(pid)
        _upsert_provision(con, rec, ordinal)
        old = current.get(pid)
        if old and old[1] == new_hash:
            continue
        if old:
            close(pid, old[0])
            change(pid, "modified", old[1], new_hash)
            modified += 1
        else:
            change(pid, "added", None, new_hash)
            added += 1
        version = con.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM provision_versions "
            "WHERE provision_id = ?",
            (pid,),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO provision_versions (provision_id, version, text, "
            "content_hash, status, valid_from, valid_to, fetch_id) "
            "VALUES (?, ?, ?, ?, 'in_force', ?, NULL, ?)",
            (pid, version, rec.text, new_hash, valid_from, snap.fetch_id),
        )
    for pid, (version, old_hash) in current.items():
        if pid not in seen:
            close(pid, version)
            change(pid, "removed", old_hash, None)
            removed += 1
    con.execute(
        "UPDATE fetches SET parsed_by_run = ? WHERE fetch_id = ?",
        (run_id, snap.fetch_id),
    )
    return added, modified, removed


def reset(con: sqlite3.Connection, url_part: str, prefix: str) -> int:
    """Forget one part's parsed history so every stored snapshot applies again.

    Raw files and fetch records stay; only what parsing derived from them goes.
    """
    like = f"{prefix}%"
    with con:  # all or nothing
        con.execute("DELETE FROM changes WHERE provision_id LIKE ?", (like,))
        con.execute("DELETE FROM provision_versions WHERE provision_id LIKE ?", (like,))
        con.execute("DELETE FROM provisions WHERE provision_id LIKE ?", (like,))
        cur = con.execute(
            "UPDATE fetches SET parsed_by_run = NULL WHERE url LIKE ? "
            "AND parsed_by_run IS NOT NULL",
            (f"%{url_part}",),
        )
    return cur.rowcount


def parse(
    con: sqlite3.Connection,
    data_dir: Path,
    source: Parser,
    url_part: str,
    prefix: str,
    baseline_day: str,
) -> ParseSummary:
    """Apply every pending snapshot for one part in a single transaction."""
    snapshots = pending(con, data_dir, url_part)
    cur = con.execute(
        "INSERT INTO runs (source, command, started_at) VALUES (?, 'parse', ?)",
        (source.name, _now()),
    )
    run_id = int(cur.lastrowid or 0)
    con.commit()  # the run row survives even if the work below rolls back
    totals = [0, 0, 0]
    status = "failed"
    try:
        for snap in snapshots:
            records = list(source.parse(snap.raw))
            counts = apply_snapshot(
                con, run_id, snap, records, prefix, snap.day <= baseline_day
            )
            totals = [a + b for a, b in zip(totals, counts, strict=True)]
            log.info(
                "snapshot %s: %d added, %d modified, %d removed", snap.day, *counts
            )
        con.commit()
        status = "succeeded"
    except BaseException:
        con.rollback()  # nothing from a failed run reaches the catalog
        raise
    finally:
        con.execute(
            "UPDATE runs SET status = ?, finished_at = ?, accepted = ?, "
            "versions_added = ? WHERE run_id = ?",
            (
                status,
                _now(),
                len(snapshots) if status == "succeeded" else 0,
                totals[0] + totals[1],
                run_id,
            ),
        )
        con.commit()
    return ParseSummary(run_id, len(snapshots), *totals)
