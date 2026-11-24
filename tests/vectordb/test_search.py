import warnings
from collections.abc import Sequence
from datetime import date

import pytest
from qdrant_client import QdrantClient

from myapp.vectordb.loader import build
from myapp.vectordb.search import Hit, Searcher, SearchSettings, source_url
from tests.vectordb.conftest import FakeEncoder, make_catalog

CAMPUS = "8 CFR 214.2(f)(9)(i)"
STEM = "8 CFR 214.2(f)(10)(ii)(C)(7)"
J1 = "8 CFR 214.2(j)(1)(iv)"
GENERAL = "8 CFR 214.1(a)(3)"
FEE = "FR 2026-01234 sec. II"


@pytest.fixture(scope="module")
def searcher(tmp_path_factory: pytest.TempPathFactory) -> Searcher:
    catalog = make_catalog(
        tmp_path_factory.mktemp("cat") / "catalog.sqlite",
        provisions=[
            (CAMPUS, "ecfr", "F-1 > Employment", "regulation", '["F-1"]'),
            (STEM, "ecfr", "F-1 > STEM OPT", "regulation", '["F-1"]'),
            (J1, "ecfr", "J-1 > Employment", "regulation", '["J-1"]'),
            (GENERAL, "ecfr", "General", "regulation", "[]"),
            (FEE, "federal_register", "Proposed fees", "rulemaking", '["F-1"]'),
        ],
        versions=[
            (
                CAMPUS,
                1,
                "On-campus employment is limited to 20 hours a week.",
                "superseded",
                "2020-01-01",
                "2025-01-01",
            ),
            (
                CAMPUS,
                2,
                "On-campus employment is limited to 24 hours a week.",
                "in_force",
                "2025-01-01",
                None,
            ),
            (
                STEM,
                1,
                "The student and employer must complete Form I-983.",
                "in_force",
                "2020-01-01",
                None,
            ),
            (
                J1,
                1,
                "Exchange visitor employment requires sponsor approval.",
                "in_force",
                "2020-01-01",
                None,
            ),
            (
                GENERAL,
                1,
                "Nonimmigrants must maintain status and employment rules.",
                "in_force",
                "2020-01-01",
                None,
            ),
            (
                FEE,
                1,
                "Proposed employment authorization fee increase.",
                "proposed",
                None,
                None,
            ),
        ],
    )
    client = QdrantClient(location=":memory:")
    with warnings.catch_warnings():  # payload indexes are a no-op locally
        warnings.simplefilter("ignore", UserWarning)
        build(client, FakeEncoder(), catalog, "provisions", activate=True)
    return Searcher(  # fake vectors, so the tuned cutoff does not apply
        client, FakeEncoder(), "provisions", SearchSettings(min_relevance=0.0)
    )


def ids(hits: list[Hit]) -> list[str]:
    return [h.provision_id for h in hits]


def test_exact_term_ranks_first(searcher: Searcher) -> None:
    assert ids(searcher.search("Form I-983"))[0] == STEM


def test_returns_k_hits_in_the_agreed_shape(searcher: Searcher) -> None:
    hits = searcher.search("employment", k=2)
    assert len(hits) == 2
    assert set(hits[0].model_dump()) == {
        "provision_id",
        "score",
        "relevance",
        "heading_path",
        "parent_text",
        "text",
        "source",
        "authority",
        "status",
        "visa_types",
        "valid_from",
        "valid_to",
        "url",
    }


def test_defaults_to_rules_in_force_today(searcher: Searcher) -> None:
    hits = [
        h
        for h in searcher.search("on-campus employment hours", k=10)
        if h.provision_id == CAMPUS
    ]
    assert [h.text for h in hits] == [
        "On-campus employment is limited to 24 hours a week."
    ]


def test_as_of_returns_the_version_in_force_then(searcher: Searcher) -> None:
    hits = searcher.search("on-campus employment hours", k=10, as_of=date(2023, 6, 1))
    campus = [h for h in hits if h.provision_id == CAMPUS]
    assert [h.text for h in campus] == [
        "On-campus employment is limited to 20 hours a week."
    ]


def test_visa_filter_keeps_rules_for_everyone(searcher: Searcher) -> None:
    found = set(ids(searcher.search("employment", k=10, visa_type="J-1")))
    assert found == {J1, GENERAL}


def test_proposed_rules_only_when_asked(searcher: Searcher) -> None:
    assert FEE not in ids(searcher.search("employment fee", k=10))
    assert FEE in ids(searcher.search("employment fee", k=10, include_proposed=True))


def test_source_filter(searcher: Searcher) -> None:
    hits = searcher.search(
        "fee", k=10, sources=["federal_register"], include_proposed=True
    )
    assert ids(hits) == [FEE]


def test_blank_query_returns_nothing(searcher: Searcher) -> None:
    assert searcher.search("   ") == []


