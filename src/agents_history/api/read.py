"""Reads of stored sessions: list and search, detail, records; and the user-only delete.

`router` (the three GET routes) is mounted on the UI and the service listener. `delete_router`
is mounted on the UI listener only: the service listener (the superleader relay) answers 405 to
DELETE, so the superleader can read and search but never delete.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

import psycopg.errors
from fastapi import APIRouter, Query, Request, Response

from agents_history.api.errors import ApiError
from agents_history.api.schemas import (
    AgentView,
    MatchView,
    RecordsPage,
    RecordView,
    SessionDetail,
    SessionSummary,
)
from agents_history.db import read
from agents_history.db.sessions import delete_session

router = APIRouter()
delete_router = APIRouter()

Q_MIN = 3
Q_MAX = 500


def checked_q(q: str | None) -> str | None:
    """`q` when it is 3 to 500 characters, None when absent or empty, else 422."""
    if q is None or q == "":
        return None
    if not Q_MIN <= len(q) <= Q_MAX:
        raise ApiError(422, "query_too_short", f"q must be {Q_MIN} to {Q_MAX} characters")
    return q


@contextmanager
def search_errors() -> Iterator[None]:
    """Postgres refusals of a user's pattern or a runaway search as their 422 errors."""
    try:
        yield
    except psycopg.errors.InvalidRegularExpression:
        raise ApiError(422, "invalid_regex", "q is not a valid regular expression") from None
    except psycopg.errors.QueryCanceled:
        raise ApiError(422, "search_timeout", "the search took too long, narrow it") from None


@router.get("/sessions")
async def list_sessions(
    request: Request,
    q: str | None = None,
    regex: bool = False,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    preset: str | None = None,
    session: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[SessionSummary]:
    q = checked_q(q)
    f = read.SessionFilter(q, regex, from_, to, preset, session, limit, before_id)
    with search_errors():
        async with request.app.state.pool.connection() as conn:
            rows = await read.list_sessions(conn, f, request.app.state.search_timeout_ms)
    return [_summary(row, q, regex) for row in rows]


def _summary(row: dict, q: str | None, regex: bool) -> SessionSummary:
    matches = [
        MatchView(
            record_id=m["record_id"],
            kind=m["kind"],
            ts=m["ts"],
            snippet=read.snippet(m["text"], q or "", regex),
        )
        for m in row["matches"]
    ]
    return SessionSummary(**{**row, "matches": matches})


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: uuid.UUID) -> SessionDetail:
    async with request.app.state.pool.connection() as conn:
        row = await read.get_session(conn, session_id)
        if row is None:
            raise _not_found()
        agents = await read.agents_of(conn, row["id"])
    return SessionDetail(**row, agents=[AgentView(**a) for a in agents])


@router.get("/sessions/{session_id}/records")
async def get_records(
    request: Request,
    session_id: uuid.UUID,
    kind: str | None = None,
    q: str | None = None,
    regex: bool = False,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    after_id: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> RecordsPage:
    if before_id is not None and after_id is not None:
        raise ApiError(400, "bad_request", "before_id and after_id cannot be combined")
    q = checked_q(q)
    kinds = [k for k in (part.strip() for part in (kind or "").split(",")) if k] or None
    with search_errors():
        async with request.app.state.pool.connection() as conn:
            row = await read.get_session(conn, session_id)
            if row is None:
                raise _not_found()
            records, has_more_before, next_after_id = await read.records_page(
                conn,
                row["id"],
                kinds=kinds,
                q=q,
                regex=regex,
                before_id=before_id,
                after_id=after_id,
                limit=limit,
                timeout_ms=request.app.state.search_timeout_ms,
            )
    return RecordsPage(
        records=[RecordView(**r) for r in records],
        has_more_before=has_more_before,
        next_after_id=next_after_id,
    )


@delete_router.delete("/sessions/{session_id}", status_code=204)
async def remove_session(request: Request, session_id: uuid.UUID) -> Response:
    async with request.app.state.pool.connection() as conn:
        if not await delete_session(conn, session_id):
            raise _not_found()
    return Response(status_code=204)


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "no such session")
