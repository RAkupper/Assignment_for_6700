"""Hybrid search over the live provisions collection.

From the command line:
    python -m myapp.vectordb.search "Can I work off campus?" --visa F-1

Embedding is CPU work and the Qdrant client blocks, so call `Searcher.search`
from a plain `def` endpoint or through `asyncio.to_thread`, never bare inside
`async def`.
"""

import argparse
import math
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from qdrant_client import QdrantClient, models

from myapp.vectordb.collection import DENSE, SPARSE, QdrantSettings, get_client
from myapp.vectordb.encoders import EmbeddingSettings, Encoded, Encoder


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        str_strip_whitespace=True,
        extra="ignore",
    )
    k: int = Field(default=3, gt=0)
    candidate_k: int = Field(default=50, gt=0)  # per branch, before fusion
    min_relevance: float = Field(default=0.65, ge=-1.0, le=1.0)  # tuned, see README


class QueryEncoder(Protocol):
    def embed_query(self, text: str) -> Encoded: ...


class Hit(BaseModel):
    """One retrieved provision. `model_dump()` gives the baseline's dict shape.

    `status` is the version's status today. For an `as_of` search, a superseded
    hit was the rule on that date: present it as the rule then, not as outdated.
    """

    model_config = ConfigDict(frozen=True)

    provision_id: str  # the exact citation
    score: float  # RRF rank score: compare within one result list only
    relevance: float  # dense cosine to the question: comparable across questions
    heading_path: str
    parent_text: str  # the parent's lead-in, e.g. "...is admitted if:"
    text: str
    source: str
    authority: str
    status: str
    visa_types: list[str]
    valid_from: str | None
    valid_to: str | None
    url: str | None = None  # official text, as it read on the search's as_of date


def build_filter(
    as_of: date | None = None,
    visa_type: str | None = None,
    sources: list[str] | None = None,
    include_proposed: bool = False,
) -> models.Filter:
    """In force on `as_of` (today by default), plus the optional narrowing filters."""
    day = as_of or datetime.now(UTC).date()
    when = datetime(day.year, day.month, day.day, tzinfo=UTC)
    from_null = models.IsNullCondition(is_null=models.PayloadField(key="valid_from"))
    to_null = models.IsNullCondition(is_null=models.PayloadField(key="valid_to"))
    started = models.FieldCondition(
        key="valid_from", range=models.DatetimeRange(lte=when)
    )
    not_ended = models.FieldCondition(
        key="valid_to", range=models.DatetimeRange(gt=when)
    )
    must: list[Any] = [
        models.Filter(should=[from_null, started]),
        models.Filter(should=[to_null, not_ended]),
    ]
    if visa_type:
        tagged = models.FieldCondition(
            key="visa_types", match=models.MatchAny(any=[visa_type])
        )
        untagged = models.IsEmptyCondition(
            is_empty=models.PayloadField(key="visa_types")
        )  # empty means the provision applies to every visa type
        must.append(models.Filter(should=[tagged, untagged]))
    if sources:
        must.append(
            models.FieldCondition(key="source", match=models.MatchAny(any=sources))
        )
    excluded = ["withdrawn"] if include_proposed else ["withdrawn", "proposed"]
    dropped = models.FieldCondition(key="status", match=models.MatchAny(any=excluded))
    return models.Filter(must=must, must_not=dropped)


