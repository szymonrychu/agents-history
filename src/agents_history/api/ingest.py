"""POST /history/v1/ingest: the orchestrator's discard export, streamed into the database.

Mounted on the ingest app only. The body and the bearer token are never logged.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request

from agents_history.api.errors import ApiError
from agents_history.api.schemas import IngestOut
from agents_history.db.sessions import replace_session
from agents_history.export import ExportError, Line, Record, iter_lines, parse_line, session_head

log = logging.getLogger(__name__)

router = APIRouter()

# Mounted under PREFIX ("/history/v1") by create_ingest_app.
INGEST_ROUTES = [("POST", "/history/v1/ingest")]


def session_id_header(value: str | None) -> uuid.UUID:
    """The X-Orch-Session-Id header as a uuid, or 422 invalid_request."""
    try:
        return uuid.UUID(value or "")
    except ValueError:
        raise ApiError(
            422, "invalid_request", "X-Orch-Session-Id: a session uuid is required"
        ) from None


async def _records(lines: AsyncIterator[Line]) -> AsyncIterator[Record]:
    async for line in lines:
        yield parse_line(line)


@router.post("/ingest", status_code=201)
async def ingest(request: Request) -> IngestOut:
    started = time.monotonic()
    session_id = session_id_header(request.headers.get("x-orch-session-id"))
    lines = iter_lines(request.stream(), request.app.state.settings.ingest_max_line_bytes)
    try:
        first = await anext(lines, None)
        if first is None:
            raise ApiError(422, "empty_export", "the export holds no lines")
        head = session_head(parse_line(first), session_id)
        async with request.app.state.pool.connection() as conn:
            result = await replace_session(conn, head, _records(lines))
    except ExportError as exc:
        log.warning("ingest of session %s refused: %s", session_id, exc.code)
        raise ApiError(422, exc.code, exc.message) from None
    log.info(
        "ingested session %s: %d records, %d truncated, replaced %s, %.0f ms",
        session_id,
        result.records,
        result.truncated,
        result.replaced,
        (time.monotonic() - started) * 1000,
    )
    return IngestOut(**vars(result))
