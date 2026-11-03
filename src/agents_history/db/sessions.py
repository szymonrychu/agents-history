"""Writes of the `sessions` and `records` tables (the api process is their only writer)."""

from __future__ import annotations

import math
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agents_history.export import Record, SessionHead, clean_str

COPY_RECORDS = "COPY records (session_pk, kind, ts, body, text, truncated) FROM STDIN"


@dataclass(frozen=True)
class IngestResult:
    session_id: uuid.UUID
    records: int
    truncated: int
    replaced: bool
    unchanged: bool = False


class IngestInProgress(Exception):
    """Another ingest of the same session holds its lock: do not start a second rewrite."""


def jsonb_safe(value: Any) -> Any:
    """A copy of a parsed JSON value that Postgres jsonb accepts: every string and key through
    `clean_str` (no NUL, no lone surrogate) and NaN or Infinity as null. Iterative, so any
    depth `json.loads` produced is fine."""
    holder = [value]
    pending: list[tuple[Any, Any]] = [(holder, 0)]
    while pending:
        parent, key = pending.pop()
        node = parent[key]
        if isinstance(node, dict):
            copy = {clean_str(k): v for k, v in node.items()}
            parent[key] = copy
            pending.extend((copy, k) for k in copy)
        elif isinstance(node, list):
            copy = list(node)
            parent[key] = copy
            pending.extend((copy, i) for i in range(len(copy)))
        elif isinstance(node, str):
            parent[key] = clean_str(node)
        elif isinstance(node, float) and not math.isfinite(node):
            parent[key] = None
    return holder[0]


def _optional(value: str | None) -> str | None:
    return None if value is None else clean_str(value)


async def replace_session(
    conn: psycopg.AsyncConnection,
    head: SessionHead,
    records: AsyncIterator[Record],
    content_sha256: str | None = None,
) -> IngestResult:
    """Store one export in one transaction: drop any stored copy of the session, insert the
    session row, COPY every record, then set the per-kind counts and the byte total (every
    line, the session line included). Any exception, an ExportError from `records` included,
    rolls the whole transaction back, so a previous copy stays intact.

    Idempotent: the call takes a transaction-scoped advisory lock keyed on the session id without
    waiting and raises IngestInProgress when another ingest of the session holds it. When the
    stored copy has the same `content_sha256` (not None) nothing is written and the result has
    `unchanged` True, `replaced` False; `records` is not consumed."""
    counts: Counter[str] = Counter()
    truncated = 0
    size = head.size
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (str(head.session_id),)
        )
        locked = await cur.fetchone()
        if not (locked and locked[0]):
            raise IngestInProgress(str(head.session_id))
        if content_sha256 is not None:
            cur = await conn.execute(
                "SELECT s.record_counts, s.content_sha256 = %s,"
                " (SELECT count(*) FROM records r WHERE r.session_pk = s.id AND r.truncated)"
                " FROM sessions s WHERE s.session_id = %s",
                (content_sha256, head.session_id),
            )
            stored = await cur.fetchone()
            if stored and stored[1]:
                return IngestResult(
                    head.session_id, sum(stored[0].values()), stored[2], False, True
                )
        deleted = await conn.execute(
            "DELETE FROM sessions WHERE session_id = %s", (head.session_id,)
        )
        cur = await conn.execute(
            "INSERT INTO sessions (session_id, slug, preset_id, preset_name, created_by_service,"
            " created_by_session, created_at, session,"
            " content_sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                head.session_id,
                clean_str(head.slug),
                head.preset_id,
                _optional(head.preset_name),
                _optional(head.created_by_service),
                head.created_by_session,
                head.created_at,
                Jsonb(jsonb_safe(head.raw)),
                content_sha256,
            ),
        )
        row = await cur.fetchone()
        assert row is not None
        session_pk = row[0]
        async with conn.cursor() as copy_cur, copy_cur.copy(COPY_RECORDS) as copy:
            async for record in records:
                await copy.write_row(
                    (
                        session_pk,
                        record.kind,
                        record.ts,
                        Jsonb(jsonb_safe(record.body)),
                        record.text,
                        record.truncated,
                    )
                )
                counts[record.kind] += 1
                truncated += record.truncated
                size += record.size
        await conn.execute(
            "UPDATE sessions SET record_counts = %s, bytes = %s WHERE id = %s",
            (Jsonb(dict(counts)), size, session_pk),
        )
    return IngestResult(head.session_id, counts.total(), truncated, deleted.rowcount > 0)


async def delete_session(conn: psycopg.AsyncConnection, session_id: uuid.UUID) -> bool:
    """Delete one stored session (its records cascade); False when it is not stored."""
    cur = await conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
    return cur.rowcount > 0
