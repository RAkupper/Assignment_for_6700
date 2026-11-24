"""Dense (BGE) and sparse (BM25) encoders shared by the loader and search."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastembed import SparseEmbedding, SparseTextEmbedding, TextEmbedding
from numpy.typing import NDArray
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import models

QUERY_PREFIX = (  # BGE is asymmetric: queries only, never documents
    "Represent this sentence for searching relevant passages: "
)


class EmbeddingSettings(BaseSettings):
    """Which models to load and where to cache them, from EMBED_* variables."""

    model_config = SettingsConfigDict(
        env_prefix="EMBED_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )
    dense_model: str = Field(default="BAAI/bge-small-en-v1.5", min_length=1)
    sparse_model: str = Field(default="Qdrant/bm25", min_length=1)
    cache_dir: Path | None = None  # a volume in Docker so weights download once
    batch_size: int = Field(default=64, gt=0)


@dataclass(frozen=True)
class Encoded:
    """One text as a dense BGE vector and a sparse BM25 vector."""

    dense: list[float]
    sparse: models.SparseVector


class Encoder:
    """Load once per process; encoding is CPU-bound, so call it off the event loop."""

    def __init__(self, settings: EmbeddingSettings) -> None:
        """Load both models, downloading them to the cache the first time."""
        cache = str(settings.cache_dir) if settings.cache_dir else None
        self.dense_model = settings.dense_model
        self.dim = TextEmbedding.get_embedding_size(settings.dense_model)
        self.batch_size = settings.batch_size
        self._dense = TextEmbedding(settings.dense_model, cache_dir=cache)
        self._sparse = SparseTextEmbedding(settings.sparse_model, cache_dir=cache)

    def embed_documents(self, texts: Sequence[str]) -> list[Encoded]:
        """Encode provision texts in batches, without the query prefix."""
        dense = self._dense.embed(texts, batch_size=self.batch_size)  # no prefix
        sparse = self._sparse.embed(texts, batch_size=self.batch_size)  # BM25 tf
        return _pair(dense, sparse)

    def embed_query(self, text: str) -> Encoded:
        """Encode a question, with the BGE query prefix on the dense side."""
        dense = self._dense.embed([QUERY_PREFIX + text])  # fastembed won't add it
        sparse = self._sparse.query_embed(text)  # token ids; IDF applied by Qdrant
        return _pair(dense, sparse)[0]


def _pair(
    dense: Iterable[NDArray[Any]], sparse: Iterable[SparseEmbedding]
) -> list[Encoded]:
    return [
        Encoded(
            dense=d.tolist(),
            sparse=models.SparseVector(
                indices=s.indices.tolist(), values=s.values.tolist()
            ),
        )
        for d, s in zip(dense, sparse, strict=True)
    ]
