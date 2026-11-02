"""The orchestrator's JSONL session export, parsed line by line.

Pure functions and one async generator, no I/O of their own: the ingest route feeds the request
body chunks in and writes the parsed records out.
"""

import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

TRUNCATED_TEXT_BYTES = 1048576

_KIND = re.compile(r"^[a-z_]{1,32}$")
_PREFIX_KIND = re.compile(rb'"kind"\s*:\s*"([a-z_]{1,32})"')
_PREFIX_ID = re.compile(rb'"id"\s*:\s*(-?\d+|"[^"\\]*")')
_TS_KEYS = ("ts", "created_at", "started_at")
_SURROGATE = re.compile("[\\ud800-\\udfff]")


class ExportError(Exception):
    """A refusable export: `bad_line` (one line is unusable) or `bad_export` (the session
    record does not match the request)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Line:
    """One non-empty export line. `size` is the full byte length without the newline; `data`
    is the whole line, or its first TRUNCATED_TEXT_BYTES when `over`."""

    data: bytes
    size: int
    over: bool
    number: int


@dataclass(frozen=True)
class Record:
    kind: str
    ts: datetime | None
    body: dict[str, Any]
    text: str
    truncated: bool
    size: int


@dataclass(frozen=True)
class SessionHead:
    session_id: uuid.UUID
    slug: str
    preset_id: uuid.UUID | None
    preset_name: str | None
    created_by_service: str | None
    created_by_session: uuid.UUID | None
    created_at: datetime
    raw: dict[str, Any]
    size: int


async def iter_lines(chunks: AsyncIterator[bytes], max_line_bytes: int) -> AsyncIterator[Line]:
    """Split the chunks on newlines. Empty lines are skipped and not counted. A line longer
    than `max_line_bytes` is yielded `over`, with only its first TRUNCATED_TEXT_BYTES kept, so
    at most `max_line_bytes` bytes of one line are ever buffered."""
    buffer = bytearray()
    size = 0
    number = 0

    def take(piece: bytes | memoryview) -> None:
        nonlocal size
        size += len(piece)
        room = max_line_bytes - len(buffer)
        if room > 0:
            buffer.extend(piece[:room])

    def finish() -> Line | None:
        nonlocal size, number
        if size == 0:
            return None
        number += 1
        over = size > max_line_bytes
        done = Line(
            bytes(buffer[:TRUNCATED_TEXT_BYTES]) if over else bytes(buffer), size, over, number
        )
        buffer.clear()
        size = 0
        return done

    async for chunk in chunks:
        view = memoryview(chunk)
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            if newline < 0:
                take(view[start:])
                break
            take(view[start:newline])
            start = newline + 1
            if (done := finish()) is not None:
                yield done
    if (done := finish()) is not None:
        yield done


def clean_str(value: str) -> str:
    """`value` without NUL and with every lone surrogate (a JSON "\\ud800" escape) replaced by
    U+FFFD: Postgres text and jsonb can hold neither."""
    return _SURROGATE.sub("\ufffd", value.replace("\x00", ""))


def record_text(body: Any) -> str:
    """Every string leaf (keys excluded) in document order, joined by newlines, each through
    `clean_str`."""
    leaves: list[str] = []
    pending = [body]
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            leaves.append(clean_str(node))
        elif isinstance(node, dict):
            pending.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            pending.extend(reversed(node))
    return "\n".join(leaves)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def record_ts(body: dict[str, Any]) -> datetime | None:
    """The first of ts, created_at, started_at that is an ISO 8601 time with a zone."""
    for key in _TS_KEYS:
        if (parsed := _aware(body.get(key))) is not None:
            return parsed
    return None


def _prefix_id(data: bytes) -> int | str | None:
    """The first `"id": <int or string>` in the prefix, best-effort: a nested id that comes
    first wins, and one that does not decode (over 4300 digits, a control character, invalid
    UTF-8) is None, so the truncated record is still stored."""
    ident = _PREFIX_ID.search(data)
    if ident is None:
        return None
    try:
        return json.loads(ident.group(1))
    except ValueError:
        return None


def _over_record(line: Line) -> Record:
    """The truncated record of an over line; its text is the prefix decoded with bad bytes
    replaced and NUL removed."""
    kind = _PREFIX_KIND.search(line.data)
    k = kind.group(1).decode() if kind else "unknown"
    i = _prefix_id(line.data)
    body = {"kind": k, "id": i, "truncated": True, "bytes": line.size}
    text = clean_str(line.data.decode("utf-8", errors="replace"))
    return Record(k, None, body, text, True, line.size)


def parse_line(line: Line) -> Record:
    if line.over:
        return _over_record(line)
    try:
        body = json.loads(line.data)
    except (ValueError, RecursionError):
        raise ExportError("bad_line", f"line {line.number}: not valid JSON") from None
    if not isinstance(body, dict):
        raise ExportError("bad_line", f"line {line.number}: not a JSON object")
    kind = body.get("kind")
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise ExportError("bad_line", f"line {line.number}: missing or invalid kind")
    return Record(kind, record_ts(body), body, record_text(body), False, line.size)


def _uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def session_head(record: Record, header_id: uuid.UUID) -> SessionHead:
    """The first record must be this request's session. The optional fields that do not parse
    become None: a refused export would block the discard forever."""
    body = record.body
    if record.kind != "session":
        raise ExportError("bad_export", f"the first record is kind {record.kind}, not session")
    if _uuid(body.get("id")) != header_id:
        raise ExportError("bad_export", "the session record id differs from X-Orch-Session-Id")
    slug = body.get("slug")
    if not isinstance(slug, str) or not slug:
        raise ExportError("bad_export", "the session record has no slug")
    created_at = _aware(body.get("created_at"))
    if created_at is None:
        raise ExportError("bad_export", "the session record has no valid created_at")
    return SessionHead(
        session_id=header_id,
        slug=slug,
        preset_id=_uuid(body.get("preset_id")),
        preset_name=_optional_str(body.get("preset_name")),
        created_by_service=_optional_str(body.get("created_by_service")),
        created_by_session=_uuid(body.get("created_by_session")),
        created_at=created_at,
        raw=body,
        size=record.size,
    )
