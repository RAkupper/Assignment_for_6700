"""Fetch step: download, validate, and store raw responses. Network only.

Every response is kept, accepted or not. Logs hold counts, never content.
"""

import gzip
import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from myapp.ingest.schema import RawDocument
from myapp.ingest.source import Source

log = logging.getLogger(__name__)

EXTENSIONS = {"xml": "xml", "json": "json", "html": "html"}


@dataclass(frozen=True)
class FetchSummary:
    run_id: int
    accepted: int
    rejected: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _extension(content_type: str) -> str:
    return next((ext for key, ext in EXTENSIONS.items() if key in content_type), "bin")


def store_raw(
    con: sqlite3.Connection, data_dir: Path, raw: RawDocument
) -> tuple[str, str]:
    """Write the body gzipped under its hash, once. Returns (relative path, sha256)."""
    sha = hashlib.sha256(raw.body).hexdigest()
    row = con.execute(
        "SELECT raw_path FROM fetches WHERE sha256 = ? AND raw_path IS NOT NULL",
        (sha,),
    ).fetchone()
    if row:
        return row["raw_path"], sha  # same bytes already stored
    day = raw.retrieved_at.date().isoformat()
    relative = f"raw/{raw.source}/{day}/{sha}.{_extension(raw.content_type)}.gz"
    target = data_dir / relative
    if not target.exists():  # raw files are never rewritten
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".partial")
        with gzip.GzipFile(partial, "wb", mtime=0) as out:
            out.write(raw.body)
        os.replace(partial, target)  # never leaves a half-written file in place
    return relative, sha


def read_raw(data_dir: Path, relative: str) -> bytes:
    with gzip.open(data_dir / relative, "rb") as f:
        return f.read()


def already_fetched(con: sqlite3.Connection, url: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM fetches WHERE url = ? AND accepted = 1", (url,)
    ).fetchone()
    return row is not None


def fetch(con: sqlite3.Connection, data_dir: Path, source: Source) -> FetchSummary:
    """One run for one source. Each fetch is committed as it lands, so reruns resume."""
    cur = con.execute(
        "INSERT INTO runs (source, command, started_at) VALUES (?, 'fetch', ?)",
        (source.name, _now()),
    )
    con.commit()
    run_id = int(cur.lastrowid or 0)
    accepted = rejected = 0
    status = "failed"  # also covers Ctrl+C, which is not an Exception
    try:
        for raw in source.fetch():
            reason = source.validate(raw)
            relative, sha = store_raw(con, data_dir, raw)
            con.execute(
                "INSERT INTO fetches (run_id, url, fetched_at, status_code, "
                "content_type, sha256, bytes, source_version, raw_path, accepted, "
                "reject_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    str(raw.url),
                    raw.retrieved_at.isoformat(),
                    raw.status_code,
                    raw.content_type,
                    sha,
                    len(raw.body),
                    raw.source_version,
                    relative,
                    int(reason is None),
                    reason,
                ),
            )
            if reason is None:
                accepted += 1
            else:
                rejected += 1
                log.warning(
                    "rejected %s snapshot %s: %s",
                    source.name,
                    raw.source_version,
                    reason,
                )
            con.execute(
                "UPDATE runs SET accepted = ?, rejected = ? WHERE run_id = ?",
                (accepted, rejected, run_id),
            )
            con.commit()
        status = "succeeded"
    finally:
        con.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
            (status, _now(), run_id),
        )
        con.commit()
        log.info(
            "fetch run %d %s: %d accepted, %d rejected",
            run_id,
            status,
            accepted,
            rejected,
        )
    return FetchSummary(run_id, accepted, rejected)
