"""Probes and listener guards; no database: the pool is a stub."""

import pytest
from conftest import DownPool, asgi_client

from agents_history.api.app import PREFIX, create_app, create_ingest_app, create_service_app


@pytest.fixture
def ui(settings):
    return create_app(settings, DownPool())


async def test_healthz_ok_without_db(ui):
    async with asgi_client(ui) as http:
        response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_503_when_db_down(ui):
    async with asgi_client(ui) as http:
        response = await http.get("/readyz")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_ready"


async def test_ui_app_refuses_foreign_peer(ui):
    async with asgi_client(ui, peer="10.0.0.5") as http:
        refused = await http.get(f"{PREFIX}/x")
        health = await http.get("/healthz")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert health.status_code == 200


async def test_ui_app_serves_allowed_peer(ui):
    async with asgi_client(ui) as http:
        response = await http.get(f"{PREFIX}/x")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_search_timeout_is_kept_in_milliseconds(settings_for):
    app = create_app(settings_for("postgresql://u:p@db.invalid/history"), DownPool())
    assert app.state.search_timeout_ms == 8000
    slow = settings_for("postgresql://u:p@db.invalid/history", search_timeout_seconds=2.5)
    assert create_app(slow, DownPool()).state.search_timeout_ms == 2500


@pytest.mark.parametrize("factory", [create_service_app, create_ingest_app])
async def test_service_and_ingest_apps_need_token(settings, factory):
    app = factory(settings, DownPool(), ["t"])
    async with asgi_client(app, peer="10.9.9.9") as http:
        anonymous = await http.get(f"{PREFIX}/x")
        wrong = await http.get(f"{PREFIX}/x", headers={"Authorization": "Bearer nope"})
        known = await http.get(f"{PREFIX}/x", headers={"Authorization": "Bearer t"})
        health = await http.get("/healthz")
        ready = await http.get("/readyz")
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert known.status_code == 404
    assert health.status_code == 200
    assert ready.status_code == 503
