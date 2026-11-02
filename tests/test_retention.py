"""Retention settings and the sweep over a migrated database (Postgres: CI, or a local server).
Sessions are seeded through `replace_session`; `discarded_at` is moved with UPDATE."""

import uuid
from datetime import UTC, datetime, timedelta

from agents_history.api.app import PREFIX
from agents_history.db.sessions import replace_session
from agents_history.export import Record, SessionHead, record_text
from agents_history.sweep import LOCK_KEY, sweep_once

SETTINGS = f"{PREFIX}/settings"
CREATED = datetime(2026, 1, 1, tzinfo=UTC)


async def seed(db, days_ago: float) -> uuid.UUID:
    """One stored session with one record, discarded `days_ago` days ago."""
    sid = uuid.uuid4()
    raw = {"kind": "session", "id": str(sid), "slug": "s"}
    head = SessionHead(sid, "s", None, None, None, None, CREATED, raw, 50)
    body = {"kind": "note", "body": "hello"}

    async def stream():
        yield Record("note", CREATED, body, record_text(body), False, 10)

    await replace_session(db, head, stream())
    await db.execute(
        "UPDATE sessions SET discarded_at = now() - make_interval(days => %s)"
        " WHERE session_id = %s",
        (days_ago, sid),
    )
    return sid


async def stored(db) -> set[uuid.UUID]:
    cur = await db.execute("SELECT session_id FROM sessions")
    return {row[0] for row in await cur.fetchall()}


async def test_default_ninety(ui_client):
    r = await ui_client.get(SETTINGS)
    assert r.status_code == 200
    body = r.json()
    assert body["retention_days"] == 90
    assert body["last_sweep_at"] is None
    assert body["last_sweep_deleted"] is None
    assert body["updated_at"]


async def test_put_bounds(ui_client):
    for bad in (0, 3651):
        r = await ui_client.put(SETTINGS, json={"retention_days": bad})
        assert r.status_code == 422, bad
        assert r.json()["error"]["code"] == "invalid_request"
    r = await ui_client.put(SETTINGS, json={"retention_days": 30, "x": 1})
    assert r.status_code == 422
    assert (await ui_client.get(SETTINGS)).json()["retention_days"] == 90


async def test_put_persists(ui_client):
    r = await ui_client.put(SETTINGS, json={"retention_days": 30})
    assert r.status_code == 200
    assert r.json()["retention_days"] == 30
    assert (await ui_client.get(SETTINGS)).json()["retention_days"] == 30
    for edge in (1, 3650):
        r = await ui_client.put(SETTINGS, json={"retention_days": edge})
        assert r.status_code == 200


async def test_sweep_deletes_only_older(db, pool):
    await db.execute("UPDATE settings SET retention_days = 10")
    old, recent = await seed(db, 11), await seed(db, 9)
    assert await sweep_once(pool, batch=50) == 1
    assert await stored(db) == {recent}
    cur = await db.execute("SELECT count(*) FROM records")
    assert (await cur.fetchone())[0] == 1  # the old session's record went with it
    cur = await db.execute("SELECT last_sweep_at, last_sweep_deleted FROM settings")
    at, deleted = await cur.fetchone()
    assert at is not None and datetime.now(UTC) - at < timedelta(minutes=1)
    assert deleted == 1
    assert old not in await stored(db)


async def test_sweep_batches(db, pool):
    await db.execute("UPDATE settings SET retention_days = 10")
    for _ in range(7):
        await seed(db, 20)
    keep = await seed(db, 1)
    assert await sweep_once(pool, batch=3) == 7
    assert await stored(db) == {keep}
    cur = await db.execute("SELECT last_sweep_deleted FROM settings")
    assert (await cur.fetchone())[0] == 7


async def test_sweep_with_nothing_old_records_zero(db, pool):
    await seed(db, 1)
    assert await sweep_once(pool, batch=3) == 0
    cur = await db.execute("SELECT last_sweep_at, last_sweep_deleted FROM settings")
    at, deleted = await cur.fetchone()
    assert at is not None and deleted == 0


async def test_sweep_single_flight(db, pool):
    await db.execute("UPDATE settings SET retention_days = 10")
    await seed(db, 20)
    async with db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        assert await sweep_once(pool, batch=50) is None
    assert len(await stored(db)) == 1  # the refused sweep deleted nothing
    assert await sweep_once(pool, batch=50) == 1


async def test_lowering_wakes_sweeper(ui_client, app):
    wake = app.state.sweep_wake
    assert not wake.is_set()
    assert (await ui_client.put(SETTINGS, json={"retention_days": 30})).status_code == 200
    assert wake.is_set()
    wake.clear()
    assert (await ui_client.put(SETTINGS, json={"retention_days": 60})).status_code == 200
    assert not wake.is_set()
    assert (await ui_client.put(SETTINGS, json={"retention_days": 60})).status_code == 200
    assert not wake.is_set()


async def test_settings_not_on_service_or_ingest(service_client, ingest_client):
    assert (await service_client.get(SETTINGS)).status_code == 404
    assert (await ingest_client.get(SETTINGS)).status_code == 404
    body = {"retention_days": 1}
    assert (await service_client.put(SETTINGS, json=body)).status_code == 404
    assert (await ingest_client.put(SETTINGS, json=body)).status_code == 404
