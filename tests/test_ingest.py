"""POST /history/v1/ingest over a migrated database (Postgres: CI, or a local server)."""

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

import httpx
import pytest_asyncio
from conftest import POD_PEER, asgi_client

from agents_history.api.app import PREFIX, create_ingest_app

BEARER = "ingestbearer"
SID = uuid.UUID("6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b")
SID2 = uuid.UUID("7a2d3b4c-5e6f-4a71-9b8c-0d1e2f3a4b5c")
PRESET = "22222222-2222-4222-8222-222222222222"
PARENT = "33333333-3333-4333-8333-333333333333"
CREATED = "2026-11-01T10:00:00+00:00"

# The record kinds in the order agents_orchestrator/db/export.py writes them after the session
# record (its QUERIES); every kind but the session is stored in `records`.
EXPORT_KINDS = ("agent", "message", "turn", "command", "check", "event")


def session_record(session_id: uuid.UUID) -> dict:
    return {
        "kind": "session",
        "id": str(session_id),
        "slug": "alpha",
        "spec": {"agents": [{"name": "lead"}]},
        "created_at": CREATED,
        "preset_id": PRESET,
        "preset_name": "reviewer",
        "created_by_service": "webhook",
        "created_by_session": PARENT,
    }


AGENT_ID = "6f1c2a5e-3b7d-4c8e-9a10-2d4f6b8c0e11"


def kind_record(kind: str, session_id: uuid.UUID, n: int, data: dict | None = None) -> dict:
    sid = str(session_id)
    return {
        "agent": {
            "kind": "agent",
            "id": AGENT_ID,
            "session_id": sid,
            "ordinal": 0,
            "pod_name": "pod-lead",
            "created_at": CREATED,
        },
        "message": {"kind": "message", "id": n, "session_id": sid, "body": "hello"},
        "turn": {"kind": "turn", "turn_id": f"t{n}", "agent_id": AGENT_ID, "started_at": CREATED},
        "command": {"kind": "command", "id": n, "agent_id": AGENT_ID, "command_kind": "compact"},
        "check": {"kind": "check", "id": n, "session_id": sid, "ok": True},
        "event": {"kind": "event", "id": n, "session_id": sid, "type": "tool_result", "data": data},
    }[kind]


def export_lines(
    session_id: uuid.UUID, *, events: int = 3, event_data: dict | None = None
) -> list[bytes]:
    """The lines of an export shaped like the orchestrator's: one session record, then one
    record of each kind and `events` event records, in export order."""
    lines = [session_record(session_id)]
    for kind in EXPORT_KINDS:
        for n in range(1, (events if kind == "event" else 1) + 1):
            lines.append(kind_record(kind, session_id, n, event_data or {"output": f"out {n}"}))
    return [json.dumps(item, separators=(",", ":")).encode() for item in lines]


def export(session_id: uuid.UUID, **kwargs) -> bytes:
    return b"\n".join(export_lines(session_id, **kwargs)) + b"\n"


def stored_kinds(lines: list[bytes]) -> list[str]:
    """The kinds an export stores in `records`: every line after the session record."""
    return [json.loads(item)["kind"] for item in lines[1:]]


@pytest_asyncio.fixture
async def app(settings, pool):
    return create_ingest_app(settings, pool, [BEARER])


@pytest_asyncio.fixture
async def app_small(settings_for, pool):
    small = settings_for("postgresql://u:p@db.invalid/history", ingest_max_line_bytes=1048576)
    return create_ingest_app(small, pool, [BEARER])


