"""Versioned Qdrant collections for provision chunks (docs/ingestion.md).

Inspect and roll back from the command line:
    python -m myapp.vectordb.collection status
    python -m myapp.vectordb.collection activate provisions_v1
"""

import argparse
import re
from collections.abc import Sequence

from loguru import logger
from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import QdrantClient, models

from myapp.observability import setup_logging

DENSE = "dense"
SPARSE = "sparse"

PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {  # handoff contract
    "provision_id": models.PayloadSchemaType.KEYWORD,
    "source": models.PayloadSchemaType.KEYWORD,
    "authority": models.PayloadSchemaType.KEYWORD,
    "status": models.PayloadSchemaType.KEYWORD,
    "visa_types": models.PayloadSchemaType.KEYWORD,
    "valid_from": models.PayloadSchemaType.DATETIME,
    "valid_to": models.PayloadSchemaType.DATETIME,
}


class QdrantSettings(BaseSettings):
    """Where Qdrant is and which alias the app queries, from QDRANT_* variables."""

    model_config = SettingsConfigDict(
        env_prefix="QDRANT_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )
    url: HttpUrl | None = HttpUrl("http://localhost:6333")  # None means in-memory
    api_key: SecretStr | None = None
    alias: str = Field(default="provisions", min_length=1)  # the name the app queries
    # Every request to Qdrant gives up after this many seconds. A search takes well
    # under 0.2 s on TEAM02, so 5 s fails fast on a stuck Qdrant; jobs, which write
    # in bulk and take snapshots, get 60 s from compose.yml.
    timeout_s: int = Field(default=5, ge=1, le=300)


def get_client(settings: QdrantSettings) -> QdrantClient:
    """Connect to the configured Qdrant, or an in-memory one when no URL is set."""
    if settings.url is None:
        return QdrantClient(location=":memory:")
    key = settings.api_key.get_secret_value() if settings.api_key else None
    return QdrantClient(url=str(settings.url), api_key=key, timeout=settings.timeout_s)


def versioned_name(alias: str, version: int) -> str:
    """The collection name for one version: provisions_v3."""
    return f"{alias}_v{version}"


def versions(client: QdrantClient, alias: str) -> list[int]:
    """Version numbers of every `<alias>_v<N>` collection, ascending."""
    pattern = re.compile(rf"{re.escape(alias)}_v(\d+)")
    found = (pattern.fullmatch(c.name) for c in client.get_collections().collections)
    return sorted(int(m.group(1)) for m in found if m)


def next_version(client: QdrantClient, alias: str) -> int:
    """One past the highest existing version, or 1 when there is none."""
    return max(versions(client, alias), default=0) + 1


def create_collection(client: QdrantClient, name: str, dense_dim: int) -> None:
    """Create the collection and its payload indexes. No-op if it already exists."""
    if client.collection_exists(name):
        return
    client.create_collection(
        collection_name=name,
        vectors_config={
            DENSE: models.VectorParams(size=dense_dim, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={  # BM25 weights; Qdrant applies IDF at query time
            SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )
    for field, schema in PAYLOAD_INDEXES.items():
        client.create_payload_index(name, field_name=field, field_schema=schema)


def dense_size(client: QdrantClient, name: str) -> int:
    """The dense vector size a collection was built with."""
    vectors = client.get_collection(name).config.params.vectors
    assert isinstance(vectors, dict)  # always named vectors here
    return vectors[DENSE].size


def current_target(client: QdrantClient, alias: str) -> str | None:
    """Return the collection the alias points at, or None."""
    for a in client.get_aliases().aliases:
        if a.alias_name == alias:
            return a.collection_name
    return None


def point_alias(client: QdrantClient, alias: str, name: str) -> str | None:
    """Atomically move the alias to `name`. Returns the previous target for rollback."""
    if not client.collection_exists(name):
        raise ValueError(f"no collection named {name}")
    previous = current_target(client, alias)
    ops: list[models.AliasOperations] = []
    if previous is not None:
        delete = models.DeleteAlias(alias_name=alias)
        ops.append(models.DeleteAliasOperation(delete_alias=delete))
    ops.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(collection_name=name, alias_name=alias)
        )
    )
    client.update_collection_aliases(change_aliases_operations=ops)  # atomic swap
    return previous


def status(client: QdrantClient, alias: str) -> list[str]:
    """Lines showing where the alias points and each version's point count."""
    live = current_target(client, alias)
    lines = [f"alias {alias} -> {live}"]
    for v in versions(client, alias):
        name = versioned_name(alias, v)
        mark = "  (live)" if name == live else ""
        lines.append(f"{name}: {client.count(name).count} points{mark}")
    return lines


def main(argv: Sequence[str] | None = None) -> None:
    """Print the status, or move the alias to another collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the live collection and every version")
    act = sub.add_parser("activate", help="point the alias at a collection")
    act.add_argument("name")
    args = parser.parse_args(argv)
    setup_logging(json_lines=False)

    settings = QdrantSettings()
    client = get_client(settings)
    if args.command == "activate":
        previous = point_alias(client, settings.alias, args.name)
        logger.info(f"alias {settings.alias}: {previous} -> {args.name}")
    else:
        logger.info("\n".join(status(client, settings.alias)))


if __name__ == "__main__":
    main()
