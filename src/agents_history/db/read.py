"""Reads of the `sessions` and `records` tables: filtered session pages with search hits,
the stored session, its agents and its record pages. Read-only; writes live in `sessions.py`.

Every query with a search term runs inside a transaction that first sets `statement_timeout`
(`set_config(..., true)` is `SET LOCAL`), so a runaway pattern ends as `QueryCanceled`.
"""

from __future__ import annotations

import re
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

SNIPPET_WIDTH = 160
MATCHES_PER_SESSION = 3

_SUMMARY = (
    "s.id, s.session_id, s.slug, s.preset_id, s.preset_name, s.created_by_service,"
    " s.created_by_session, s.created_at, s.discarded_at, s.record_counts, s.bytes"
)


def escape_like(q: str) -> str:
    """`q` with the LIKE metacharacters `\\`, `%` and `_` escaped by a backslash."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def snippet(text: str, q: str, regex: bool, width: int = SNIPPET_WIDTH) -> str:
    """At most `width` characters of `text` centred on the first case-insensitive match of `q`,
    with "..." where the text was cut. A pattern Python cannot compile (Postgres regex syntax is
    wider) or that finds nothing gives the first `width` characters."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)  # Postgres sets like [[:alpha:]]
            found = re.search(q if regex else re.escape(q), text, re.IGNORECASE)
    except re.error:
        found = None
    if found is None:
        return text[:width]
    start = max(0, (found.start() + found.end()) // 2 - width // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)
    return ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")


@dataclass
class SessionFilter:
    q: str | None = None
    regex: bool = False
    from_: datetime | None = None
    to: datetime | None = None
    preset: str | None = None
    session: str | None = None
    limit: int = 50
    before_id: int | None = None


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _begin_search(conn: psycopg.AsyncConnection, timeout_ms: int, q: str, regex: bool):
    """Inside the caller's transaction: the statement timeout and, for a regex, an up-front
    compile of the pattern (Postgres compiles it only when it meets a row, so an empty table
    would otherwise accept a broken pattern)."""
    await conn.execute("SELECT set_config('statement_timeout', %s, true)", (str(timeout_ms),))
    if regex:
        await conn.execute("SELECT '' ~* %s", (q,))


def _text_match(column: str, regex: bool) -> str:
    return f"{column} ~* %(rx)s" if regex else f"{column} ILIKE %(like)s"


def _search_params(q: str, regex: bool) -> dict[str, Any]:
    return {"q": q, "rx": q, "like": f"%{escape_like(q)}%", "w": SNIPPET_WIDTH}


def _position(regex: bool) -> str:
    """SQL for the 1-based position of the first match in `r.text` (0 when unknown)."""
    if regex:
        return "regexp_instr(r.text, %(rx)s, 1, 1, 0, 'i')"
    return "strpos(lower(r.text), lower(%(q)s))"


async def list_sessions(
    conn: psycopg.AsyncConnection, f: SessionFilter, timeout_ms: int
) -> list[dict]:
    """Sessions newest id first. Every filter, the `q` match included, narrows the rows before
    the keyset page is cut, so a page is never emptied by a later filter. With `q` each row has
    `hits` (matching records) and up to three `matches` `{record_id, kind, ts, text}`, `text`
    being a window of the record text around the match; without it `hits` is None."""
    where: list[str] = []
    params: dict[str, Any] = {"limit": f.limit}
    if f.before_id is not None:
        where.append("s.id < %(before_id)s")
        params["before_id"] = f.before_id
    if f.from_ is not None:
        where.append("s.discarded_at >= %(from)s")
        params["from"] = f.from_
    if f.to is not None:
        where.append("s.created_at < %(to)s")
        params["to"] = f.to
    if f.preset is not None:
        preset_id = _as_uuid(f.preset)
        where.append("(s.preset_name = %(preset)s OR s.preset_id = %(preset_id)s)")
        params.update(preset=f.preset, preset_id=preset_id)
    if f.session is not None:
        session_id = _as_uuid(f.session)
        where.append("(s.session_id = %(session_id)s OR s.slug ILIKE %(slug)s)")
        params.update(session_id=session_id, slug=f"%{escape_like(f.session)}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    async with conn.transaction():
        if f.q is None:
            query = f"SELECT {_SUMMARY} FROM sessions s {clause} ORDER BY s.id DESC LIMIT %(limit)s"
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params)
                return [{**row, "hits": None, "matches": []} for row in await cur.fetchall()]
        await _begin_search(conn, timeout_ms, f.q, f.regex)
        params.update(_search_params(f.q, f.regex))
        query = f"""
            WITH s AS (SELECT {_SUMMARY} FROM sessions s {clause}),
            h AS (
                SELECT r.session_pk, count(*) AS hits FROM records r
                JOIN s ON s.id = r.session_pk
                WHERE {_text_match("r.text", f.regex)} GROUP BY r.session_pk
            ),
            page AS (
                SELECT s.*, h.hits FROM s JOIN h ON h.session_pk = s.id
                ORDER BY s.id DESC LIMIT %(limit)s
            )
            SELECT page.*, m.record_id, m.kind AS match_kind, m.ts AS match_ts, m.text AS match_text
            FROM page LEFT JOIN LATERAL (
                SELECT x.id AS record_id, x.kind, x.ts,
                    CASE WHEN x.pos > 0 THEN substr(x.text, greatest(x.pos - %(w)s, 1), %(w)s * 3)
                    ELSE left(x.text, %(w)s * 3) END AS text
                FROM (
                    SELECT r.id, r.kind, r.ts, r.text, {_position(f.regex)} AS pos
                    FROM records r
                    WHERE r.session_pk = page.id AND {_text_match("r.text", f.regex)}
                    ORDER BY r.id LIMIT {MATCHES_PER_SESSION}
                ) x
            ) m ON true
            ORDER BY page.id DESC, m.record_id
        """
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()
    sessions: dict[int, dict] = {}
    for row in rows:
        record_id = row.pop("record_id")
        match = {
            "record_id": record_id,
            "kind": row.pop("match_kind"),
            "ts": row.pop("match_ts"),
            "text": row.pop("match_text"),
        }
        session = sessions.setdefault(row["id"], {**row, "matches": []})
        if record_id is not None:
            session["matches"].append(match)
    return list(sessions.values())


async def get_session(conn: psycopg.AsyncConnection, session_id: uuid.UUID) -> dict | None:
    """The summary columns plus the stored `session` record of one session, or None."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_SUMMARY}, s.session FROM sessions s WHERE s.session_id = %s", (session_id,)
        )
        row = await cur.fetchone()
    return None if row is None else {**row, "hits": None, "matches": []}


async def agents_of(conn: psycopg.AsyncConnection, session_pk: int) -> list[dict]:
    """`{id, ordinal, pod_name}` of each stored `agent` record, in record order; an agent whose
    id or ordinal is not an integer is left out."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT body -> 'id', body -> 'ordinal', body ->> 'pod_name' FROM records"
            " WHERE session_pk = %s AND kind = 'agent' ORDER BY id",
            (session_pk,),
        )
        rows = await cur.fetchall()
    return [
        {"id": agent_id, "ordinal": ordinal, "pod_name": pod_name}
        for agent_id, ordinal, pod_name in rows
        if all(type(v) is int for v in (agent_id, ordinal))
    ]


