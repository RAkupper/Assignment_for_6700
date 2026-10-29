"""The prototype page and assets must ship with the same API package."""

from fastapi.testclient import TestClient

from myapp.app import app


def test_web_page_and_local_assets_are_served() -> None:
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert '<html lang="en">' in page.text
    assert 'id="question-form"' in page.text
    for asset in ["/assets/app.css", "/assets/app.js"]:
        assert client.get(asset).status_code == 200
    assert client.get("/assets/../../.env").status_code == 404
