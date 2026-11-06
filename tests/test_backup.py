"""Tests for backups: what a backup holds, its checks, restore, and GitHub releases."""

import io
import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from myapp.backup import (
    CATALOG,
    FILES,
    MANIFEST,
    SNAPSHOT,
    BackupError,
    BackupSettings,
    Releases,
    check_catalog,
    create,
    pack_data,
    restore,
    unpack_data,
    verify,
)
from myapp.ingest.catalog import migrate


class FakeCollections:
    """Collections whose snapshot file is just its point count."""

    alias = "provisions"

    def __init__(self, points: dict[str, int], live: str | None = None) -> None:
        self.points = points
        self.target = live
        self.dropped: list[str] = []
        self.short_by = 0  # make the next load come up short

    def live(self) -> str:
        if self.target is None:
            raise BackupError("alias points at nothing")
        return self.target

    def count(self, name: str) -> int:
        return self.points[name]

    def embed_model(self, name: str) -> str | None:
        return "BAAI/bge-small-en-v1.5"

    def next_name(self) -> str:
        return f"provisions_v{len(self.points) + 1}"

    def save(self, name: str, dest: Path) -> None:
        dest.write_text(str(self.points[name]))

    def load(self, snapshot: Path, name: str) -> None:
        self.points[name] = int(snapshot.read_text()) - self.short_by

    def drop(self, name: str) -> None:
        self.dropped.append(name)
        del self.points[name]

    def activate(self, name: str) -> str | None:
        previous, self.target = self.target, name
        return previous


def make_data_dir(root: Path, versions: int) -> Path:
    """A /data folder with a migrated catalog, some rows, and a raw download."""
    data = root / "data"
    migrate(data / CATALOG, data / "backups")
    with closing(sqlite3.connect(data / CATALOG)) as con:
        con.execute(
            "INSERT INTO provisions (provision_id, source, ordinal, authority) "
            "VALUES ('8 CFR 214.2(f)', 'ecfr', 1, 'regulation')"
        )
        for v in range(1, versions + 1):
            con.execute(
                "INSERT INTO provision_versions VALUES "
                "('8 CFR 214.2(f)', ?, 't', 'h', 'in_force', NULL, ?, NULL, 1)",
                (v, None if v == versions else f"202{v}-01-01"),
            )
        con.commit()
    (data / "raw" / "ecfr").mkdir(parents=True)
    (data / "raw" / "ecfr" / "a.xml.gz").write_bytes(b"snapshot")
    return data


def versions_in(catalog: Path) -> int:
    with closing(sqlite3.connect(catalog)) as con:
        return int(con.execute("SELECT count(*) FROM provision_versions").fetchone()[0])


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    source = make_data_dir(tmp_path / "source", versions=3)
    out = tmp_path / "backup"
    out.mkdir()
    create(out, source, FakeCollections({"provisions_v2": 3882}, live="provisions_v2"))
    return out


def test_backup_holds_catalog_files_snapshot_and_manifest(backup: Path) -> None:
    manifest = json.loads((backup / MANIFEST).read_text())

    assert sorted(p.name for p in backup.iterdir()) == sorted(
        [CATALOG, FILES, SNAPSHOT, MANIFEST]
    )
    assert manifest["collection"] == "provisions_v2"
    assert manifest["points"] == 3882
    assert manifest["provision_versions"] == 3
    assert manifest["catalog_schema"] >= 1
    assert set(manifest["files"]) == {CATALOG, FILES, SNAPSHOT}
    assert verify(backup).points == 3882


def test_pack_leaves_out_the_live_catalog_and_old_backups(tmp_path: Path) -> None:
    data = make_data_dir(tmp_path, versions=1)
    (data / f"{CATALOG}-wal").write_bytes(b"wal")
    (data / "backups").mkdir(exist_ok=True)
    (data / "backups" / "old.sqlite").write_bytes(b"old")

    pack_data(data, tmp_path / FILES)

    with tarfile.open(tmp_path / FILES) as tar:
        names = tar.getnames()
    assert "raw/ecfr/a.xml.gz" in names
    assert not any(n.startswith((CATALOG, "backups")) for n in names)


def test_unpack_refuses_paths_outside_data(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))

    with pytest.raises(tarfile.FilterError):
        unpack_data(archive, tmp_path / "data")
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.parametrize("damaged", [CATALOG, SNAPSHOT, FILES])
def test_verify_rejects_a_changed_file(backup: Path, damaged: str) -> None:
    with (backup / damaged).open("ab") as f:
        f.write(b"x")

    with pytest.raises(BackupError, match=damaged):
        verify(backup)