async def records_page(
    conn: psycopg.AsyncConnection,
    session_pk: int,
    *,
    kinds: list[str] | None,
    q: str | None,
    regex: bool,
    before_id: int | None,
    after_id: int | None,
    limit: int,
    timeout_ms: int,
) -> tuple[list[dict], bool, int | None]:
    """One page of a session's records in ascending id order: `(records, has_more_before,
    next_after_id)`. No cursor gives the newest `limit`; `after_id` (0 = from the start) the
    next `limit` with `next_after_id` set when more follow; `before_id` the `limit` before it."""
    where = ["r.session_pk = %(pk)s"]
    params: dict[str, Any] = {"pk": session_pk, "limit": limit + 1}
    if kinds:
        where.append("r.kind = ANY(%(kinds)s)")
        params["kinds"] = kinds
    if q is not None:
        where.append(_text_match("r.text", regex))
        params.update(_search_params(q, regex))
    base = " AND ".join(where)
    columns = "r.id, r.kind, r.ts, r.truncated, r.body"
    async with conn.transaction():
        if q is not None:
            await _begin_search(conn, timeout_ms, q, regex)
        async with conn.cursor(row_factory=dict_row) as cur:
            if after_id is not None:
                await cur.execute(
                    f"SELECT {columns} FROM records r WHERE {base} AND r.id > %(cursor)s"
                    " ORDER BY r.id LIMIT %(limit)s",
                    {**params, "cursor": after_id},
                )
                rows = await cur.fetchall()
                more = len(rows) > limit
                page = rows[:limit]
                before = False
                if after_id > 0:
                    await cur.execute(
                        f"SELECT 1 FROM records r WHERE {base} AND r.id <= %(cursor)s LIMIT 1",
                        {**params, "cursor": after_id},
                    )
                    before = await cur.fetchone() is not None
                return page, before, page[-1]["id"] if more else None
            upper = "" if before_id is None else " AND r.id < %(cursor)s"
            await cur.execute(
                f"SELECT {columns} FROM records r WHERE {base}{upper}"
                " ORDER BY r.id DESC LIMIT %(limit)s",
                {**params, "cursor": before_id},
            )
            rows = await cur.fetchall()
    return rows[:limit][::-1], len(rows) > limit, None
