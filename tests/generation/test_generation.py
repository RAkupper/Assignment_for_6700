"""API integration with the actual team loader/searcher and in-memory Qdrant.

Only model inference is fake; there are no model downloads or external services.
"""

import json
import threading
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException

from myapp import app as app_module
from myapp.app import app, get_generation_service, get_model_client
from myapp.generation.schemas import QuestionRequest
from myapp.generation.service import GenerationService, GenerationSettings
from myapp.model_client import EchoModelClient, ModelTimeoutError
from myapp.vectordb.loader import build, point_id
from myapp.vectordb.search import Searcher, SearchSettings
from tests.fakes import FakeModelClient
from tests.vectordb.conftest import FakeEncoder, make_catalog


@pytest.fixture
def generation(tmp_path: Path) -> Iterator[GenerationService]:
    catalog = make_catalog(
        tmp_path / "catalog.sqlite",
        provisions=[
            ("TEST-1", "ecfr", "Fixture > Student employment", "regulation", '["F-1"]'),
            ("TEST-2", "sevp", "Fixture > Guidance", "guidance", '["J-1"]'),
            ("TEST-P", "federal_register", "Fixture proposal", "rulemaking", '["F-1"]'),
        ],
        versions=[
            (
                "TEST-1",
                1,
                "Historical fixture: student employment Form I-983.",
                "superseded",
                "2020-01-01",
                "2024-01-01",
            ),
            (
                "TEST-1",
                2,
                "Current fixture: student employment Form I-983.",
                "in_force",
                "2024-01-01",
                None,
            ),
            (
                "TEST-2",
                1,
                "Fixture guidance about exchange visitor employment.",
                "in_force",
                "2020-01-01",
                None,
            ),
            (
                "TEST-P",
                1,
                "Fixture proposed employment changes.",
                "proposed",
                None,
                None,
            ),
        ],
    )
    client = QdrantClient(location=":memory:")
    encoder = FakeEncoder()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        build(client, encoder, catalog, "provisions", activate=True)
    # The production cutoff is calibrated for BGE, not this bag-of-words fake.
    searcher = Searcher(
        client,
        encoder,
        "provisions",
        SearchSettings(_env_file=None, min_relevance=0.0),
    )
    service = GenerationService(searcher, GenerationSettings(_env_file=None))
    yield service
    service.close()


@pytest.fixture
def api(
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, FakeModelClient]]:
    # Startup stays offline; requests use the team's real Searcher from the fixture.
    monkeypatch.setattr(
        app_module,
        "generation_settings",
        GenerationSettings(
            _env_file=None,
            enabled=False,
        ),
    )
    model = FakeModelClient(answer="Fixture answer [1].")
    app.dependency_overrides[get_generation_service] = lambda: generation
    app.dependency_overrides[get_model_client] = lambda: model
    try:
        with TestClient(app) as client:
            yield client, model
    finally:
        app.dependency_overrides.clear()


