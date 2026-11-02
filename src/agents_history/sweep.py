"""The retention sweep: deletes sessions discarded longer ago than the retention period.

It runs inside the api process (the api stays the only writer of every table). A transaction
level advisory lock keeps replicas from sweeping at the same time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from psycopg_pool import AsyncConnectionPool

from agents_history.settings import Settings

log = logging.getLogger(__name__)

LOCK_KEY = "agents-history-sweep"


async def sweep_once(pool: AsyncConnectionPool, *, batch: int) -> int | None:
    """Delete every session older than the retention period, `batch` sessions per statement,
    and record the outcome in the settings row. The number deleted, or None when another
    sweep holds the lock."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        row = await cur.fetchone()
        if row is None or not row[0]:
            return None
        cur = await conn.execute("SELECT retention_days FROM settings WHERE id = 1")
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("settings row is missing: run the migrations")
        days = row[0]
        deleted = 0
        while True:
            cur = await conn.execute(
                "DELETE FROM sessions WHERE id IN ("
                " SELECT id FROM sessions"
                " WHERE discarded_at < now() - make_interval(days => %s)"
                " ORDER BY id LIMIT %s)",
                (days, batch),
            )
            deleted += cur.rowcount
            if cur.rowcount < batch:
                break
        await conn.execute(
            "UPDATE settings SET last_sweep_at = now(), last_sweep_deleted = %s WHERE id = 1",
            (deleted,),
        )
    return deleted


async def _first_of(*events: asyncio.Event) -> None:
    """Returns once any of the events is set; cancels its waiters when it is cancelled."""
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


async def run_sweeper(
    pool: AsyncConnectionPool,
    settings: Settings,
    stop: asyncio.Event,
    wake: asyncio.Event,
    *,
    sleep=asyncio.wait_for,
) -> None:
    """Sweep at start, then on every interval and every time `wake` is set (a lowered
    retention), until `stop` is set. An error is logged and the loop carries on. `sleep` is
    called as `sleep(awaitable, timeout=seconds)`, raising TimeoutError when the interval is up."""
    while not stop.is_set():
        wake.clear()
        try:
            deleted = await sweep_once(pool, batch=settings.sweep_batch)
            if deleted:
                log.info("retention sweep deleted %d sessions", deleted)
        except Exception:
            log.exception("retention sweep failed")
        with contextlib.suppress(TimeoutError):
            await sleep(_first_of(stop, wake), timeout=settings.sweep_interval_seconds)