def test_verify_rejects_a_missing_manifest(backup: Path) -> None:
    (backup / MANIFEST).unlink()

    with pytest.raises(BackupError, match=MANIFEST):
        verify(backup)


def test_restore_onto_an_empty_machine(backup: Path, tmp_path: Path) -> None:
    """A fresh install: no catalog, no collections, then a working index."""
    data = tmp_path / "fresh" / "data"
    collections = FakeCollections({})

    lines = restore(backup, data, collections)

    assert versions_in(data / CATALOG) == 3
    assert (data / "raw" / "ecfr" / "a.xml.gz").read_bytes() == b"snapshot"
    assert collections.points == {"provisions_v1": 3882}
    assert collections.target == "provisions_v1"
    assert lines[-1] == "alias provisions: None -> provisions_v1"


def test_restore_keeps_what_was_there(backup: Path, tmp_path: Path) -> None:
    """Existing collections stay, and the replaced catalog is saved first."""
    data = make_data_dir(tmp_path / "live", versions=1)
    collections = FakeCollections({"provisions_v1": 10}, live="provisions_v1")

    restore(backup, data, collections)

    assert collections.points == {"provisions_v1": 10, "provisions_v2": 3882}
    assert collections.target == "provisions_v2"
    saved = list((data / "backups").glob("*-before-restore.sqlite"))
    assert len(saved) == 1
    assert versions_in(saved[0]) == 1
    assert versions_in(data / CATALOG) == 3


def test_restore_without_activate_leaves_the_alias(
    backup: Path, tmp_path: Path
) -> None:
    collections = FakeCollections({"provisions_v1": 10}, live="provisions_v1")

    restore(backup, tmp_path / "data", collections, activate=False)

    assert collections.target == "provisions_v1"
    assert "provisions_v2" in collections.points


def test_short_snapshot_is_dropped_and_nothing_else_changes(
    backup: Path, tmp_path: Path
) -> None:
    data = make_data_dir(tmp_path / "live", versions=1)
    collections = FakeCollections({"provisions_v1": 10}, live="provisions_v1")
    collections.short_by = 1

    with pytest.raises(BackupError, match="3881 points"):
        restore(backup, data, collections)

    assert collections.dropped == ["provisions_v2"]
    assert collections.target == "provisions_v1"
    assert versions_in(data / CATALOG) == 1


def test_a_damaged_backup_changes_nothing(backup: Path, tmp_path: Path) -> None:
    (backup / SNAPSHOT).write_text("1")
    data = make_data_dir(tmp_path / "live", versions=1)
    collections = FakeCollections({"provisions_v1": 10}, live="provisions_v1")

    with pytest.raises(BackupError):
        restore(backup, data, collections)

    assert collections.points == {"provisions_v1": 10}
    assert versions_in(data / CATALOG) == 1


class FakeGitHub:
    """Just enough of the releases API, recording every call."""

    def __init__(self, releases: list[dict[str, Any]] | None = None) -> None:
        self.releases = releases or []
        self.calls: list[str] = []
        self.fail_upload = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if method == "GET" and path.endswith("/releases"):
            visible = [r for r in self.releases if not r["draft"]]
            return httpx2.Response(200, json=visible)
        if method == "POST" and path.endswith("/releases"):
            body = json.loads(request.content)
            release = {"id": 99, "tag_name": body["tag_name"], "draft": body["draft"]}
            self.releases.append(release | {"assets": [], "html_url": "https://x/99"})
            return httpx2.Response(201, json=release)
        if method == "POST" and "/assets" in path:
            if self.fail_upload:
                return httpx2.Response(500)
            return httpx2.Response(201, json={"name": request.url.params["name"]})
        if method == "PATCH":
            self.releases[-1]["draft"] = False
            return httpx2.Response(200, json=self.releases[-1])
        if method == "DELETE":
            return httpx2.Response(204)
        if method == "GET" and "/releases/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            return httpx2.Response(200, json={"tag_name": tag, "assets": []})
        return httpx2.Response(404)


def releases(fake: FakeGitHub, token: str | None = "t" * 40) -> Releases:
    settings = BackupSettings(
        _env_file=None, github_token=SecretStr(token) if token else None
    )
    return Releases(settings, transport=httpx2.MockTransport(fake))


def release(tag: str, draft: bool = False, rid: int = 1) -> dict[str, Any]:
    return {"id": rid, "tag_name": tag, "draft": draft, "assets": [], "html_url": ""}


