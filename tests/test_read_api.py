"""List, search, detail, records and delete over a migrated database (Postgres: CI, or a local
server). Sessions are seeded through `replace_session`; `discarded_at` is moved with UPDATE."""

import uuid
from datetime import UTC, datetime

import pytest_asyncio
from conftest import asgi_client

from agents_history.api.app import PREFIX
from agents_history.db.sessions import replace_session
from agents_history.export import Record, SessionHead, record_text

CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def day(n: int) -> datetime:
    return datetime(2026, 1, n, tzinfo=UTC)


SESSIONS = f"{PREFIX}/sessions"


@pytest_asyncio.fixture
async def app(ui_app):
    return ui_app


@pytest_asyncio.fixture
async def client(ui_client):
    return ui_client


@pytest_asyncio.fixture
async def db(aconn):
    return aconn


def rec(kind: str, text: str = "", **body) -> Record:
    body = {"kind": kind, **({"body": text} if text else {}), **body}
    return Record(kind, CREATED, body, record_text(body), False, 10)


async def seed(
    db,
    records: list[Record] | None = None,
    *,
    slug: str = "alpha",
    preset_id: uuid.UUID | None = None,
    preset_name: str | None = None,
    created_at: datetime = CREATED,
    session_id: uuid.UUID | None = None,
    discarded_at: datetime | None = None,
) -> uuid.UUID:
    sid = session_id or uuid.uuid4()
    raw = {"kind": "session", "id": str(sid), "slug": slug}
    head = SessionHead(sid, slug, preset_id, preset_name, None, None, created_at, raw, 50)

    async def stream():
        for record in records or []:
            yield record

    await replace_session(db, head, stream())
    if discarded_at is not None:
        await db.execute(
            "UPDATE sessions SET discarded_at = %s WHERE session_id = %s", (discarded_at, sid)
        )
    return sid


