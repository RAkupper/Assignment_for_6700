"""Create and version the Qdrant collection for provision chunks (docs/ingestion.md)."""

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import QdrantClient, models

DENSE = "dense"
SPARSE = "sparse"

# Fields from the ingestion handoff contract, indexed so filters run before search
PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "provision_id": models.PayloadSchemaType.KEYWORD,
    "source": models.PayloadSchemaType.KEYWORD,
    "authority": models.PayloadSchemaType.KEYWORD,
    "status": models.PayloadSchemaType.KEYWORD,
    "visa_types": models.PayloadSchemaType.KEYWORD,
    "valid_from": models.PayloadSchemaType.DATETIME,
    "valid_to": models.PayloadSchemaType.DATETIME,
}


class QdrantSettings(BaseSettings):
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
    dense_dim: int = Field(default=384, gt=0)  # BAAI/bge-small-en-v1.5


def get_client(settings: QdrantSettings) -> QdrantClient:
    if settings.url is None:
        return QdrantClient(location=":memory:")
    key = settings.api_key.get_secret_value() if settings.api_key else None
    return QdrantClient(url=str(settings.url), api_key=key)


def versioned_name(alias: str, version: int) -> str:
    return f"{alias}_v{version}"


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


def current_target(client: QdrantClient, alias: str) -> str | None:
    """Return the collection the alias points at, or None."""
    for a in client.get_aliases().aliases:
        if a.alias_name == alias:
            return a.collection_name
    return None


def point_alias(client: QdrantClient, alias: str, name: str) -> str | None:
    """Atomically move the alias to `name`. Returns the previous target for rollback."""
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


def setup(settings: QdrantSettings, version: int = 1) -> str:
    """Create `<alias>_v<version>` and point the alias at it if nothing else is live."""
    client = get_client(settings)
    name = versioned_name(settings.alias, version)
    create_collection(client, name, settings.dense_dim)
    if current_target(client, settings.alias) is None:
        point_alias(client, settings.alias, name)
    return name


if __name__ == "__main__":
    print(setup(QdrantSettings()))
