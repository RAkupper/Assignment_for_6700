import warnings
from datetime import date

import pytest
from qdrant_client import QdrantClient

from myapp.vectordb.loader import build
from myapp.vectordb.search import Hit, Searcher, SearchSettings
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
    return Searcher(client, FakeEncoder(), "provisions")


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
        "text",
        "source",
        "authority",
        "status",
        "visa_types",
        "valid_from",
        "valid_to",
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