def test_published_lists_only_finished_data_releases_newest_first() -> None:
    fake = FakeGitHub(
        [
            release("data-20261101T000000Z"),
            release("v1.0.0"),
            release("data-20261103T000000Z"),
            release("data-20261105T000000Z", draft=True),
        ]
    )

    tags = [r["tag_name"] for r in releases(fake, token=None).published()]

    assert tags == ["data-20261103T000000Z", "data-20261101T000000Z"]


def test_latest_is_the_newest_data_release() -> None:
    fake = FakeGitHub(
        [release("data-20261101T000000Z"), release("data-20261102T000000Z")]
    )

    assert releases(fake, None).find("latest")["tag_name"] == "data-20261102T000000Z"


def test_latest_with_no_backups_is_a_clear_error() -> None:
    with pytest.raises(BackupError, match="no data-"):
        releases(FakeGitHub(), None).find("latest")


def test_publish_uploads_to_a_draft_then_publishes(backup: Path) -> None:
    fake = FakeGitHub()
    names = [CATALOG, FILES, SNAPSHOT, MANIFEST]

    url = releases(fake).publish("data-20261106T000000Z", backup, names, "body")

    assert url == "https://x/99"
    assert fake.calls[0].startswith("POST") and fake.calls[0].endswith("/releases")
    assert sum("/assets" in c for c in fake.calls) == 4
    assert fake.calls[-1].startswith("PATCH")
    assert fake.releases[-1]["draft"] is False


def test_failed_upload_deletes_the_draft(backup: Path) -> None:
    fake = FakeGitHub()
    fake.fail_upload = True

    with pytest.raises(BackupError, match="500"):
        releases(fake).publish("data-x", backup, [CATALOG], "body")

    assert fake.calls[-1] == "DELETE /repos/RAkupper/Assignment_for_6700/releases/99"
    assert not any(c.startswith("PATCH") for c in fake.calls)


def test_publish_needs_a_token(backup: Path) -> None:
    with pytest.raises(BackupError, match="BACKUP_GITHUB_TOKEN"):
        releases(FakeGitHub(), token=None).publish("data-x", backup, [CATALOG], "")


def test_prune_keeps_the_newest_and_deletes_release_and_tag() -> None:
    fake = FakeGitHub([release(f"data-2026110{d}T000000Z", rid=d) for d in range(1, 8)])

    removed = releases(fake).prune(keep=5)

    assert removed == ["data-20261102T000000Z", "data-20261101T000000Z"]
    repo = "/repos/RAkupper/Assignment_for_6700"
    deletes = [c for c in fake.calls if c.startswith("DELETE")]
    assert deletes == [
        f"DELETE {repo}/releases/2",
        f"DELETE {repo}/git/refs/tags/data-20261102T000000Z",
        f"DELETE {repo}/releases/1",
        f"DELETE {repo}/git/refs/tags/data-20261101T000000Z",
    ]


def test_blank_token_is_unset_and_a_set_one_stays_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKUP_GITHUB_TOKEN", " ")
    assert BackupSettings(_env_file=None).github_token is None

    monkeypatch.setenv("BACKUP_GITHUB_TOKEN", "github_pat_secret")
    settings = BackupSettings(_env_file=None)
    assert "github_pat_secret" not in repr(settings)


def test_an_unreadable_catalog_is_a_backup_error(backup: Path) -> None:
    (backup / CATALOG).write_bytes(b"not a database")

    with pytest.raises(BackupError, match="unreadable"):
        check_catalog(backup / CATALOG)


def test_a_failure_after_loading_puts_everything_back(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_data_dir(tmp_path / "live", versions=1)
    collections = FakeCollections({"provisions_v1": 10}, live="provisions_v1")

    def broken(archive: Path, data_dir: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("myapp.backup.unpack_data", broken)

    with pytest.raises(OSError, match="disk full"):
        restore(backup, data, collections)

    assert collections.dropped == ["provisions_v2"]
    assert collections.target == "provisions_v1"
    assert versions_in(data / CATALOG) == 1


def test_a_failed_restore_onto_an_empty_machine_leaves_no_catalog(
    backup: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "fresh" / "data"
    collections = FakeCollections({})

    def broken(archive: Path, data_dir: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("myapp.backup.unpack_data", broken)

    with pytest.raises(OSError, match="disk full"):
        restore(backup, data, collections)

    assert collections.points == {}
    assert not (data / CATALOG).exists()
