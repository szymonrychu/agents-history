"""The api command as a real process and real listeners.

The tests taking `db_url` or `empty_db_url` need Postgres (testcontainers): CI is their evidence.
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import psycopg
import pytest
from conftest import DownPool

from agents_history import runtime
from agents_history.api.app import create_app


def env_for(url: str | None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HISTORY_")}
    if url is not None:
        env["HISTORY_DATABASE_URL"] = url
    env.update(HISTORY_SHUTDOWN_GRACE_SECONDS="10", PYTHONUNBUFFERED="1", **extra)
    return env


def history(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agents_history.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait(check: Callable[[], object], timeout: float = 30, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def api_process(tmp_path: Path) -> Iterator[Callable[[dict[str, str]], subprocess.Popen]]:
    started: list[tuple[subprocess.Popen, object]] = []

    def start(env: dict[str, str]) -> subprocess.Popen:
        out = (tmp_path / f"api{len(started)}.log").open("w")
        proc = subprocess.Popen(
            [sys.executable, "-m", "agents_history.cli", "api"],
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        started.append((proc, out))
        return proc

    yield start
    for proc, out in started:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        out.close()


def test_help_lists_commands():
    result = history("--help", env=env_for(None))
    assert result.returncode == 0
    for command in ("migrate", "api"):
        assert command in result.stdout


def test_bad_config_exits_78():
    env = env_for(None, HISTORY_SWEEP_BATCH="0")
    for command in ("migrate", "api"):
        result = history(command, env=env)
        assert result.returncode == 78, (command, result.stderr)
        assert "HISTORY_DATABASE_URL" in result.stderr
        assert "HISTORY_SWEEP_BATCH" in result.stderr


def test_same_token_in_both_files_exits_78(tmp_path, settings_for, capsys):
    service = tmp_path / "service"
    ingest = tmp_path / "ingest"
    service.write_text("shared-token\n")
    ingest.write_text("shared-token\n")
    settings = settings_for(
        "postgresql://u:p@db.invalid/history",
        service_tokens_file=str(service),
        ingest_tokens_file=str(ingest),
    )
    assert runtime.api_main(settings) == 78
    err = capsys.readouterr().err
    assert "HISTORY_INGEST_TOKENS_FILE" in err
    assert "shared-token" not in err


def test_empty_tokens_file_exits_78(tmp_path, settings_for, capsys):
    empty = tmp_path / "tokens"
    empty.write_text("\n")
    settings = settings_for("postgresql://u:p@db.invalid/history", service_tokens_file=str(empty))
    assert runtime.api_main(settings) == 78
    assert "HISTORY_SERVICE_TOKENS_FILE" in capsys.readouterr().err


def test_listener_tokens_reads_each_enabled_file(tmp_path, settings_for):
    service = tmp_path / "service"
    ingest = tmp_path / "ingest"
    service.write_text("s1\ns2\n")
    ingest.write_text("i1\n")
    url = "postgresql://u:p@db.invalid/history"
    both = settings_for(url, service_tokens_file=str(service), ingest_tokens_file=str(ingest))
    assert runtime._listener_tokens(both) == {"service": ["s1", "s2"], "ingest": ["i1"]}
    assert runtime._listener_tokens(settings_for(url)) == {}


def test_missing_tokens_file_names_the_variable(tmp_path, settings_for):
    settings = settings_for(
        "postgresql://u:p@db.invalid/history", ingest_tokens_file=str(tmp_path / "absent")
    )
    with pytest.raises(ValueError, match="HISTORY_INGEST_TOKENS_FILE"):
        runtime._listener_tokens(settings)


@pytest.mark.parametrize(
    ("cidrs", "expected"),
    [("127.0.0.1/32", 404), ("203.0.113.9/32", 403)],
)
async def test_api_ignores_forwarded_headers(settings_for, cidrs, expected):
    """Behind the oauth2-proxy sidecar the allowlist sees the socket peer, not X-Forwarded-For:
    a forwarded allowed address neither opens nor closes the door."""
    port = free_port()
    settings = settings_for(
        "postgresql://u:p@db.invalid/history",
        http_host="127.0.0.1",
        http_port=port,
        allowed_cidrs=[cidrs],
    )
    server = runtime._Server(runtime._api_config(settings, create_app(settings, DownPool()), port))
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            forwarded = await http.get(
                "/history/v1/x",
                headers={"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.9"},
            )
        assert forwarded.status_code == expected, forwarded.text
    finally:
        server.should_exit = True
        await serving


def test_migrate_command(empty_db_url: str):
    result = history("migrate", env=env_for(empty_db_url))
    assert result.returncode == 0, result.stderr
    assert "0001_init.sql" in result.stdout
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('public.sessions')::text").fetchone()[0] == (
            "sessions"
        )
    again = history("migrate", env=env_for(empty_db_url))
    assert again.returncode == 0
    assert "0001_init.sql" not in again.stdout


def test_api_serves_on_http_port_and_stops_on_sigterm(db_url: str, api_process):
    port = free_port()
    proc = api_process(env_for(db_url, HISTORY_HTTP_PORT=str(port), HISTORY_HTTP_HOST="127.0.0.1"))

    def healthy() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False

    wait(healthy, what="the api to answer /readyz")
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 0


async def test_token_listeners_serve_behind_their_tokens(db_url: str, settings_for):
    ui_port, service_port, ingest_port = free_port(), free_port(), free_port()
    settings = settings_for(
        db_url,
        http_host="127.0.0.1",
        http_port=ui_port,
        service_port=service_port,
        service_tokens_file="/unused",
        ingest_port=ingest_port,
        ingest_tokens_file="/unused",
    )
    tokens = {"service": ["svc-test-token"], "ingest": ["ingest-test-token"]}
    stop = asyncio.Event()
    serving = asyncio.create_task(runtime._serve(settings, stop, tokens))
    try:
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{ui_port}") as ui,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{service_port}") as service,
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{ingest_port}") as ingest,
        ):
            for http in (ui, service, ingest):
                for _ in range(100):
                    try:
                        if (await http.get("/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail(f"listener {http.base_url} never answered 200 on /healthz")
            svc = {"Authorization": "Bearer svc-test-token"}
            ing = {"Authorization": "Bearer ingest-test-token"}
            assert (await ui.get("/history/v1/x")).status_code == 404
            assert (await service.get("/history/v1/x")).status_code == 401
            assert (await service.get("/history/v1/x", headers=svc)).status_code == 404
            assert (await ingest.get("/history/v1/x", headers=svc)).status_code == 401
            assert (await ingest.get("/history/v1/x", headers=ing)).status_code == 404
            assert (await service.get("/history/v1/x", headers=ing)).status_code == 401
    finally:
        stop.set()
        await serving
