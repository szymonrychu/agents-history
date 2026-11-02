"""The retention settings routes (UI listener only)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from agents_history.api.schemas import RetentionIn, RetentionView
from agents_history.db.settings import get_retention, set_retention

router = APIRouter()


@router.get("/settings")
async def read_settings(request: Request) -> RetentionView:
    async with request.app.state.pool.connection() as conn:
        return RetentionView(**await get_retention(conn))


@router.put("/settings")
async def write_settings(body: RetentionIn, request: Request) -> RetentionView:
    """Store the retention period; a shorter one wakes the sweeper so it applies at once."""
    async with request.app.state.pool.connection() as conn:
        row, previous = await set_retention(conn, body.retention_days)
    if body.retention_days < previous:
        request.app.state.sweep_wake.set()
    return RetentionView(**row)
