import os

import pytest
from agents_common.settings import SettingsError

from agents_history.settings import Settings, load_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("HISTORY_"):
            monkeypatch.delenv(key)


def problems(monkeypatch, **env) -> dict[str, str]:
    monkeypatch.setenv("HISTORY_DATABASE_URL", "postgresql://x/y")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SettingsError) as exc:
        load_settings()
    return exc.value.problems


def test_defaults(monkeypatch):
    monkeypatch.setenv("HISTORY_DATABASE_URL", "postgresql://x/y")
    s = load_settings()
    assert isinstance(s, Settings)
    assert (s.http_port, s.service_port, s.ingest_port) == (8000, 8001, 8002)
    assert (s.search_timeout_seconds, s.sweep_interval_seconds, s.sweep_batch) == (8, 3600, 50)
    assert s.ingest_max_line_bytes == 67108864
    assert not hasattr(s, "retention_days")  # retention lives in the settings row
    assert s.service_tokens_file is None and s.ingest_tokens_file is None
    assert s.allowed_cidrs == ["127.0.0.1/32"]
    assert "postgresql://x/y" not in repr(s)


def test_missing_database_url_names_variable(monkeypatch):
    with pytest.raises(SettingsError) as exc:
        load_settings()
    assert "HISTORY_DATABASE_URL" in exc.value.problems


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("HISTORY_SEARCH_TIMEOUT_SECONDS", "0"),
        ("HISTORY_SEARCH_TIMEOUT_SECONDS", "61"),
        ("HISTORY_SWEEP_INTERVAL_SECONDS", "59"),
        ("HISTORY_SWEEP_INTERVAL_SECONDS", "86401"),
        ("HISTORY_SWEEP_BATCH", "0"),
        ("HISTORY_SWEEP_BATCH", "1001"),
        ("HISTORY_INGEST_MAX_LINE_BYTES", "1048575"),
        ("HISTORY_INGEST_MAX_LINE_BYTES", "268435457"),
    ],
)
def test_bounds(monkeypatch, var, value):
    assert var in problems(monkeypatch, **{var: value})


def test_bad_cidr_refused(monkeypatch):
    assert "HISTORY_ALLOWED_CIDRS" in problems(monkeypatch, HISTORY_ALLOWED_CIDRS="nope")


def test_cidr_list_accepted(monkeypatch):
    monkeypatch.setenv("HISTORY_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("HISTORY_ALLOWED_CIDRS", "10.0.0.0/8,127.0.0.1/32")
    s = load_settings()
    assert s.allowed_cidrs == ["10.0.0.0/8", "127.0.0.1/32"]
    assert [str(n) for n in s.api_networks()] == ["10.0.0.0/8", "127.0.0.1/32"]


@pytest.mark.parametrize(
    ("env", "variable"),
    [
        (
            {"HISTORY_SERVICE_TOKENS_FILE": "/t", "HISTORY_SERVICE_PORT": "8000"},
            "HISTORY_SERVICE_PORT",
        ),
        (
            {
                "HISTORY_SERVICE_TOKENS_FILE": "/t",
                "HISTORY_INGEST_TOKENS_FILE": "/i",
                "HISTORY_INGEST_PORT": "8001",
            },
            "HISTORY_INGEST_PORT",
        ),
        (
            {"HISTORY_INGEST_TOKENS_FILE": "/i", "HISTORY_INGEST_PORT": "8000"},
            "HISTORY_INGEST_PORT",
        ),
    ],
)
def test_ports_must_differ(monkeypatch, env, variable):
    assert variable in problems(monkeypatch, **env)


def test_disabled_listener_port_is_not_checked(monkeypatch):
    monkeypatch.setenv("HISTORY_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("HISTORY_SERVICE_PORT", "8000")
    assert load_settings().service_port == 8000