def test_relevance_is_a_cosine(searcher: Searcher) -> None:
    hits = searcher.search("Form I-983", k=10)
    assert all(-1.0 <= h.relevance <= 1.0 for h in hits)
    assert hits[0].relevance == max(h.relevance for h in hits)


def test_cutoff_drops_weak_hits(searcher: Searcher) -> None:
    hits = searcher.search("Form I-983", k=10)
    best = hits[0].relevance
    kept = searcher.search("Form I-983", k=10, min_relevance=best)
    assert ids(kept) == [STEM]


def test_nothing_relevant_returns_empty(searcher: Searcher) -> None:
    assert searcher.search("Form I-983", min_relevance=1.01) == []


def test_settings_set_the_defaults(searcher: Searcher) -> None:
    strict = Searcher(
        searcher.client,
        searcher.encoder,
        "provisions",
        SearchSettings(k=1, min_relevance=1.0),
    )
    assert strict.search("employment") == []
    assert len(strict.search("employment", min_relevance=0.0)) == 1


def test_ecfr_hits_link_to_their_paragraph(searcher: Searcher) -> None:
    found = searcher.search("employment fees", k=10, include_proposed=True)
    hits = {h.provision_id: h for h in found}
    assert hits[CAMPUS].url == (
        "https://www.ecfr.gov/current/title-8/section-214.2#p-214.2(f)(9)(i)"
    )
    assert hits[FEE].url is None


def test_historical_search_links_to_the_text_on_that_date(searcher: Searcher) -> None:
    hits = searcher.search("on-campus employment hours", k=10, as_of=date(2023, 6, 1))
    campus = next(h for h in hits if h.provision_id == CAMPUS)
    assert campus.url == (
        "https://www.ecfr.gov/on/2023-06-01/title-8/section-214.2#p-214.2(f)(9)(i)"
    )


@pytest.mark.parametrize(
    ("provision_id", "url"),
    [
        ("8 CFR 214.3", "https://www.ecfr.gov/current/title-8/section-214.3"),
        (
            '8 CFR 214.1(a)(2) "Nonimmigrant"',
            "https://www.ecfr.gov/current/title-8/section-214.1#p-214.1(a)(2)",
        ),
        (
            "22 CFR 62.23(g)",
            "https://www.ecfr.gov/current/title-22/section-62.23#p-62.23(g)",
        ),
        (
            "8 CFR 274a.12(c)(3)",
            "https://www.ecfr.gov/current/title-8/section-274a.12#p-274a.12(c)(3)",
        ),
        ("not a citation", None),
    ],
)
def test_source_url_forms(provision_id: str, url: str | None) -> None:
    assert source_url(provision_id, "ecfr") == url


class FakeReranker:
    """Scores a passage by whether it contains a word; records what it was asked."""

    def __init__(self, word: str) -> None:
        self.word = word
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, documents: Sequence[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        return [5.0 if self.word in d else -5.0 for d in documents]


def reranked(
    searcher: Searcher,
    reranker: FakeReranker,
    min_rerank: float = -1.0,
    rerank_candidates: int = 20,
) -> Searcher:
    settings = SearchSettings(
        min_relevance=0.9,  # ignored once reranking
        min_rerank=min_rerank,
        rerank_candidates=rerank_candidates,
    )
    return Searcher(searcher.client, searcher.encoder, "provisions", settings, reranker)


def test_reranker_decides_the_order(searcher: Searcher) -> None:
    hits = reranked(searcher, FakeReranker("Exchange")).search("employment", k=1)
    assert ids(hits) == [J1]
    assert hits[0].score == 5.0  # the reranker's score replaces RRF's


def test_reranker_cutoff_drops_weak_hits(searcher: Searcher) -> None:
    hits = reranked(searcher, FakeReranker("Exchange")).search("employment", k=10)
    assert ids(hits) == [J1]  # every other passage scored -5, below the -1 default


def test_reranker_ignores_the_dense_cutoff(searcher: Searcher) -> None:
    lenient = reranked(searcher, FakeReranker("Form I-983"), min_rerank=-10.0)
    assert len(lenient.search("employment", k=10)) > 1


def test_reranker_rereads_more_candidates_than_it_returns(searcher: Searcher) -> None:
    fake = FakeReranker("Exchange")
    reranked(searcher, fake, rerank_candidates=4).search("employment", k=1)
    _, documents = fake.calls[0]
    assert len(documents) == 4


def test_reranker_reads_the_lead_in_with_the_text(searcher: Searcher) -> None:
    fake = FakeReranker("Exchange")
    reranked(searcher, fake).search("Form I-983", k=1)
    query, documents = fake.calls[0]
    assert query == "Form I-983"
    assert "The student and employer must complete Form I-983." in documents


def test_nothing_above_the_rerank_cutoff_returns_empty(searcher: Searcher) -> None:
    assert reranked(searcher, FakeReranker("no such word")).search("employment") == []
