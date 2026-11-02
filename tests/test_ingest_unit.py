"""Ingest pieces that need no database: header parsing, jsonb cleaning, the gate and routing."""

import json
import math
import uuid

import pytest
from conftest import POD_PEER, SERVICE_TOKEN, DownPool, asgi_client

from agents_history.api.app import PREFIX, create_app, create_ingest_app, create_service_app
from agents_history.api.errors import ApiError
from agents_history.api.ingest import INGEST_ROUTES, session_id_header
from agents_history.db.sessions import jsonb_safe

BEARER = "ingestbearer"
SID = uuid.UUID("6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b")


@pytest.fixture
def ingest(settings):
    return create_ingest_app(settings, DownPool(), [BEARER])


def test_header_parse():
    assert session_id_header(str(SID)) == SID
    for value in (None, "", "nope"):
        with pytest.raises(ApiError) as exc:
            session_id_header(value)
        assert (exc.value.status, exc.value.code) == (422, "invalid_request")
        assert "X-Orch-Session-Id" in exc.value.message


def test_jsonb_safe_cleans_strings_keys_and_non_finite_numbers():
    body = json.loads(
        '{"a\\u0000b": ["x\\u0000y", {"k": "\\ud800z"}], "n": NaN, "i": -Infinity,'
        ' "ok": "\\ud83d\\ude00", "f": 1.5, "t": true, "z": null}'
    )
    assert jsonb_safe(body) == {
        "ab": ["xy", {"k": "\ufffdz"}],
        "n": None,
        "i": None,
        "ok": "\U0001f600",
        "f": 1.5,
        "t": True,
        "z": None,
    }
    assert body["a\x00b"] == ["x\x00y", {"k": "\ud800z"}]  # the input is left as it was
    assert math.isnan(body["n"])


def test_jsonb_safe_scalars_and_deep_nesting():
    assert jsonb_safe("a\x00") == "a"
    assert jsonb_safe(3) == 3
    deep = json.loads("[" * 5000 + '"\\u0000x"' + "]" * 5000)
    cleaned = jsonb_safe(deep)
    for _ in range(5000):
        cleaned = cleaned[0]
    assert cleaned == "x"


def test_ingest_routes_match_the_ingest_app(settings):
    paths = create_ingest_app(settings, DownPool(), [BEARER]).openapi()["paths"]
    routes = [
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith(PREFIX)
        for method in operations
    ]
    assert routes == INGEST_ROUTES == [("POST", "/history/v1/ingest")]


async def test_wrong_token_401(ingest):
    async with asgi_client(ingest, peer=POD_PEER) as http:
        response = await http.post(
            f"{PREFIX}/ingest",
            content=b"{}\n",
            headers={"Authorization": "Bearer other", "X-Orch-Session-Id": str(SID)},
        )
    assert response.status_code == 401


async def test_missing_or_bad_header(ingest):
    async with asgi_client(ingest, peer=POD_PEER) as http:
        http.headers["Authorization"] = f"Bearer {BEARER}"
        missing = await http.post(f"{PREFIX}/ingest", content=b"{}\n")
        bad = await http.post(
            f"{PREFIX}/ingest", content=b"{}\n", headers={"X-Orch-Session-Id": "nope"}
        )
    for response in (missing, bad):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


async def test_empty_body(ingest):
    async with asgi_client(ingest, peer=POD_PEER) as http:
        http.headers["Authorization"] = f"Bearer {BEARER}"
        http.headers["X-Orch-Session-Id"] = str(SID)
        empty = await http.post(f"{PREFIX}/ingest", content=b"")
        blank = await http.post(f"{PREFIX}/ingest", content=b"\n\n")
    for response in (empty, blank):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "empty_export"


async def test_ingest_not_on_ui_or_service_app(settings):
    ui = create_app(settings, DownPool())
    service = create_service_app(settings, DownPool(), [SERVICE_TOKEN])
    async with asgi_client(ui) as http:
        on_ui = await http.post(f"{PREFIX}/ingest", content=b"{}\n")
    async with asgi_client(service, peer=POD_PEER) as http:
        on_service = await http.post(
            f"{PREFIX}/ingest",
            content=b"{}\n",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        )
    assert on_ui.status_code in (404, 405)
    assert on_service.status_code in (404, 405)
