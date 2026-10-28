import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from myapp.ingest.catalog import (
    MIGRATIONS,
    SCHEMA_REQUIRED,
    CatalogOutdatedError,
    available,
    connect,
    migrate,
    require_current,
    schema_version,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "catalog.sqlite"


def tables(path: Path) -> set[str]:
    with closing(connect(path)) as con:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {r[0] for r in rows}


def test_fresh_catalog_gets_every_migration(db: Path, tmp_path: Path) -> None:
    log = migrate(db, tmp_path / "backups")
    assert log[-1] == f"schema is at {SCHEMA_REQUIRED}"
    assert {"runs", "fetches", "provisions", "provision_versions", "changes"} <= tables(
        db
    )
    assert not (tmp_path / "backups").exists()  # nothing to back up yet


def test_migrate_twice_does_nothing(db: Path, tmp_path: Path) -> None:
    migrate(db, tmp_path / "backups")
    assert migrate(db, tmp_path / "backups") == [
        f"schema is at {SCHEMA_REQUIRED}: nothing to do"
    ]


def test_shipped_migrations_are_numbered_without_gaps() -> None:
    assert [n for n, _ in available(MIGRATIONS)] == list(range(1, SCHEMA_REQUIRED + 1))


def _migrations(tmp_path: Path, *bodies: str) -> Path:
    folder = tmp_path / "migrations"
    folder.mkdir()
    for i, body in enumerate(bodies, start=1):
        (folder / f"{i:04d}_step.sql").write_text(body)
    return folder


def test_failed_migration_rolls_back_completely(db: Path, tmp_path: Path) -> None:
    source = _migrations(
        tmp_path,
        "CREATE TABLE a (x INTEGER);",
        "CREATE TABLE b (x INTEGER); INSERT INTO nowhere VALUES (1);",
    )
    with pytest.raises(RuntimeError, match="0002_step.sql failed"):
        migrate(db, tmp_path / "backups", source)  # type: ignore[arg-type]
    assert "a" in tables(db) and "b" not in tables(db)
    with closing(connect(db)) as con:
        assert schema_version(con) == 1


def test_backup_before_changing_an_existing_catalog(db: Path, tmp_path: Path) -> None:
    first = _migrations(tmp_path, "CREATE TABLE a (x INTEGER);")
    migrate(db, tmp_path / "backups", first)  # type: ignore[arg-type]
    (first / "0002_step.sql").write_text("CREATE TABLE b (x INTEGER);")
    log = migrate(db, tmp_path / "backups", first)  # type: ignore[arg-type]
    assert log[0].startswith("backed up to")
    [saved] = (tmp_path / "backups").iterdir()
    with closing(sqlite3.connect(saved)) as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert "a" in names and "b" not in names  # the state before 0002


def test_outdated_catalog_is_refused_with_the_fix(db: Path) -> None:
    with (
        closing(connect(db)) as con,
        pytest.raises(CatalogOutdatedError, match="migrate"),
    ):
        require_current(con)


def test_only_one_open_version_per_provision(db: Path, tmp_path: Path) -> None:
    migrate(db, tmp_path / "backups")
    with closing(connect(db)) as con:
        con.execute(
            "INSERT INTO runs (run_id, source, command, started_at) "
            "VALUES (1, 'ecfr', 'fetch', 'now')"
        )
        con.execute(
            "INSERT INTO fetches (fetch_id, run_id, url, fetched_at, accepted) "
            "VALUES (1, 1, 'u', 'now', 1)"
        )
        con.execute(
            "INSERT INTO provisions (provision_id, source, ordinal, authority) "
            "VALUES ('p', 'ecfr', 0, 'regulation')"
        )
        insert = (
            "INSERT INTO provision_versions (provision_id, version, text, "
            "content_hash, status, valid_to, fetch_id) VALUES ('p', ?, 't', 'h', "
            "'in_force', NULL, 1)"
        )
        con.execute(insert, (1,))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, (2,))
