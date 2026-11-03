"""Schema checks; they need Postgres (testcontainers), so CI is their evidence."""

import uuid

import psycopg
import pytest

from agents_history.db.migrate import migrate

CREATED = "2026-11-01T10:00:00Z"


def add_session(conn, slug="alpha") -> int:
    return conn.execute(
        "INSERT INTO sessions (session_id, slug, created_at, session) "
        "VALUES (%s, %s, %s, '{}') RETURNING id",
        (uuid.uuid4(), slug, CREATED),
    ).fetchone()[0]


def add_record(conn, session_pk: int, kind="message") -> None:
    conn.execute(
        "INSERT INTO records (session_pk, kind, body, text) VALUES (%s, %s, '{}', 'hello')",
        (session_pk, kind),
    )


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_migrate_applies_once(empty_db_url, settings_for):
    settings = settings_for(empty_db_url)
    assert migrate(settings) == ["0001_init.sql", "0002_content_hash.sql"]
    assert migrate(settings) == []


def test_schema(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    }
    assert {"sessions", "records", "settings"} <= tables
    extension = conn.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
    assert extension.fetchone() == ("pg_trgm",)
    row = conn.execute("SELECT id, retention_days FROM settings").fetchall()
    assert row == [(1, 90)]


def test_kind_check(conn):
    pk = add_session(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_record(conn, pk, kind="Bad-Kind")


def test_cascade(conn):
    pk = add_session(conn)
    other = add_session(conn, slug="beta")
    add_record(conn, pk)
    add_record(conn, pk, kind="tool_use")
    add_record(conn, other)
    conn.execute("DELETE FROM sessions WHERE id = %s", (pk,))
    assert count(conn, "records") == 1


def test_settings_row_is_single_and_bounded(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO settings (id, retention_days) VALUES (2, 90)")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE settings SET retention_days = 0")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE settings SET retention_days = 3651")


def test_session_constraints(conn):
    add_session(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_session(conn, slug="")
    with pytest.raises(psycopg.errors.CheckViolation):
        add_session(conn, slug="x" * 129)
    fixed = uuid.uuid4()
    insert = (
        "INSERT INTO sessions (session_id, slug, created_at, session) VALUES (%s, 's', %s, '{}')"
    )
    conn.execute(insert, (fixed, CREATED))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (fixed, CREATED))
