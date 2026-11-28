"""The committed OpenAPI documents per listener."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from agents_history.api import openapi
from agents_history.api.app import PREFIX, create_app, create_ingest_app, create_service_app
from agents_history.api.ingest import INGEST_ROUTES
from agents_history.settings import Settings

ROOT = Path(__file__).resolve().parent.parent

READ_ROUTES = {
    ("get", "/history/v1/sessions"),
    ("get", "/history/v1/sessions/{session_id}"),
    ("get", "/history/v1/sessions/{session_id}/records"),
}
DELETE_ROUTE = ("delete", "/history/v1/sessions/{session_id}")
SETTINGS_ROUTES = {("get", "/history/v1/settings"), ("put", "/history/v1/settings")}


def doc_routes(doc: dict) -> set[tuple[str, str]]:
    return {(method, path) for path, methods in doc["paths"].items() for method in methods}


def app_routes(app: FastAPI) -> set[tuple[str, str]]:
    """The API routes of a listener, read from its generated OpenAPI (FastAPI includes routers
    lazily, so `app.routes` does not list them); the probes are not API."""
    paths = app.openapi()["paths"]
    return {(m, p) for p, methods in paths.items() if p.startswith(PREFIX) for m in methods}


def test_committed_documents_match() -> None:
    ui = (ROOT / "openapi.json").read_text(encoding="utf-8")
    service = (ROOT / "openapi-service.json").read_text(encoding="utf-8")
    assert ui == openapi.export(), "run: uv run python -m agents_history.api.openapi > openapi.json"
    assert service == openapi.export(service=True), (
        "run: uv run python -m agents_history.api.openapi --service > openapi-service.json"
    )
    assert ui.endswith("\n") and service.endswith("\n")


def test_service_document_routes() -> None:
    doc = openapi.document(service=True)
    assert doc_routes(doc) == READ_ROUTES
    assert "delete" not in {m for m, _ in doc_routes(doc)}  # Q6: the superleader never deletes


def test_ui_document_routes() -> None:
    routes = doc_routes(openapi.document())
    assert routes == READ_ROUTES | {DELETE_ROUTE} | SETTINGS_ROUTES
    assert not any(p.endswith("/ingest") for _, p in routes)


def test_routes_per_listener_match_apps() -> None:
    settings = Settings(database_url="postgresql://unused/db")
    ui = create_app(settings, pool=None)  # type: ignore[arg-type]
    service = create_service_app(settings, pool=None, tokens=["t"])  # type: ignore[arg-type]
    ingest = create_ingest_app(settings, pool=None, tokens=["t"])  # type: ignore[arg-type]
    assert app_routes(ui) == doc_routes(openapi.document())
    assert app_routes(service) == doc_routes(openapi.document(service=True))
    assert app_routes(ingest) == {(m.lower(), p) for m, p in INGEST_ROUTES}
