"""Build a new Qdrant collection version from catalog.sqlite.

Run as a one-off task, never at app startup:
    docker compose run --rm app python -m myapp.vectordb.loader --activate
"""

import argparse
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from myapp.vectordb.collection import (
    DENSE,
    SPARSE,
    QdrantSettings,
    create_collection,
    current_target,
    get_client,
    next_version,
    point_alias,
    versioned_name,
)
from myapp.vectordb.encoders import EmbeddingSettings, Encoded, Encoder

log = logging.getLogger(__name__)

POINT_NAMESPACE = uuid.UUID("5b6f2c1e-6f1a-4b8e-9a8e-2f6d1c3b7a90")  # never change

LEAD_WORDS = 60  # parent lead-in kept short so chunks stay under 512 tokens
SCHEMA_REQUIRED = 2  # parsed_by_run arrived in migration 0002

VERSIONS_SQL = """
SELECT p.provision_id, p.source, p.heading_path, p.authority, p.visa_types,
       v.version, v.text, v.status, v.valid_from, v.valid_to,
       (SELECT pv.text FROM provision_versions pv
         WHERE pv.provision_id = p.parent_id
           AND coalesce(pv.valid_from, '') <= coalesce(v.valid_from, '')
           AND (pv.valid_to IS NULL OR pv.valid_to > coalesce(v.valid_from, ''))
         ORDER BY pv.version DESC LIMIT 1) AS parent_text
FROM provision_versions v
JOIN provisions p ON p.provision_id = v.provision_id
JOIN fetches f ON f.fetch_id = v.fetch_id
JOIN runs r ON r.run_id = f.parsed_by_run
WHERE r.status = 'succeeded'
ORDER BY p.provision_id, v.version
"""


class CatalogTooOldError(RuntimeError):
    """The catalog predates the schema this loader reads."""


class DocumentEncoder(Protocol):
    dense_model: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[Encoded]: ...


@dataclass(frozen=True)
class Chunk:
    provision_id: str
    version: int
    chunk: int
    text: str  # what gets embedded: heading path prepended
    payload: dict[str, Any]


def point_id(provision_id: str, version: int, chunk: int) -> str:
    """UUIDv5 per the handoff contract, so reloading updates in place."""
    return str(uuid.uuid5(POINT_NAMESPACE, f"{provision_id}|{version}|{chunk}"))


def to_rfc3339(value: str | None) -> str | None:
    """Dates and naive timestamps become UTC, as Qdrant's datetime index wants."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _lead(text: str | None) -> str:
    words = (text or "").split()
    return " ".join(words[:LEAD_WORDS]) + (" ..." if len(words) > LEAD_WORDS else "")


def read_chunks(catalog: Path) -> Iterator[Chunk]:
    """One chunk per provision version, embedded with its parent's lead-in."""
    uri = f"file:{catalog}?mode=ro"  # the loader never writes the catalog
    with closing(sqlite3.connect(uri, uri=True)) as con:
        con.row_factory = sqlite3.Row
        have = con.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        if (have or 0) < SCHEMA_REQUIRED:
            raise CatalogTooOldError(
                f"catalog schema is at {have}, the loader needs {SCHEMA_REQUIRED}: "
                "run python -m myapp.ingest.catalog migrate"
            )
        for row in con.execute(VERSIONS_SQL):
            heading = row["heading_path"] or ""
            lead = _lead(row["parent_text"])
            payload = {
                "provision_id": row["provision_id"],
                "version": row["version"],
                "chunk": 0,
                "source": row["source"],
                "authority": row["authority"],
                "status": row["status"],
                "visa_types": json.loads(row["visa_types"] or "[]"),
                "valid_from": to_rfc3339(row["valid_from"]),
                "valid_to": to_rfc3339(row["valid_to"]),  # None means in force now
                "heading_path": heading,
                "parent_text": lead,
                "text": row["text"],
            }
            embedded = " ".join(
                x for x in (f"{heading}." if heading else "", lead, row["text"]) if x
            )
            yield Chunk(
                provision_id=row["provision_id"],
                version=row["version"],
                chunk=0,
                text=embedded,
                payload=payload,
            )


def _batches(chunks: Iterator[Chunk], size: int) -> Iterator[list[Chunk]]:
    while batch := list(islice(chunks, size)):
        yield batch


def load(
    client: QdrantClient,
    encoder: DocumentEncoder,
    catalog: Path,
    collection: str,
    batch_size: int = 128,
) -> int:
    """Embed and upsert every chunk into `collection`. Returns the point count."""
    total = 0
    for batch in _batches(read_chunks(catalog), batch_size):
        encoded = encoder.embed_documents([c.text for c in batch])
        points = [
            models.PointStruct(
                id=point_id(c.provision_id, c.version, c.chunk),
                vector={DENSE: e.dense, SPARSE: e.sparse},
                payload=c.payload | {"embed_model": encoder.dense_model},
            )
            for c, e in zip(batch, encoded, strict=True)
        ]
        client.upsert(collection, points=points, wait=True)
        total += len(points)
        log.info("upserted %d points (%d total)", len(points), total)  # counts only
    return total


def build(
    client: QdrantClient,
    encoder: DocumentEncoder,
    catalog: Path,
    alias: str,
    version: int | None = None,
    replace: bool = False,
    activate: bool = False,
) -> str:
    """Load into `<alias>_v<version>` (next free version by default). Returns its name.

    Never touches the live collection: a rebuild is always a new or non-live version.
    """
    name = versioned_name(alias, version or next_version(client, alias))
    if client.collection_exists(name):
        if name == current_target(client, alias):
            raise ValueError(f"{name} is live; build a new version instead")
        if client.count(name).count and not replace:
            raise ValueError(f"{name} already has points; pass replace to rebuild it")
        client.delete_collection(name)  # empty or partial, and not live
    create_collection(client, name, encoder.dim)
    total = load(client, encoder, catalog, name)
    log.info("loaded %d points into %s", total, name)
    if activate:
        previous = point_alias(client, alias, name)
        log.info("alias %s: %s -> %s", alias, previous, name)
    return name


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument("--version", type=int)  # default: next free version
    parser.add_argument("--replace", action="store_true")  # rebuild a non-live one
    parser.add_argument("--activate", action="store_true")  # move the alias after
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request

    settings = QdrantSettings()
    try:
        build(
            get_client(settings),
            Encoder(EmbeddingSettings()),
            args.catalog,
            settings.alias,
            version=args.version,
            replace=args.replace,
            activate=args.activate,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