def test_ask_retrieves_and_sends_evidence_to_existing_llm(
    api: tuple[TestClient, FakeModelClient],
) -> None:
    client, model = api
    response = client.post("/ask", json={"question": "Form I-983", "k": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Fixture answer [1]."
    assert body["sources"][0]["provision_id"] == "TEST-1"
    assert "relevance" in body["sources"][0]
    assert "is_demo" not in body
    evidence = json.loads(model.prompts[0].split("Evidence JSON: ", 1)[1])
    assert evidence[0]["citation"] == "[1]"
    assert evidence[0]["text"] == body["sources"][0]["text"]
    assert "Current fixture" in evidence[0]["text"]


def test_uncited_answer_is_returned_without_retry_or_rejection(
    generation: GenerationService,
) -> None:
    model = FakeModelClient(answer="A prototype response without inline references.")
    result = generation.ask(QuestionRequest(question="Form I-983", k=1), model)
    assert result.answer == model.answer
    assert result.status == "answered"
    assert result.sources
    assert len(model.prompts) == 1


def test_parent_context_reaches_generation_prompt(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
) -> None:
    lead = "Parent fixture: these conditions apply only to enrolled students."
    generation.searcher.client.set_payload(
        "provisions_v1",
        {"parent_text": lead},
        points=[point_id("TEST-1", 2, 0)],
        wait=True,
    )
    client, model = api
    response = client.post("/ask", json={"question": "Form I-983", "k": 1})
    assert response.status_code == 200
    evidence = json.loads(model.prompts[0].split("Evidence JSON: ", 1)[1])
    assert evidence[0]["parent_text"] == lead
    assert response.json()["sources"][0]["parent_text"] == lead


def test_retrieve_and_ask_share_filters_and_historical_results(
    api: tuple[TestClient, FakeModelClient],
) -> None:
    client, model = api
    request = {
        "question": "student employment",
        "as_of": "2023-06-01",
        "visa_type": "F-1",
        "sources": ["ecfr"],
    }
    retrieved = client.post("/retrieve", json=request)
    assert retrieved.status_code == 200
    assert model.prompts == []
    assert len(retrieved.json()) == 1
    assert "Historical fixture" in retrieved.json()[0]["text"]
    response = client.post("/ask", json=request)
    assert response.json()["sources"] == retrieved.json()
    assert "Requested date: 2023-06-01" in model.prompts[0]
    assert "status_today" in model.prompts[0]


def test_proposals_are_opt_in_and_labeled_in_prompt(
    api: tuple[TestClient, FakeModelClient],
) -> None:
    client, model = api
    request = {"question": "employment", "sources": ["federal_register"]}
    assert client.post("/ask", json=request).json()["sources"] == []
    assert model.prompts == []
    body = client.post("/ask", json={**request, "include_proposed": True}).json()
    assert body["sources"][0]["status"] == "proposed"
    assert "Label proposed rules as proposals" in model.prompts[0]


def test_empty_search_does_not_call_model(
    api: tuple[TestClient, FakeModelClient],
) -> None:
    client, model = api
    body = client.post(
        "/ask",
        json={
            "question": "employment",
            "sources": ["uscis_policy_manual"],
        },
    ).json()
    assert body["sources"] == []
    assert "Insufficient evidence" in body["answer"]
    assert "DSO" in body["answer"]
    assert model.prompts == []


def test_excerpt_budget_matches_sources_returned_to_user(
    generation: GenerationService,
) -> None:
    generation.settings.max_context_chars = 20
    model = FakeModelClient()
    result = generation.ask(QuestionRequest(question="Form I-983", k=1), model)
    evidence = json.loads(model.prompts[0].split("Evidence JSON: ", 1)[1])
    assert len(result.sources[0].text) == 20
    assert evidence[0]["excerpt_truncated"] is True
    assert evidence[0]["text"] == result.sources[0].text


@pytest.mark.parametrize(
    "body",
    [
        {"question": " "},
        {"question": "test", "k": 0},
        {"question": "test", "visa_type": "invalid"},
        {"question": "test", "as_of": "not-a-date"},
    ],
)
def test_bad_questions_are_rejected(
    api: tuple[TestClient, FakeModelClient],
    body: dict[str, object],
) -> None:
    client, model = api
    assert client.post("/ask", json=body).status_code == 422
    assert model.prompts == []


def test_disabled_generation_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_module,
        "generation_settings",
        GenerationSettings(
            _env_file=None,
            enabled=False,
        ),
    )
    with TestClient(app) as client:
        assert client.post("/ask", json={"question": "test"}).status_code == 503
        assert client.get("/health").status_code == 200


def test_qdrant_failure_becomes_503(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> list[object]:
        raise ResponseHandlingException(ConnectionError("offline"))

    monkeypatch.setattr(generation.searcher, "search", fail)
    client, model = api
    response = client.post("/ask", json={"question": "test"})
    assert response.status_code == 503
    assert "Retrieval unavailable" in response.json()["detail"]
    assert model.prompts == []


def test_llm_timeout_keeps_existing_error_mapping(
    api: tuple[TestClient, FakeModelClient],
) -> None:
    client, model = api
    model.error = ModelTimeoutError("timed out")
    assert client.post("/ask", json={"question": "Form I-983"}).status_code == 504


def test_searcher_is_created_once_and_closed(
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[GenerationSettings] = []
    closed: list[bool] = []

    def create(settings: GenerationSettings) -> GenerationService:
        built.append(settings)
        return generation

    monkeypatch.setattr(GenerationService, "from_settings", staticmethod(create))
    monkeypatch.setattr(generation, "close", lambda: closed.append(True))
    monkeypatch.setattr(
        app_module,
        "generation_settings",
        GenerationSettings(
            _env_file=None,
            enabled=True,
        ),
    )
    with TestClient(app) as client:
        for _ in range(2):
            assert (
                client.post(
                    "/retrieve",
                    json={"question": "Form I-983"},
                ).status_code
                == 200
            )
        assert app.state.generation is generation
        assert closed == []
    assert len(built) == 1
    assert closed == [True]
    assert app.state.generation is None


def test_health_stays_available_during_generation(
    api: tuple[TestClient, FakeModelClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = api
    started, release = threading.Event(), threading.Event()

    def slow_generate(prompt: str) -> str:
        started.set()
        assert release.wait(timeout=5)
        return "Fixture answer [1]."

    monkeypatch.setattr(model, "generate", slow_generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answer = pool.submit(client.post, "/ask", json={"question": "Form I-983"})
        try:
            assert started.wait(timeout=5)
            health = pool.submit(client.get, "/health")
            assert health.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert answer.result(timeout=5).status_code == 200


@pytest.fixture
def ready_api(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, FakeModelClient]:
    # The normal API fixture skips startup model loading; install its real searcher
    # in app state so /ready checks the same state that production startup creates.
    monkeypatch.setattr(app_module.generation_settings, "enabled", True)
    app.state.generation = generation
    return api


def test_ready_accepts_loaded_index_without_running_inference(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_inference(*args: object, **kwargs: object) -> None:
        raise AssertionError("readiness must not run embeddings")

    monkeypatch.setattr(
        generation.searcher.encoder, "embed_query", unexpected_inference
    )
    client, model = ready_api
    assert client.get("/ready").json() == {"status": "ready"}
    assert model.pings == 1
    assert model.prompts == []


def test_ready_rejects_unactivated_index(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
) -> None:
    generation.searcher.client.update_collection_aliases(
        [
            models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name="provisions")
            )
        ]
    )
    client, _ = ready_api
    response = client.get("/ready")
    assert response.status_code == 503
    assert "not activated" in response.json()["detail"]
    assert client.get("/health").status_code == 200


def test_ready_rejects_empty_index(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
) -> None:
    generation.searcher.client.delete(
        "provisions_v1",
        models.FilterSelector(filter=models.Filter()),
        wait=True,
    )
    client, _ = ready_api
    response = client.get("/ready")
    assert response.status_code == 503
    assert "empty" in response.json()["detail"]


@pytest.mark.parametrize("fault", ["dimensions", "sparse", "distance", "red"])
def test_ready_rejects_incompatible_or_failed_collection(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    client = generation.searcher.client
    info = client.get_collection("provisions_v1").model_copy(deep=True)
    vectors = info.config.params.vectors
    assert isinstance(vectors, dict)
    if fault == "dimensions":
        vectors["dense"].size += 1
    elif fault == "distance":
        vectors["dense"].distance = models.Distance.DOT
    elif fault == "sparse":
        info.config.params.sparse_vectors = None
    else:
        info.status = models.CollectionStatus.RED
    monkeypatch.setattr(client, "get_collection", lambda *args, **kwargs: info)
    api_client, _ = ready_api
    assert api_client.get("/ready").status_code == 503


@pytest.mark.parametrize("payload", [{"embed_model": "other-model"}, {"text": None}])
def test_ready_rejects_wrong_model_or_missing_evidence(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    payload: dict[str, object],
) -> None:
    client = generation.searcher.client
    first = client.scroll("provisions_v1", limit=1)[0][0]
    client.set_payload("provisions_v1", payload, points=[first.id], wait=True)
    api_client, _ = ready_api
    assert api_client.get("/ready").status_code == 503


def test_ready_reports_qdrant_connection_failure(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def offline(*args: object, **kwargs: object) -> None:
        raise ResponseHandlingException(ConnectionError("offline"))

    monkeypatch.setattr(generation.searcher.client, "get_aliases", offline)
    client, model = ready_api
    response = client.get("/ready")
    assert response.status_code == 503
    assert "Qdrant readiness check failed" in response.json()["detail"]
    assert client.get("/health").status_code == 200
    assert model.prompts == []


def test_ready_rejects_missing_generation_state(
    ready_api: tuple[TestClient, FakeModelClient],
) -> None:
    app.state.generation = None
    client, _ = ready_api
    assert client.get("/ready").status_code == 503


def test_ready_rejects_uninitialized_encoder(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation.searcher.encoder, "dim", 0)
    client, _ = ready_api
    response = client.get("/ready")
    assert response.status_code == 503
    assert "encoder is not initialized" in response.json()["detail"]


def test_ready_rejects_echo_for_enabled_generation(
    ready_api: tuple[TestClient, FakeModelClient],
) -> None:
    app.dependency_overrides[get_model_client] = EchoModelClient
    client, _ = ready_api
    response = client.get("/ready")
    assert response.status_code == 503
    assert "real model backend" in response.json()["detail"]


def test_health_stays_available_during_readiness_probe(
    ready_api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = ready_api
    started, release = threading.Event(), threading.Event()

    def slow_check() -> None:
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(generation, "check_ready", slow_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ready = pool.submit(client.get, "/ready")
        try:
            assert started.wait(timeout=5)
            health = pool.submit(client.get, "/health")
            assert health.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert ready.result(timeout=5).status_code == 200


def _held_api(
    generation: GenerationService,
    model: FakeModelClient,
    monkeypatch: pytest.MonkeyPatch,
    max_concurrent: int,
    queue_timeout_s: float,
) -> tuple[threading.Event, threading.Event]:
    started, release = threading.Event(), threading.Event()
    generation.settings = GenerationSettings(
        _env_file=None, max_concurrent=max_concurrent, queue_timeout_s=queue_timeout_s
    )
    generation._slots = threading.BoundedSemaphore(max_concurrent)

    def held_generate(prompt: str) -> str:
        started.set()
        assert release.wait(timeout=5)
        return "Fixture answer [1]."

    monkeypatch.setattr(model, "generate", held_generate)
    return started, release


def test_busy_slots_return_503_without_calling_the_model(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = api
    started, release = _held_api(generation, model, monkeypatch, 1, 0.05)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/ask", json={"question": "Form I-983"})
        try:
            assert started.wait(timeout=5)
            busy = client.post("/ask", json={"question": "Form I-983"})
            assert busy.status_code == 503
            assert "busy" in busy.json()["detail"]
            assert client.post("/retrieve", json={"question": "Form I-983"}).is_success
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200


def test_waiting_request_gets_a_freed_slot(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = api
    started, release = _held_api(generation, model, monkeypatch, 1, 5.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/ask", json={"question": "Form I-983"})
        assert started.wait(timeout=5)
        second = pool.submit(client.post, "/ask", json={"question": "Form I-983"})
        release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200


def test_failed_generation_frees_its_slot(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
) -> None:
    client, model = api
    generation.settings = GenerationSettings(
        _env_file=None, max_concurrent=1, queue_timeout_s=0.05
    )
    generation._slots = threading.BoundedSemaphore(1)
    model.error = ModelTimeoutError("timed out")
    assert client.post("/ask", json={"question": "Form I-983"}).status_code == 504
    model.error = None
    assert client.post("/ask", json={"question": "Form I-983"}).status_code == 200


def test_not_covered_answer_does_not_wait_for_a_busy_slot(
    api: tuple[TestClient, FakeModelClient],
    generation: GenerationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, model = api
    started, release = _held_api(generation, model, monkeypatch, 1, 5.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, "/ask", json={"question": "Form I-983"})
        try:
            assert started.wait(timeout=5)
            empty = client.post(
                "/ask",
                json={"question": "employment", "sources": ["uscis_policy_manual"]},
            )
            assert empty.status_code == 200
            assert empty.json()["status"] == "insufficient_evidence"
            assert first.running()
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