async def post(app, body: bytes, *, sid: uuid.UUID | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {BEARER}"}
    if sid is not None:
        headers["X-Orch-Session-Id"] = str(sid)
    async with asgi_client(app, peer=POD_PEER) as http:
        return await http.post(f"{PREFIX}/ingest", content=body, headers=headers)


async def rows(db, query: str, *params) -> list[tuple]:
    cur = await db.execute(query, params)
    return await cur.fetchall()


async def test_stores_every_record_in_order(app, aconn):
    lines = export_lines(SID)
    body = b"\n".join(lines) + b"\n"
    before = datetime.now(UTC)
    r = await post(app, body, sid=SID)
    kinds = stored_kinds(lines)
    assert r.status_code == 201
    assert r.json() == {
        "session_id": str(SID),
        "records": len(kinds),
        "truncated": 0,
        "replaced": False,
    }
    records = await rows(
        aconn,
        "SELECT r.kind, r.truncated, r.ts IS NOT NULL FROM records r"
        " JOIN sessions s ON s.id = r.session_pk WHERE s.session_id = %s ORDER BY r.id",
        SID,
    )
    assert [kind for kind, _, _ in records] == kinds
    assert kinds == ["agent", "message", "turn", "command", "check", "event", "event", "event"]
    assert not any(truncated for _, truncated, _ in records)
    assert [has_ts for _, _, has_ts in records[:3]] == [True, False, True]
    [session] = await rows(
        aconn,
        "SELECT slug, preset_id, preset_name, created_by_service, created_by_session,"
        " created_at, discarded_at, session, record_counts, bytes FROM sessions"
        " WHERE session_id = %s",
        SID,
    )
    slug, preset_id, preset_name, service, parent, created, discarded, raw, counts, size = session
    assert (slug, str(preset_id), preset_name, service, str(parent)) == (
        "alpha",
        PRESET,
        "reviewer",
        "webhook",
        PARENT,
    )
    assert created == datetime.fromisoformat(CREATED)
    assert discarded >= before
    assert raw == session_record(SID)
    assert counts == {kind: kinds.count(kind) for kind in EXPORT_KINDS}
    assert counts == {"agent": 1, "message": 1, "event": 3, "turn": 1, "command": 1, "check": 1}
    assert size == sum(len(item) for item in body.split(b"\n") if item)


async def test_redelivery_replaces(app, aconn):
    lines = export_lines(SID)
    first = await post(app, export(SID), sid=SID)
    second = await post(app, export(SID), sid=SID)
    assert first.json()["replaced"] is False
    assert second.status_code == 201
    assert second.json()["replaced"] is True
    assert second.json()["records"] == len(stored_kinds(lines))
    assert await rows(aconn, "SELECT count(*) FROM records") == [(len(stored_kinds(lines)),)]
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(1,)]


async def test_concurrent_deliveries_of_one_session_both_succeed(app, aconn):
    """Two overlapping deliveries of a new session serialise on the session id: both answer 201
    (the later one replaces) and one copy is stored, instead of a UniqueViolation 500."""
    body = export(SID, events=2000)
    kinds = stored_kinds(export_lines(SID, events=2000))
    first, second = await asyncio.gather(post(app, body, sid=SID), post(app, body, sid=SID))
    assert [r.status_code for r in (first, second)] == [201, 201]
    assert sorted(r.json()["replaced"] for r in (first, second)) == [False, True]
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(1,)]
    assert await rows(aconn, "SELECT count(*) FROM records") == [(len(kinds),)]


async def test_bad_line_rolls_back_and_keeps_previous(app, aconn):
    good = export_lines(SID)
    assert (await post(app, export(SID), sid=SID)).status_code == 201
    [(pk,)] = await rows(aconn, "SELECT id FROM sessions WHERE session_id = %s", SID)
    broken = [*good[:3], b"{not json", *good[4:]]
    r = await post(app, b"\n".join(broken) + b"\n", sid=SID)
    assert r.status_code == 422
    assert r.json()["error"] == {"code": "bad_line", "message": "line 4: not valid JSON"}
    assert await rows(aconn, "SELECT id FROM sessions WHERE session_id = %s", SID) == [(pk,)]
    count = await rows(aconn, "SELECT count(*) FROM records WHERE session_pk = %s", pk)
    assert count == [(len(stored_kinds(good)),)]


async def test_header_mismatch_stores_nothing(app, aconn):
    r = await post(app, export(SID), sid=SID2)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_export"
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(0,)]
    assert await rows(aconn, "SELECT count(*) FROM records") == [(0,)]


async def test_first_line_not_session(app, aconn):
    lines = export_lines(SID)
    r = await post(app, b"\n".join(lines[1:]) + b"\n", sid=SID)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_export"
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(0,)]


async def test_empty_body(app, aconn):
    r = await post(app, b"", sid=SID)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_export"
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(0,)]


