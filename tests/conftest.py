import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import httpx
import psycopg
import pytest
import pytest_asyncio
from agents_common.db import open_pool
from fastapi import FastAPI
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from agents_history.api.app import create_app, create_ingest_app, create_service_app
from agents_history.settings import Settings

PACKAGE = "agents_history"
SERVICE_TOKEN = "t"
INGEST_TOKEN = "i"
UI_PEER = "127.0.0.1"
POD_PEER = "10.9.9.9"


@pytest.fixture
def db_url(migrated_db: Callable[[str], str]) -> str:
    """A fresh database cloned from the migrated template."""
    return migrated_db(PACKAGE)


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        yield connection


@pytest_asyncio.fixture
async def aconn(db_url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as connection:
        yield connection


@pytest_asyncio.fixture
async def pool(db_url: str) -> AsyncIterator[AsyncConnectionPool]:
    opened = await open_pool(db_url, min_size=1, max_size=4)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
def empty_db_url(pg_url: str) -> Iterator[str]:
    """A fresh database with no schema at all."""
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        yield f"{pg_url.rsplit('/', 1)[0]}/{name}"
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def settings_for() -> Callable[..., Settings]:
    """Settings for a database url; keyword overrides replace any field."""

    def make(url: str, **overrides: object) -> Settings:
        return Settings(database_url=url, **overrides)

    return make


@pytest.fixture
def settings(settings_for: Callable[..., Settings]) -> Settings:
    """Settings that need no database."""
    return settings_for("postgresql://u:p@db.invalid/history")


def asgi_client(app: FastAPI, peer: str = UI_PEER) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(peer, 50000)), base_url="http://history"
    )


@pytest_asyncio.fixture
async def ui_app(settings: Settings, pool: AsyncConnectionPool) -> FastAPI:
    """The UI app over the migrated database."""
    return create_app(settings, pool)


@pytest_asyncio.fixture
async def service_app(settings: Settings, pool: AsyncConnectionPool) -> FastAPI:
    """The service app over the migrated database; the bearer token is `t`."""
    return create_service_app(settings, pool, [SERVICE_TOKEN])


@pytest_asyncio.fixture
async def ingest_app(settings: Settings, pool: AsyncConnectionPool) -> FastAPI:
    """The ingest app over the migrated database; the bearer token is `i`."""
    return create_ingest_app(settings, pool, [INGEST_TOKEN])


@pytest_asyncio.fixture
async def ui_client(ui_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The UI app called from the allowed peer 127.0.0.1."""
    async with asgi_client(ui_app) as http:
        yield http


@pytest_asyncio.fixture
async def service_client(service_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The service app called from a pod address with the service bearer token."""
    async with asgi_client(service_app, peer=POD_PEER) as http:
        http.headers["Authorization"] = f"Bearer {SERVICE_TOKEN}"
        yield http


@pytest_asyncio.fixture
async def ingest_client(ingest_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The ingest app called from a pod address with the ingest bearer token."""
    async with asgi_client(ingest_app, peer=POD_PEER) as http:
        http.headers["Authorization"] = f"Bearer {INGEST_TOKEN}"
        yield http


class DownPool:
    """A pool stub for the apps whose tests need no database."""

    def connection(self):
        raise RuntimeError("database is down")