class Searcher:
    """Holds the client and loaded models; create once per process."""

    def __init__(
        self,
        client: QdrantClient,
        encoder: QueryEncoder,
        collection: str,
        settings: SearchSettings | None = None,
    ) -> None:
        self.client = client
        self.encoder = encoder
        self.collection = collection  # the alias, so a rollback needs no restart
        self.settings = settings or SearchSettings()

    @classmethod
    def from_settings(cls) -> "Searcher":
        settings = QdrantSettings()
        return cls(
            get_client(settings),
            Encoder(EmbeddingSettings()),
            settings.alias,
            SearchSettings(),
        )

    def search(
        self,
        query: str,
        k: int | None = None,
        as_of: date | None = None,
        visa_type: str | None = None,
        sources: list[str] | None = None,
        include_proposed: bool = False,
        min_relevance: float | None = None,
    ) -> list[Hit]:
        """Top `k` provisions for `query`, dense and BM25 fused with RRF.

        Hits below `min_relevance` are dropped, so an empty list means nothing in
        the rules is close enough to answer from.
        """
        if not query.strip():
            return []
        k = k or self.settings.k
        cutoff = self.settings.min_relevance if min_relevance is None else min_relevance
        limit = self.settings.candidate_k
        q = self.encoder.embed_query(query)
        where = build_filter(as_of, visa_type, sources, include_proposed)
        result = self.client.query_points(
            self.collection,
            prefetch=[
                models.Prefetch(query=q.dense, using=DENSE, filter=where, limit=limit),
                models.Prefetch(
                    query=q.sparse, using=SPARSE, filter=where, limit=limit
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            query_filter=where,
            limit=k,
            with_payload=True,
            with_vectors=[DENSE],
        )
        hits = [_hit(p, _cosine(q.dense, _dense_of(p)), as_of) for p in result.points]
        return [h for h in hits if h.relevance >= cutoff]


def _dense_of(point: models.ScoredPoint) -> list[float]:
    vectors = point.vector
    assert isinstance(vectors, dict)  # named vectors, requested above
    return cast(list[float], vectors[DENSE])  # dense is a flat list of floats


def _cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


# "8 CFR 214.2(f)(9)(i)" and definitions like '8 CFR 214.1(a)(2) "term"'
CFR_CITATION = re.compile(r"^(\d+) CFR (\d+[a-z]?\.\d+[a-z]?)((?:\([^()\s]+\))*)")


def source_url(provision_id: str, source: str, as_of: date | None = None) -> str | None:
    """eCFR link to the paragraph; a dated link when the question is historical."""
    match = CFR_CITATION.match(provision_id) if source == "ecfr" else None
    if match is None:
        return None
    title, section, paragraphs = match.groups()
    when = f"on/{as_of.isoformat()}" if as_of else "current"
    anchor = f"#p-{section}{paragraphs}" if paragraphs else ""
    return f"https://www.ecfr.gov/{when}/title-{title}/section-{section}{anchor}"


def _hit(point: models.ScoredPoint, relevance: float, as_of: date | None = None) -> Hit:
    p = point.payload or {}
    return Hit(
        provision_id=p["provision_id"],
        score=point.score,
        relevance=round(relevance, 4),
        heading_path=p.get("heading_path", ""),
        parent_text=p.get("parent_text", ""),
        text=p["text"],
        source=p["source"],
        authority=p["authority"],
        status=p["status"],
        visa_types=p.get("visa_types", []),
        valid_from=p.get("valid_from"),
        valid_to=p.get("valid_to"),
        url=source_url(p["provision_id"], p["source"], as_of),
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Search the live collection.")
    parser.add_argument("query")
    parser.add_argument("-k", type=int)
    parser.add_argument("--visa")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--source", action="append")
    parser.add_argument("--include-proposed", action="store_true")
    parser.add_argument("--min-relevance", type=float)
    args = parser.parse_args(argv)
    hits = Searcher.from_settings().search(
        args.query,
        k=args.k,
        as_of=args.as_of,
        visa_type=args.visa,
        sources=args.source,
        include_proposed=args.include_proposed,
        min_relevance=args.min_relevance,
    )
    for h in hits:
        print(
            f"rel {h.relevance:.3f}  rrf {h.score:.3f}  {h.provision_id}  "
            f"[{h.authority}, {h.status}]"
        )
        print(f"       {h.text[:150]}")
    if not hits:
        print("nothing relevant enough: ask your DSO")


if __name__ == "__main__":
    main()
