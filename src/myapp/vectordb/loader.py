"""Load provision versions from catalog.sqlite into a new Qdrant collection.

Run as a one-off task, never at app startup:
    docker compose run --rm app python -m myapp.vectordb.loader --version 2 --activate
"""

import argparse
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from myapp.vectordb.collection import (
    DENSE,
    SPARSE,
    QdrantSettings,
    create_collection,
    get_client,
    point_alias,
    versioned_name,
)
from myapp.vectordb.encoders import EmbeddingSettings, Encoded, Encoder

log = logging.getLogger(__name__)

POINT_NAMESPACE = uuid.UUID("5b6f2c1e-6f1a-4b8e-9a8e-2f6d1c3b7a90")  # never change

VERSIONS_SQL = """
SELECT p.provision_id, p.source, p.heading_path, p.authority, p.visa_types,
       v.version, v.text, v.status, v.valid_from, v.valid_to
FROM provision_versions v
JOIN provisions p ON p.provision_id = v.provision_id
JOIN fetches f ON f.fetch_id = v.fetch_id
JOIN runs r ON r.run_id = f.run_id
WHERE r.status = 'succeeded'
ORDER BY p.provision_id, v.version
"""


class DocumentEncoder(Protocol):
    dense_model: str

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


def read_chunks(catalog: Path) -> Iterator[Chunk]:
    """One chunk per provision version for now. Never crosses a provision."""
    uri = f"file:{catalog}?mode=ro"  # the loader never writes the catalog
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute(VERSIONS_SQL):
            heading = row["heading_path"] or ""
            payload = {
                "provision_id": row["provision_id"],
                "version": row["version"],
                "chunk": 0,
                "source": row["source"],
                "authority": row["authority"],
                "status": row["status"],
                "visa_types": json.loads(row["visa_types"] or "[]"),
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],  # None means in force now
                "heading_path": heading,
                "text": row["text"],
            }
            yield Chunk(
                provision_id=row["provision_id"],
                version=row["version"],
                chunk=0,
                text=f"{heading}. {row['text']}" if heading else row["text"],
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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("data/catalog.sqlite"))
    parser.add_argument("--version", type=int, required=True)  # builds <alias>_vN
    parser.add_argument("--activate", action="store_true")  # move the alias after
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    qs = QdrantSettings()
    client = get_client(qs)
    name = versioned_name(qs.alias, args.version)
    if client.collection_exists(name) and client.count(name).count > 0:
        raise SystemExit(f"{name} already has points; build a new version instead")
    create_collection(client, name, qs.dense_dim)
    total = load(client, Encoder(EmbeddingSettings()), args.catalog, name)
    log.info("loaded %d points into %s", total, name)
    if args.activate:
        previous = point_alias(client, qs.alias, name)
        log.info(
            "alias %s: %s -> %s (roll back by pointing it at the old one)",
            qs.alias,
            previous,
            name,
        )


if __name__ == "__main__":
    main()
