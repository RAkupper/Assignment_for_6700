"""Shared fakes for vector DB tests: no model download, no Qdrant server."""

import hashlib
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from qdrant_client import models

from myapp.vectordb.encoders import Encoded

SCHEMA = Path(__file__).with_name("catalog_schema.sql")
DIM = 16


def _token_id(token: str) -> int:
    return int.from_bytes(hashlib.sha256(token.encode()).digest()[:4], "big")


def _encode(text: str) -> Encoded:
    tokens = re.findall(r"[a-z0-9-]+", text.lower())
    dense = [0.01] * DIM
    for t in tokens:
        dense[_token_id(t) % DIM] += 1.0  # shared words mean nearby vectors
    counts: dict[int, float] = {}
    for t in tokens:
        counts[_token_id(t)] = counts.get(_token_id(t), 0.0) + 1.0
    sparse = models.SparseVector(indices=list(counts), values=list(counts.values()))
    return Encoded(dense=dense, sparse=sparse)


class FakeEncoder:
    """Bag-of-words vectors, so ranking is testable; records what it embedded."""

    dense_model = "fake-encoder"
    dim = DIM

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[Encoded]:
        self.texts.extend(texts)
        return [_encode(t) for t in texts]

    def embed_query(self, text: str) -> Encoded:
        return _encode(text)


Provision = tuple[str, str, str, str, str]  # id, source, heading, authority, visas
Version = tuple[
    str, int, str, str, str, str | None
]  # id, version, text, status, from, to


def make_catalog(
    path: Path,
    provisions: list[Provision],
    versions: list[Version],
    failed: list[Version] | None = None,
) -> Path:
    """A catalog with one succeeded run, plus an optional failed one."""
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA.read_text())
        con.executemany(
            "INSERT INTO runs VALUES (?, ?, ?)",
            [(1, "ecfr", "succeeded"), (2, "ecfr", "failed")],
        )
        con.executemany("INSERT INTO fetches VALUES (?, ?)", [(10, 1), (20, 2)])
        con.executemany("INSERT INTO provisions VALUES (?, ?, ?, ?, ?)", provisions)
        rows = [(*v, 10) for v in versions] + [(*v, 20) for v in failed or []]
        con.executemany(
            "INSERT INTO provision_versions VALUES (?, ?, ?, ?, ?, ?, ?)", rows
        )
    con.close()
    return path
