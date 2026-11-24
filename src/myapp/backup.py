"""Back up the catalog and the live Qdrant collection, and restore them (#52).

Each backup is a `data-<UTC time>` release in the project repo, so a copy outlives
TEAM02 and any one laptop. Run inside the jobs container:
    python -m myapp.backup create --upload      # needs BACKUP_GITHUB_TOKEN
    python -m myapp.backup create --out DIR     # a local copy only
    python -m myapp.backup list
    python -m myapp.backup restore latest       # or a tag, or --from-dir DIR
"""

import argparse
import hashlib
import logging
import sqlite3
import tarfile
import tempfile
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from myapp.config import IngestSettings
from myapp.ingest.catalog import backup as backup_catalog
from myapp.vectordb.collection import (
    QdrantSettings,
    current_target,
    get_client,
    next_version,
    point_alias,
    versioned_name,
)

CATALOG = "catalog.sqlite"
FILES = "data-files.tar.gz"  # everything else in /data: raw downloads, captures
SNAPSHOT = "qdrant.snapshot"
MANIFEST = "manifest.json"
NOT_PACKED = {
    CATALOG,
    f"{CATALOG}-wal",
    f"{CATALOG}-shm",
    f"{CATALOG}-journal",
    "backups",
}

log = logging.getLogger(__name__)


class BackupError(RuntimeError):
    """A backup could not be made, or a copy failed its checks."""


class BackupSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BACKUP_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
        hide_input_in_errors=True,  # keeps the token out of startup errors
    )
    github_repo: str = Field(
        default="RAkupper/Assignment_for_6700", pattern=r"^[\w.-]+/[\w.-]+$"
    )
    github_token: SecretStr | None = None  # fine-grained: this repo, contents write
    keep: int = Field(default=5, ge=1)  # newest data releases kept by prune
    tag_prefix: str = Field(default="data-", min_length=1)
    timeout_s: float = Field(default=300.0, gt=0)

    @field_validator("github_token", mode="before")
    @classmethod
    def blank_token_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class FileEntry(BaseModel):
    sha256: str
    bytes: int


class Manifest(BaseModel):
    """What a backup holds, checked again before anything is restored."""

    created_at: str
    alias: str
    collection: str
    points: int
    embed_model: str | None
    catalog_schema: int
    provision_versions: int
    files: dict[str, FileEntry]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_catalog(src: Path, dest: Path, frozen: bool = False) -> None:
    """Copy with SQLite's backup API: a plain cp can catch a half-written WAL.

    ``frozen`` opens a backup file read-only, so it can come from a read-only mount.
    """
    source = f"file:{src}?mode=ro&immutable=1" if frozen else str(src)
    with (
        closing(sqlite3.connect(source, uri=frozen)) as con,
        closing(sqlite3.connect(dest)) as out,
    ):
        con.backup(out)


def check_catalog(path: Path) -> tuple[int, int]:
    """Return the schema version and provision version count; raise if damaged."""
    uri = f"file:{path}?mode=ro&immutable=1"  # a backup never changes; works read-only
    try:
        with closing(sqlite3.connect(uri, uri=True)) as con:
            result = con.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise BackupError(f"catalog failed its integrity check: {result}")
            schema = con.execute("SELECT max(version) FROM schema_migrations")
            count = con.execute("SELECT count(*) FROM provision_versions")
            return int(schema.fetchone()[0] or 0), int(count.fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"catalog in {path.parent} is unreadable: {exc}") from exc


def pack_data(data_dir: Path, dest: Path) -> None:
    with tarfile.open(dest, "w:gz") as tar:
        for item in sorted(data_dir.iterdir()):
            if item.name not in NOT_PACKED:
                tar.add(item, arcname=item.name)


def unpack_data(archive: Path, data_dir: Path) -> None:
    with tarfile.open(archive) as tar:
        tar.extractall(data_dir, filter="data")  # refuses absolute paths and ../


