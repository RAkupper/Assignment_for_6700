"""Shared fakes for vector DB tests: no model download, no Qdrant server."""

import hashlib
import re
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from qdrant_client import models

from myapp.ingest.catalog import connect, migrate
from myapp.vectordb.encoders import Encoded

DIM = 64  # wide enough that few words share a slot


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


Provision = tuple[str, ...]  # id, source, heading_path, authority, visas[, parent]
Version = tuple[
    str, int, str, str, str | None, str | None
]  # id, v, text, status, from, to


def make_catalog(
    path: Path,
    provisions: list[Provision],
    versions: list[Version],
    failed: list[Version] | None = None,
) -> Path:
    """A catalog built by the real migrations: one good parse run, one failed one."""
    migrate(path, path.parent / "backups")
    with closing(connect(path)) as con:
        con.executemany(
            "INSERT INTO runs (run_id, source, command, started_at, status) "
            "VALUES (?, 'ecfr', ?, 'now', ?)",
            [
                (1, "fetch", "succeeded"),
                (2, "parse", "succeeded"),
                (3, "parse", "failed"),
            ],
        )
        con.executemany(
            "INSERT INTO fetches (fetch_id, run_id, url, fetched_at, accepted, "
            "parsed_by_run) VALUES (?, 1, ?, 'now', 1, ?)",
            [(10, "https://example.test/a", 2), (20, "https://example.test/b", 3)],
        )
        con.executemany(
            "INSERT INTO provisions (provision_id, source, parent_id, ordinal, "
            "heading_path, authority, visa_types) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (p[0], p[1], p[5] if len(p) > 5 else None, i, p[2], p[3], p[4])
                for i, p in enumerate(provisions)
            ],
        )
        rows = [(*v, 10) for v in versions] + [(*v, 20) for v in failed or []]
        con.executemany(
            "INSERT INTO provision_versions (provision_id, version, text, "
            "content_hash, status, valid_from, valid_to, fetch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (r[0], r[1], r[2], hashlib.sha256(r[2].encode()).hexdigest(), *r[3:])
                for r in rows
            ],
        )
        con.commit()
    return path
