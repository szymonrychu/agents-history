"""POST /history/v1/ingest: the orchestrator's discard export, streamed into the database.

Mounted on the ingest app only. The body and the bearer token are never logged.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response

from agents_history.api.errors import ApiError
from agents_history.api.schemas import IngestOut
from agents_history.db.sessions import IngestInProgress, replace_session
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


SPOOL_MEMORY = 8 * 1024 * 1024
READ_CHUNK = 1024 * 1024


async def _spooled(request: Request, spool: tempfile.SpooledTemporaryFile) -> str:
    """Copy the request body into `spool` (rewound) and return its sha256 hex digest. Reading
    the whole body first costs network time only; the database sees it afterwards, and only
    when the digest differs from the stored copy."""
    digest = hashlib.sha256()
    async for chunk in request.stream():
        digest.update(chunk)
        spool.write(chunk)
    spool.seek(0)
    return digest.hexdigest()


async def _chunks(spool: tempfile.SpooledTemporaryFile) -> AsyncIterator[bytes]:
    while chunk := spool.read(READ_CHUNK):
        yield chunk


@router.post("/ingest", status_code=201)
async def ingest(request: Request, response: Response) -> IngestOut:
    """201 for a stored or replaced session, 200 when the stored copy came from a byte-identical
    body (nothing is written), 409 `ingest_in_progress` while another ingest of the session runs."""
    started = time.monotonic()
    session_id = session_id_header(request.headers.get("x-orch-session-id"))
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY) as spool:
        sha256 = await _spooled(request, spool)
        lines = iter_lines(_chunks(spool), request.app.state.settings.ingest_max_line_bytes)
        try:
            first = await anext(lines, None)
            if first is None:
                raise ApiError(422, "empty_export", "the export holds no lines")
            head = session_head(parse_line(first), session_id)
            async with request.app.state.pool.connection() as conn:
                result = await replace_session(conn, head, _records(lines), sha256)
        except ExportError as exc:
            log.warning("ingest of session %s refused: %s", session_id, exc.code)
            raise ApiError(422, exc.code, exc.message) from None
        except IngestInProgress:
            log.warning("ingest of session %s refused: another ingest is running", session_id)
            raise ApiError(
                409,
                "ingest_in_progress",
                "another ingest of this session is running: retry later",
            ) from None
    if result.unchanged:
        response.status_code = 200
    log.info(
        "ingested session %s: %d records, %d truncated, replaced %s, unchanged %s, %.0f ms",
        session_id,
        result.records,
        result.truncated,
        result.replaced,
        result.unchanged,
        (time.monotonic() - started) * 1000,
    )
    return IngestOut(
        session_id=result.session_id,
        records=result.records,
        truncated=result.truncated,
        replaced=result.replaced,
    )