async def test_missing_or_bad_header(app, aconn):
    for sid_header in (None, "nope"):
        headers = {"Authorization": f"Bearer {BEARER}"}
        if sid_header is not None:
            headers["X-Orch-Session-Id"] = sid_header
        async with asgi_client(app, peer=POD_PEER) as http:
            r = await http.post(f"{PREFIX}/ingest", content=export(SID), headers=headers)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_request"
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(0,)]


async def test_oversize_line_stored_truncated(app_small, aconn):
    lines = export_lines(SID)
    huge = json.dumps(
        {"kind": "event", "id": 99, "data": {"output": "x" * (2 * 1048576)}},
        separators=(",", ":"),
    ).encode()
    body = b"\n".join([*lines, huge]) + b"\n"
    r = await post(app_small, body, sid=SID)
    assert r.status_code == 201
    assert r.json()["truncated"] == 1
    assert r.json()["records"] == len(stored_kinds(lines)) + 1
    [(kind, truncated, stored, length)] = await rows(
        aconn, "SELECT kind, truncated, body, length(text) FROM records WHERE truncated"
    )
    assert (kind, truncated) == ("event", True)
    assert stored == {"kind": "event", "id": 99, "truncated": True, "bytes": len(huge)}
    assert length <= 1048576
    [(size,)] = await rows(aconn, "SELECT bytes FROM sessions WHERE session_id = %s", SID)
    assert size == sum(len(item) for item in body.split(b"\n") if item)


async def test_text_column_is_searchable(app, aconn):
    r = await post(app, export(SID, events=1, event_data={"text": "Needle42"}), sid=SID)
    assert r.status_code == 201
    found = await rows(aconn, "SELECT count(*) FROM records WHERE text ILIKE '%%needle42%%'")
    assert found == [(1,)]


async def test_nul_surrogates_and_nan_are_cleaned_before_jsonb(app, aconn):
    """S-R7: Postgres jsonb refuses NUL, lone surrogates and NaN; the export is stored with
    them cleaned, never refused."""
    lines = export_lines(SID, events=1)
    dirty = (
        b'{"kind":"event","id":7,"data":{"a\\u0000k":"x\\u0000y","s":"p\\ud800q",'
        b'"n":NaN,"deep":[["z\\u0000"]]}}'
    )
    session = json.loads(lines[0])
    session["spec"] = {"note": "spec\u0000with nul"}
    head = json.dumps(session).encode()
    body = b"\n".join([head, *lines[1:], dirty]) + b"\n"
    r = await post(app, body, sid=SID)
    assert r.status_code == 201
    [(stored, text)] = await rows(aconn, "SELECT body, text FROM records WHERE body->>'id' = '7'")
    assert stored == {
        "kind": "event",
        "id": 7,
        "data": {"ak": "xy", "s": "p\ufffdq", "n": None, "deep": [["z"]]},
    }
    assert text == "event\nxy\np\ufffdq\nz"
    [(raw,)] = await rows(aconn, "SELECT session FROM sessions WHERE session_id = %s", SID)
    assert raw["spec"] == {"note": "specwith nul"}


async def test_wrong_token_401(app, aconn):
    async with asgi_client(app, peer=POD_PEER) as http:
        r = await http.post(
            f"{PREFIX}/ingest",
            content=export(SID),
            headers={"Authorization": "Bearer other", "X-Orch-Session-Id": str(SID)},
        )
    assert r.status_code == 401
    assert await rows(aconn, "SELECT count(*) FROM sessions") == [(0,)]


async def test_body_never_logged(app, aconn, caplog):
    caplog.set_level(logging.DEBUG)
    ok = await post(app, export(SID, event_data={"secret": "s3cr3t-value"}), sid=SID)
    lines = export_lines(SID2, event_data={"secret": "s3cr3t-value"})
    refused = await post(app, b"\n".join([*lines, b'{"s3cr3t-value']) + b"\n", sid=SID2)
    assert ok.status_code == 201
    assert refused.status_code == 422
    messages = [record.getMessage() for record in caplog.records]
    assert any(str(SID) in message for message in messages)
    assert not any("s3cr3t-value" in message or BEARER in message for message in messages)