class Collections:
    """The Qdrant calls a backup needs. Snapshots stream over HTTP, never in memory."""

    def __init__(
        self, settings: QdrantSettings, transport: httpx2.BaseTransport | None = None
    ) -> None:
        if settings.url is None:
            raise BackupError("QDRANT_URL is unset: an in-memory index has no backup")
        self.alias = settings.alias
        self.client = get_client(settings)
        headers = {}
        if settings.api_key is not None:
            headers["api-key"] = settings.api_key.get_secret_value()
        self._http = httpx2.Client(
            base_url=str(settings.url),
            headers=headers,
            timeout=httpx2.Timeout(300.0, connect=10.0),
            transport=transport,
        )

    def live(self) -> str:
        name = current_target(self.client, self.alias)
        if name is None:
            raise BackupError(
                f"alias {self.alias} points at nothing; nothing to back up"
            )
        return name

    def count(self, name: str) -> int:
        return self.client.count(name, exact=True).count

    def embed_model(self, name: str) -> str | None:
        records, _ = self.client.scroll(name, limit=1, with_payload=["embed_model"])
        value = (records[0].payload or {}).get("embed_model") if records else None
        return value if isinstance(value, str) else None

    def next_name(self) -> str:
        return versioned_name(self.alias, next_version(self.client, self.alias))

    def save(self, name: str, dest: Path) -> None:
        snapshot = self.client.create_snapshot(name, wait=True)
        if snapshot is None:
            raise BackupError(f"Qdrant made no snapshot of {name}")
        try:
            url = f"collections/{name}/snapshots/{snapshot.name}"
            with self._http.stream("GET", url) as response, dest.open("wb") as out:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    out.write(chunk)
        finally:
            self.client.delete_snapshot(
                name, snapshot.name
            )  # the file sits on the same disk

    def load(self, snapshot: Path, name: str) -> None:
        with snapshot.open("rb") as f:
            response = self._http.post(
                f"collections/{name}/snapshots/upload",
                params={"priority": "snapshot", "wait": "true"},
                files={"snapshot": (snapshot.name, f, "application/octet-stream")},
            )
        response.raise_for_status()
        for leftover in self.client.list_snapshots(name):  # Qdrant keeps the upload
            self.client.delete_snapshot(name, leftover.name)

    def drop(self, name: str) -> None:
        self.client.delete_collection(name)

    def activate(self, name: str) -> str | None:
        return point_alias(self.client, self.alias, name)

    def close(self) -> None:
        self._http.close()
        self.client.close()


