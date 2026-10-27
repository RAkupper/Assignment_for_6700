import hashlib
import sqlite3
import warnings
from collections.abc import Sequence
from pathlib import Path

import pytest
from qdrant_client import QdrantClient, models

from myapp.vectordb.collection import (
    create_collection,
    current_target,
    dense_size,
    status,
    versions,
)
from myapp.vectordb.encoders import Encoded
from myapp.vectordb.loader import build, load, point_id, to_rfc3339

SCHEMA = Path(__file__).with_name("catalog_schema.sql")
DIM = 4
OPT = "8 CFR 214.2(f)(10)(ii)"


class FakeEncoder:
    """Deterministic vectors from a hash; records what it was asked to embed."""

    dense_model = "fake-encoder"
    dim = DIM

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_documents(self, texts: Sequence[str]) -> list[Encoded]:
        self.texts.extend(texts)
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            dense = [b / 255 + 0.01 for b in h[:DIM]]
            sparse = models.SparseVector(indices=[h[0], 256 + h[1]], values=[1.0, 1.0])
            out.append(Encoded(dense=dense, sparse=sparse))
        return out


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite"
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA.read_text())
        con.executemany(
            "INSERT INTO runs VALUES (?, ?, ?)",
            [(1, "ecfr", "succeeded"), (2, "ecfr", "failed")],
        )
        con.executemany("INSERT INTO fetches VALUES (?, ?)", [(10, 1), (20, 2)])
        con.executemany(
            "INSERT INTO provisions VALUES (?, ?, ?, ?, ?)",
            [
                (
                    OPT,
                    "ecfr",
                    "Part 214 > F-1 > Practical training",
                    "regulation",
                    '["F-1", "F-2"]',
                ),
                (
                    "8 CFR 214.2(j)(1)",
                    "ecfr",
                    "Part 214 > J-1",
                    "regulation",
                    '["J-1", "J-2"]',
                ),
            ],
        )
        con.executemany(
            "INSERT INTO provision_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    OPT,
                    1,
                    "Old OPT text.",
                    "superseded",
                    "2020-01-01T00:00:00Z",
                    "2024-01-01T00:00:00Z",
                    10,
                ),
                (
                    OPT,
                    2,
                    "Current OPT text.",
                    "in_force",
                    "2024-01-01",  # date only, as the catalog will store it
                    None,
                    10,
                ),
                (
                    "8 CFR 214.2(j)(1)",
                    1,
                    "Exchange visitor text.",
                    "in_force",
                    "2020-01-01T00:00:00Z",
                    None,
                    10,
                ),
                (
                    "8 CFR 214.2(j)(1)",
                    2,
                    "From a failed run.",
                    "in_force",
                    "2026-01-01T00:00:00Z",
                    None,
                    20,
                ),
            ],
        )
    return path


@pytest.fixture
def client() -> QdrantClient:
    c = QdrantClient(location=":memory:")
    with warnings.catch_warnings():  # payload indexes are a no-op locally
        warnings.simplefilter("ignore", UserWarning)
        create_collection(c, "provisions_v1", DIM)
    return c


def test_loads_every_version_from_succeeded_runs_only(
    client: QdrantClient, catalog: Path
) -> None:
    assert load(client, FakeEncoder(), catalog, "provisions_v1") == 3
    ids = {p.id for p in client.scroll("provisions_v1", limit=10)[0]}
    assert point_id("8 CFR 214.2(j)(1)", 2, 0) not in ids


def test_reloading_updates_in_place(client: QdrantClient, catalog: Path) -> None:
    load(client, FakeEncoder(), catalog, "provisions_v1")
    load(client, FakeEncoder(), catalog, "provisions_v1")
    assert client.count("provisions_v1").count == 3


def test_heading_path_is_embedded_but_kept_separate(
    client: QdrantClient, catalog: Path
) -> None:
    enc = FakeEncoder()
    load(client, enc, catalog, "provisions_v1")
    assert "Part 214 > F-1 > Practical training. Current OPT text." in enc.texts
    point = client.retrieve("provisions_v1", [point_id(OPT, 2, 0)])[0]
    assert point.payload is not None
    assert point.payload["text"] == "Current OPT text."
    assert point.payload["visa_types"] == ["F-1", "F-2"]
    assert point.payload["embed_model"] == "fake-encoder"
    assert point.payload["valid_from"] == "2024-01-01T00:00:00Z"


def test_in_force_filter_skips_superseded_versions(
    client: QdrantClient, catalog: Path
) -> None:
    load(client, FakeEncoder(), catalog, "provisions_v1")
    in_force = models.Filter(
        must=[models.IsNullCondition(is_null=models.PayloadField(key="valid_to"))]
    )
    points, _ = client.scroll("provisions_v1", scroll_filter=in_force, limit=10)
    assert {(p.payload or {}).get("version") for p in points} == {1, 2}
    assert all((p.payload or {}).get("status") == "in_force" for p in points)


def test_dates_become_utc_rfc3339() -> None:
    assert to_rfc3339(None) is None
    assert to_rfc3339("2024-01-01") == "2024-01-01T00:00:00Z"
    assert to_rfc3339("2024-01-01T05:00:00+05:00") == "2024-01-01T00:00:00Z"


@pytest.fixture
def empty() -> QdrantClient:
    return QdrantClient(location=":memory:")


def _build(
    client: QdrantClient,
    catalog: Path,
    version: int | None = None,
    replace: bool = False,
    activate: bool = False,
) -> str:
    with warnings.catch_warnings():  # payload indexes are a no-op locally
        warnings.simplefilter("ignore", UserWarning)
        return build(
            client, FakeEncoder(), catalog, "provisions", version, replace, activate
        )


def test_build_picks_the_next_version_and_activates(
    empty: QdrantClient, catalog: Path
) -> None:
    assert _build(empty, catalog, activate=True) == "provisions_v1"
    assert _build(empty, catalog, activate=True) == "provisions_v2"
    assert versions(empty, "provisions") == [1, 2]
    assert current_target(empty, "provisions") == "provisions_v2"
    assert dense_size(empty, "provisions_v2") == DIM
    assert status(empty, "provisions")[0] == "alias provisions -> provisions_v2"


def test_build_never_touches_the_live_collection(
    empty: QdrantClient, catalog: Path
) -> None:
    _build(empty, catalog, activate=True)
    with pytest.raises(ValueError, match="live"):
        _build(empty, catalog, version=1, replace=True)
    assert empty.count("provisions_v1").count == 3


def test_partial_build_needs_replace(empty: QdrantClient, catalog: Path) -> None:
    _build(empty, catalog)  # v1, not activated, stands in for a crashed load
    with pytest.raises(ValueError, match="replace"):
        _build(empty, catalog, version=1)
    assert _build(empty, catalog, version=1, replace=True) == "provisions_v1"
    assert empty.count("provisions_v1").count == 3
