"""The ingestion catalog: one SQLite file, changed only by numbered migrations.

Migrations run as their own one-off step, never at startup:
    python -m myapp.ingest.catalog migrate
    python -m myapp.ingest.catalog status
"""

import argparse
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from myapp.config import IngestSettings

MIGRATIONS = files("myapp.ingest") / "migrations"
NAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")


class CatalogOutdatedError(RuntimeError):
    """The catalog's schema is older than this code needs."""


def catalog_path(settings: IngestSettings) -> Path:
    return settings.data_dir / "catalog.sqlite"


def connect(path: Path) -> sqlite3.Connection:
    """Open the catalog. Callers close it, e.g. with contextlib.closing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")  # the loader can read while ingest writes
    return con


def available(source: Traversable = MIGRATIONS) -> list[tuple[int, str]]:
    """Every migration file as (number, filename), in order."""
    found = []
    for entry in source.iterdir():
        match = NAME.fullmatch(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name))
    found.sort()
    numbers = [n for n, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"migrations must be numbered 1..N with no gaps: {numbers}")
    return found


SCHEMA_REQUIRED = len(available())


def schema_version(con: sqlite3.Connection) -> int:
    con.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = con.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def require_current(con: sqlite3.Connection) -> None:
    have = schema_version(con)
    if have < SCHEMA_REQUIRED:
        raise CatalogOutdatedError(
            f"the catalog schema is at {have} and this code needs {SCHEMA_REQUIRED}: "
            "run python -m myapp.ingest.catalog migrate"
        )


def backup(con: sqlite3.Connection, backups: Path, label: str) -> Path:
    """Copy the live database with SQLite's backup API, which is safe under WAL."""
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = backups / f"catalog-{stamp}-before-{label}.sqlite"
    with sqlite3.connect(target) as dest:
        con.backup(dest)
    dest.close()
    return target


def migrate(path: Path, backups: Path, source: Traversable = MIGRATIONS) -> list[str]:
    """Apply pending migrations in order. Each is all or nothing. Returns log lines."""
    log = []
    con = connect(path)
    try:
        have = started = schema_version(con)
        pending = [(n, name) for n, name in available(source) if n > have]
        if not pending:
            return [f"schema is at {have}: nothing to do"]
        for number, name in pending:
            if started > 0:  # a catalog created by this run has nothing to lose
                log.append(f"backed up to {backup(con, backups, name[:4])}")
            sql = (source / name).read_text()
            stamp = datetime.now(UTC).isoformat()
            try:
                con.executescript(
                    f"BEGIN;\n{sql}\n"
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    f"VALUES ({number}, '{name}', '{stamp}');\nCOMMIT;"
                )
            except sqlite3.Error as exc:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise RuntimeError(
                    f"migration {name} failed and was rolled back"
                ) from exc
            have = number
            log.append(f"applied {name}")
        log.append(f"schema is at {have}")
        return log
    finally:
        con.close()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["migrate", "status"])
    args = parser.parse_args(argv)
    settings = IngestSettings()
    path = catalog_path(settings)
    if args.command == "migrate":
        print("\n".join(migrate(path, settings.data_dir / "backups")))
        return
    con = connect(path)
    try:
        print(f"schema is at {schema_version(con)}, code needs {SCHEMA_REQUIRED}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