async def listed(client, **params) -> list[dict]:
    r = await client.get(SESSIONS, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_newest_first_and_paging(client, db):
    for slug in ("one", "two", "three"):
        await seed(db, slug=slug)
    page = await listed(client, limit=2)
    assert [s["id"] for s in page] == [3, 2]
    assert [s["slug"] for s in page] == ["three", "two"]
    assert [s["id"] for s in await listed(client, limit=2, before_id=2)] == [1]
    first = page[0]
    assert first["hits"] is None and first["matches"] == []
    assert first["record_counts"] == {} and first["bytes"] == 50


async def test_list_is_a_json_list(client, db):
    await seed(db)
    r = await client.get(SESSIONS)
    assert isinstance(r.json(), list)


async def test_list_empty(client):
    assert await listed(client) == []


async def test_substring_case_insensitive(client, db):
    sid = await seed(db, [rec("message", "Deploy FAILED on prod")])
    await seed(db, [rec("message", "all good")])
    [found] = await listed(client, q="deploy failed")
    assert found["session_id"] == str(sid)
    assert found["hits"] == 1
    [match] = found["matches"]
    assert "Deploy FAILED" in match["snippet"]
    assert match["kind"] == "message" and match["record_id"] > 0 and match["ts"] is not None


async def test_like_wildcards_are_literal(client, db):
    first = await seed(db, [rec("message", "100%_done")])
    await seed(db, [rec("message", "100xxdone")])
    await seed(db, [rec("message", "back\\slash")])
    assert [s["session_id"] for s in await listed(client, q="100%_")] == [str(first)]
    assert len(await listed(client, q="k\\s")) == 1


async def test_regex(client, db):
    a = await seed(db, [rec("message", "it failed")])
    b = await seed(db, [rec("message", "a failure")])
    await seed(db, [rec("message", "fine")])
    got = await listed(client, q="fail(ed|ure)", regex="true")
    assert [s["session_id"] for s in got] == [str(b), str(a)]
    assert "failure" in got[0]["matches"][0]["snippet"]


async def test_regex_is_case_insensitive_and_literal_mode_is_not_regex(client, db):
    await seed(db, [rec("message", "FAILED")])
    assert len(await listed(client, q="fail.d", regex="true")) == 1
    assert await listed(client, q="fail.d") == []


async def test_invalid_regex_422(client, db):
    r = await client.get(SESSIONS, params={"q": "(abc", "regex": "true"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_regex"
    await seed(db, [rec("message", "abc")])
    r = await client.get(SESSIONS, params={"q": "(abc", "regex": "true"})
    assert r.json()["error"]["code"] == "invalid_regex"


async def test_short_query_422(client, db):
    sid = await seed(db, [rec("message", "abc")])
    for q in ("ab", "x" * 501):
        r = await client.get(SESSIONS, params={"q": q})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "query_too_short"
    r = await client.get(f"{SESSIONS}/{sid}/records", params={"q": "ab"})
    assert r.json()["error"]["code"] == "query_too_short"
    assert len(await listed(client, q="x" * 500, regex="false")) == 0


async def test_search_timeout_422(client, app, db):
    app.state.search_timeout_ms = 1
    await seed(db, [rec("event", "abcd" * 50 + str(n)) for n in range(5000)])
    r = await client.get(SESSIONS, params={"q": ".*a.*b.*c.*d.*e", "regex": "true"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "search_timeout"


async def test_matches_capped_at_three(client, db):
    await seed(db, [rec("message", f"needle {n}") for n in range(5)] + [rec("message", "other")])
    [found] = await listed(client, q="needle")
    assert found["hits"] == 5
    assert len(found["matches"]) == 3
    ids = [m["record_id"] for m in found["matches"]]
    assert ids == sorted(ids)


async def test_page_is_filled_with_matching_sessions(client, db):
    """Filters go to the query: the page holds the newest matching sessions, never [] while
    older ones match."""
    old = await seed(db, [rec("message", "needle here")], slug="old")
    for n in range(3):
        await seed(db, [rec("message", "nothing")], slug=f"new{n}")
    page = await listed(client, q="needle", limit=1)
    assert [s["session_id"] for s in page] == [str(old)]
    assert await listed(client, q="needle", limit=1, before_id=page[0]["id"]) == []


async def test_filters(client, db):
    preset = uuid.uuid4()
    other = uuid.uuid4()
    a = await seed(
        db,
        slug="abc-one",
        preset_id=preset,
        preset_name="reviewer",
        created_at=day(1),
        discarded_at=day(10),
    )
    b = await seed(
        db,
        slug="a_c-two",
        preset_id=other,
        preset_name="triager",
        created_at=day(10),
        discarded_at=day(20),
    )
    c = await seed(db, slug="zzz", preset_name="reviewer", created_at=day(25), discarded_at=day(28))

    def ids(rows):
        return [s["session_id"] for s in rows]

    # life [created_at, discarded_at] overlaps [from, to): discarded_at == from is kept,
    # created_at == to is dropped.
    window = await listed(client, **{"from": day(10).isoformat(), "to": day(25).isoformat()})
    assert ids(window) == [str(b), str(a)]
    assert ids(await listed(client, **{"from": day(11).isoformat()})) == [str(c), str(b)]
    assert ids(await listed(client, to=day(10).isoformat())) == [str(a)]
    assert ids(await listed(client, preset=str(preset))) == [str(a)]
    assert ids(await listed(client, preset="reviewer")) == [str(c), str(a)]
    assert ids(await listed(client, preset="review")) == []
    assert ids(await listed(client, session=str(c))) == [str(c)]
    assert ids(await listed(client, session="abc")) == [str(a)]
    assert ids(await listed(client, session="a_c")) == [str(b)]  # _ is literal, not a wildcard
    assert ids(await listed(client, session="ONE")) == [str(a)]
    assert ids(await listed(client, preset="reviewer", session="zz")) == [str(c)]


async def test_filters_apply_with_q(client, db):
    keep = await seed(db, [rec("message", "needle")], preset_name="keep")
    await seed(db, [rec("message", "needle")], preset_name="drop")
    assert [s["session_id"] for s in await listed(client, q="needle", preset="keep")] == [str(keep)]


async def test_detail_with_agents(client, db):
    sid = await seed(
        db,
        [
            rec("agent", id=7, ordinal=0, pod_name="pod-a"),
            rec("agent", id="x", ordinal=1),
            rec("message", "hi"),
        ],
        slug="alpha",
    )
    r = await client.get(f"{SESSIONS}/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["session"]["slug"] == "alpha"
    assert body["agents"] == [{"id": 7, "ordinal": 0, "pod_name": "pod-a"}]
    assert body["record_counts"] == {"agent": 2, "message": 1}
    assert body["session_id"] == str(sid) and body["hits"] is None and body["matches"] == []


async def test_unknown_session_404(client):
    missing = uuid.uuid4()
    for method, path in (
        ("GET", f"{SESSIONS}/{missing}"),
        ("GET", f"{SESSIONS}/{missing}/records"),
        ("DELETE", f"{SESSIONS}/{missing}"),
    ):
        r = await client.request(method, path)
        assert r.status_code == 404, (method, path)
        assert r.json()["error"]["code"] == "not_found"


async def test_records_pages(client, db):
    sid = await seed(db, [rec("event", f"event {n}") for n in range(250)])
    url = f"{SESSIONS}/{sid}/records"
    newest = (await client.get(url)).json()
    ids = [r["id"] for r in newest["records"]]
    assert len(ids) == 200 and ids == sorted(ids)
    assert newest["has_more_before"] is True and newest["next_after_id"] is None
    assert newest["records"][-1]["body"]["body"] == "event 249"
    older = (await client.get(url, params={"before_id": ids[0]})).json()
    assert len(older["records"]) == 50
    assert older["has_more_before"] is False
    assert older["records"][-1]["id"] == ids[0] - 1
    first = (await client.get(url, params={"after_id": 0, "limit": 100})).json()
    assert len(first["records"]) == 100
    assert first["has_more_before"] is False
    assert first["next_after_id"] == first["records"][-1]["id"]
    rest = (await client.get(url, params={"after_id": first["next_after_id"], "limit": 200})).json()
    assert len(rest["records"]) == 150
    assert rest["has_more_before"] is True and rest["next_after_id"] is None
    assert rest["records"][0]["id"] == first["records"][-1]["id"] + 1
    last = (await client.get(url, params={"after_id": 0, "limit": 250})).json()
    assert len(last["records"]) == 250 and last["next_after_id"] is None
    one = (await client.get(url, params={"limit": 1})).json()
    assert [r["id"] for r in one["records"]] == [ids[-1]]
    record = newest["records"][0]
    assert set(record) == {"id", "kind", "ts", "truncated", "body"}
    assert record["truncated"] is False


async def test_records_kind_and_q(client, db):
    sid = await seed(
        db,
        [
            rec("message", "a needle"),
            rec("turn", "needle in turn"),
            rec("event", "needle in event"),
            rec("message", "plain"),
        ],
    )
    url = f"{SESSIONS}/{sid}/records"
    got = (await client.get(url, params={"kind": "message,turn"})).json()["records"]
    assert [r["kind"] for r in got] == ["message", "turn", "message"]
    got = (await client.get(url, params={"q": "needle"})).json()["records"]
    assert [r["kind"] for r in got] == ["message", "turn", "event"]
    got = (await client.get(url, params={"q": "NEED.E", "regex": "true", "kind": "event"})).json()
    assert [r["kind"] for r in got["records"]] == ["event"]
    r = await client.get(url, params={"q": "(abc", "regex": "true"})
    assert r.json()["error"]["code"] == "invalid_regex"


async def test_cursor_conflict_400(client, db):
    sid = await seed(db, [rec("event", "x")])
    r = await client.get(f"{SESSIONS}/{sid}/records", params={"before_id": 5, "after_id": 1})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


async def test_delete(client, db):
    sid = await seed(db, [rec("event", "x"), rec("event", "y")])
    keep = await seed(db, [rec("event", "z")])
    r = await client.delete(f"{SESSIONS}/{sid}")
    assert r.status_code == 204 and r.content == b""
    assert (await client.delete(f"{SESSIONS}/{sid}")).status_code == 404
    cur = await db.execute("SELECT count(*) FROM records")
    assert await cur.fetchone() == (1,)
    assert [s["session_id"] for s in await listed(client)] == [str(keep)]


async def test_service_listener_serves_read(service_client, db):
    sid = await seed(db, [rec("message", "needle")])
    assert [s["session_id"] for s in (await service_client.get(SESSIONS)).json()] == [str(sid)]
    found = await service_client.get(SESSIONS, params={"q": "needle"})
    assert found.json()[0]["hits"] == 1
    assert (await service_client.get(f"{SESSIONS}/{sid}")).status_code == 200
    assert (await service_client.get(f"{SESSIONS}/{sid}/records")).status_code == 200
    assert (await service_client.get(f"{PREFIX}/settings")).status_code == 404


async def test_service_listener_refuses_delete(service_client, db):
    sid = await seed(db, [rec("message", "needle")])
    r = await service_client.delete(f"{SESSIONS}/{sid}")
    assert r.status_code == 405
    cur = await db.execute("SELECT count(*) FROM sessions")
    assert await cur.fetchone() == (1,)
    cur = await db.execute("SELECT count(*) FROM records")
    assert await cur.fetchone() == (1,)


async def test_service_listener_needs_the_bearer(service_app):
    async with asgi_client(service_app, peer="10.9.9.9") as http:
        assert (await http.get(SESSIONS)).status_code == 401


async def test_trigram_index_usable(db):
    await db.execute("SET enable_seqscan = off")
    cur = await db.execute("EXPLAIN SELECT id FROM records WHERE text ILIKE '%needle%'")
    plan = "\n".join(row[0] for row in await cur.fetchall())
    assert "records_text_trgm_idx" in plan
