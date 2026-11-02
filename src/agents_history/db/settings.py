"""The single `settings` row: the retention period and the last sweep outcome (the api process
is its only writer)."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

COLUMNS = "retention_days, updated_at, last_sweep_at, last_sweep_deleted"


async def get_retention(conn: psycopg.AsyncConnection) -> dict:
    """The settings row as a dict: retention_days, updated_at, last_sweep_at, last_sweep_deleted."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {COLUMNS} FROM settings WHERE id = 1")
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("settings row is missing: run the migrations")
    return row


async def set_retention(conn: psycopg.AsyncConnection, days: int) -> tuple[dict, int]:
    """Store a new retention period; the new row and the previous number of days. The row is
    locked while read, so two concurrent changes each see the other's value as previous."""
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT retention_days FROM settings WHERE id = 1 FOR UPDATE")
        old = await cur.fetchone()
        if old is None:
            raise RuntimeError("settings row is missing: run the migrations")
        await cur.execute(
            f"UPDATE settings SET retention_days = %s, updated_at = now() WHERE id = 1"
            f" RETURNING {COLUMNS}",
            (days,),
        )
        row = await cur.fetchone()
    assert row is not None
    return row, old["retention_days"]
