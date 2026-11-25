"""The prototype page and assets must ship with the same API package."""

from fastapi.testclient import TestClient

from myapp.app import VERSION, app


def test_web_page_and_local_assets_are_served() -> None:
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert '<html lang="en">' in page.text
    assert 'id="question-form"' in page.text
    for asset in ["/assets/app.css", "/assets/app.js"]:
        assert client.get(asset).status_code == 200
    assert client.get("/assets/../../.env").status_code == 404


def test_page_is_revalidated_and_its_assets_are_cached_per_release() -> None:
    client = TestClient(app)
    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    assert f"/assets/app.js?v={VERSION}" in page.text
    pinned = client.get(f"/assets/app.js?v={VERSION}")
    assert "immutable" in pinned.headers["cache-control"]
    assert client.get("/assets/app.js").headers["cache-control"] == "no-cache"


def test_answers_are_never_cached() -> None:
    response = TestClient(app).post("/ask", json={"question": ""})
    assert response.headers["cache-control"] == "no-store"