class Releases:
    """Data releases in the project repo. Reading needs no token; the repo is public."""

    API = "https://api.github.com"
    UPLOADS = "https://uploads.github.com"

    def __init__(
        self, settings: BackupSettings, transport: httpx2.BaseTransport | None = None
    ) -> None:
        self.repo = settings.github_repo
        self.prefix = settings.tag_prefix
        self.can_write = settings.github_token is not None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dsan6700-backup",
        }
        if settings.github_token is not None:
            headers["Authorization"] = (
                f"Bearer {settings.github_token.get_secret_value()}"
            )
        self._http = httpx2.Client(
            headers=headers,
            timeout=settings.timeout_s,
            follow_redirects=True,  # asset downloads redirect to storage
            transport=transport,
        )

    def _call(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        response = self._http.request(method, url, **kwargs)
        if response.is_error:
            raise BackupError(
                f"GitHub answered {response.status_code} to {method} {url}"
            )
        return response

    def published(self) -> list[dict[str, Any]]:
        """Published data releases, newest first."""
        url = f"{self.API}/repos/{self.repo}/releases"
        found = self._call("GET", url, params={"per_page": 100}).json()
        ours = [
            r for r in found if r["tag_name"].startswith(self.prefix) and not r["draft"]
        ]
        return sorted(ours, key=lambda r: r["tag_name"], reverse=True)

    def find(self, tag: str) -> dict[str, Any]:
        if tag == "latest":
            releases = self.published()
            if not releases:
                raise BackupError(f"no {self.prefix}* releases in {self.repo}")
            return releases[0]
        url = f"{self.API}/repos/{self.repo}/releases/tags/{tag}"
        release: dict[str, Any] = self._call("GET", url).json()
        return release

    def publish(self, tag: str, folder: Path, names: Sequence[str], body: str) -> str:
        """Upload to a draft, then publish, so a half-uploaded backup is never seen."""
        if not self.can_write:
            raise BackupError(
                "set BACKUP_GITHUB_TOKEN in .env.backup to upload backups"
            )
        url = f"{self.API}/repos/{self.repo}/releases"
        draft = self._call(
            "POST",
            url,
            json={
                "tag_name": tag,
                "name": tag,
                "body": body,
                "draft": True,
                "prerelease": True,
            },
        ).json()
        try:
            for name in names:
                self._call(
                    "POST",
                    f"{self.UPLOADS}/repos/{self.repo}/releases/{draft['id']}/assets",
                    params={"name": name},
                    headers={"Content-Type": "application/octet-stream"},
                    content=(folder / name).read_bytes(),
                )
            done = self._call(
                "PATCH",
                f"{url}/{draft['id']}",
                json={"draft": False, "make_latest": "false"},
            ).json()
        except Exception:
            self._call("DELETE", f"{url}/{draft['id']}")
            raise
        return str(done["html_url"])

    def download(self, release: dict[str, Any], dest: Path) -> None:
        for asset in release["assets"]:
            with self._http.stream("GET", asset["browser_download_url"]) as response:
                if response.is_error:
                    raise BackupError(
                        f"download of {asset['name']} failed: {response.status_code}"
                    )
                with (dest / asset["name"]).open("wb") as out:
                    for chunk in response.iter_bytes():
                        out.write(chunk)

    def prune(self, keep: int) -> list[str]:
        """Delete all but the newest `keep` data releases and their tags."""
        removed = []
        for old in self.published()[keep:]:
            self._call("DELETE", f"{self.API}/repos/{self.repo}/releases/{old['id']}")
            self._call(
                "DELETE",
                f"{self.API}/repos/{self.repo}/git/refs/tags/{old['tag_name']}",
            )
            removed.append(old["tag_name"])
        return removed

    def close(self) -> None:
        self._http.close()


def create(out: Path, data_dir: Path, collections: Collections) -> Manifest:
    """Write the catalog, the rest of /data, a snapshot and a manifest to `out`."""
    copy_catalog(data_dir / CATALOG, out / CATALOG)
    schema, versions = check_catalog(out / CATALOG)
    pack_data(data_dir, out / FILES)
    name = collections.live()
    points = collections.count(name)
    collections.save(name, out / SNAPSHOT)
    manifest = Manifest(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        alias=collections.alias,
        collection=name,
        points=points,
        embed_model=collections.embed_model(name),
        catalog_schema=schema,
        provision_versions=versions,
        files={
            f: FileEntry(sha256=sha256(out / f), bytes=(out / f).stat().st_size)
            for f in (CATALOG, FILES, SNAPSHOT)
        },
    )
    (out / MANIFEST).write_text(manifest.model_dump_json(indent=2))
    return manifest


def verify(folder: Path) -> Manifest:
    """Check every file against the manifest before anything is replaced."""
    try:
        manifest = Manifest.model_validate_json((folder / MANIFEST).read_text())
    except (OSError, ValueError) as exc:
        raise BackupError(f"no readable {MANIFEST} in {folder}") from exc
    for name, entry in manifest.files.items():
        path = folder / name
        if not path.is_file() or sha256(path) != entry.sha256:
            raise BackupError(f"{name} is missing or does not match the manifest")
    if check_catalog(folder / CATALOG) != (
        manifest.catalog_schema,
        manifest.provision_versions,
    ):
        raise BackupError("catalog schema or row count does not match the manifest")
    return manifest


def restore(
    folder: Path, data_dir: Path, collections: Collections, activate: bool = True
) -> list[str]:
    """Load a verified backup. Existing collections are never overwritten.

    The snapshot goes into the next `<alias>_v<N>` and is counted before anything
    else changes. The current catalog is copied to /data/backups first, so both
    halves can be rolled back.
    """
    manifest = verify(folder)
    lines = [f"verified backup of {manifest.collection} from {manifest.created_at}"]
    target = collections.next_name()
    collections.load(folder / SNAPSHOT, target)
    if (got := collections.count(target)) != manifest.points:
        collections.drop(target)
        raise BackupError(f"{target} has {got} points, the backup {manifest.points}")
    lines.append(f"loaded {target}: {manifest.points} points")
    data_dir.mkdir(parents=True, exist_ok=True)
    live = data_dir / CATALOG
    saved: Path | None = None
    try:
        if live.exists():
            with closing(sqlite3.connect(live)) as con:
                saved = backup_catalog(con, data_dir / "backups", "restore")
            lines.append(f"saved the current catalog to {saved}")
        copy_catalog(folder / CATALOG, live, frozen=True)
        unpack_data(folder / FILES, data_dir)
        lines.append(f"restored the catalog: {manifest.provision_versions} versions")
        if activate:
            previous = collections.activate(target)
            lines.append(f"alias {collections.alias}: {previous} -> {target}")
    except Exception:
        collections.drop(target)  # a half restore leaves things as they were
        if saved is not None:
            copy_catalog(saved, live)
        else:
            live.unlink(missing_ok=True)  # there was no catalog before
        raise
    return lines


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create", help="back up the catalog and the live collection")
    make.add_argument("--out", type=Path, help="also keep the files in this folder")
    make.add_argument("--upload", action="store_true", help="publish a data release")
    sub.add_parser("list", help="show the data releases, newest first")
    back = sub.add_parser("restore", help="restore a data release or a local folder")
    back.add_argument("tag", nargs="?", default="latest")
    back.add_argument("--from-dir", type=Path, help="a folder made by create --out")
    back.add_argument(
        "--no-activate", action="store_true", help="leave the alias alone"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2"):  # one line per request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = BackupSettings()
    releases = Releases(settings)
    try:
        if args.command == "list":
            for r in releases.published():
                sizes = sum(a["size"] for a in r["assets"]) // 1_000_000
                print(f"{r['tag_name']}  {sizes} MB  {r['html_url']}")
            return
        data_dir = IngestSettings().data_dir
        collections = Collections(QdrantSettings())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp)
                if args.command == "create":
                    if not args.upload and args.out is None:
                        parser.error("create needs --upload, --out DIR, or both")
                    folder = args.out or work
                    folder.mkdir(parents=True, exist_ok=True)
                    manifest = create(folder, data_dir, collections)
                    log.info(
                        "backed up %s (%d points)", manifest.collection, manifest.points
                    )
                    if args.upload:
                        tag = f"{settings.tag_prefix}{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
                        body = f"```json\n{manifest.model_dump_json(indent=2)}\n```"
                        names = [CATALOG, FILES, SNAPSHOT, MANIFEST]
                        log.info(
                            "published %s", releases.publish(tag, folder, names, body)
                        )
                        for gone in releases.prune(settings.keep):
                            log.info("pruned %s", gone)
                    return
                folder = args.from_dir or work
                if args.from_dir is None:
                    release = releases.find(args.tag)
                    log.info("downloading %s", release["tag_name"])
                    releases.download(release, work)
                for line in restore(
                    folder, data_dir, collections, not args.no_activate
                ):
                    log.info(line)
        except BackupError as exc:
            raise SystemExit(f"error: {exc}") from exc
        finally:
            collections.close()
    finally:
        releases.close()


if __name__ == "__main__":
    main()
