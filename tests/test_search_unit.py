"""Pure helpers of search and the read routes per listener (no database)."""

from conftest import DownPool

from agents_history.api.app import PREFIX, create_app, create_ingest_app, create_service_app
from agents_history.db.read import escape_like, snippet


def test_escape_like():
    assert escape_like("50%_a\\b") == "50\\%\\_a\\\\b"


def test_snippet_centres_match():
    text = "a" * 1000 + "NEEDLE" + "b" * 1000
    s = snippet(text, "needle", False)
    assert "NEEDLE" in s
    assert len(s) <= 166
    assert s.startswith("...") and s.endswith("...")


def test_snippet_near_the_start_and_end_has_one_marker():
    assert snippet("NEEDLE" + "b" * 500, "needle", False) == "NEEDLE" + "b" * 154 + "..."
    assert snippet("a" * 500 + "NEEDLE", "needle", False) == "..." + "a" * 154 + "NEEDLE"


def test_snippet_short_text_is_whole():
    assert snippet("Deploy FAILED today", "deploy failed", False) == "Deploy FAILED today"


def test_snippet_substring_mode_is_literal():
    assert snippet("x" * 300 + "a.c" + "y" * 300, "a.c", False).count("a.c") == 1
    assert snippet("abc is here, a.c is not", "a.c", False).startswith("abc is here")


def test_snippet_regex_mode():
    text = "a" * 400 + "failure" + "b" * 400
    assert "failure" in snippet(text, "fail(ed|ure)", True)


def test_snippet_regex_fallback():
    text = "x" * 500
    assert snippet(text, "[[:alpha:]]+(?", True) == text[:160]


def test_snippet_without_a_match_is_the_start():
    assert snippet("z" * 500, "needle", False) == "z" * 160


def paths(app) -> dict[str, set[str]]:
    return {path: set(item) for path, item in app.openapi()["paths"].items()}


def test_read_routes_per_listener(settings):
    read_routes = {
        f"{PREFIX}/sessions": {"get"},
        f"{PREFIX}/sessions/{{session_id}}": {"get"},
        f"{PREFIX}/sessions/{{session_id}}/records": {"get"},
    }
    service = paths(create_service_app(settings, DownPool(), ["t"]))
    ui = paths(create_app(settings, DownPool()))
    ingest = paths(create_ingest_app(settings, DownPool(), ["i"]))
    assert {p: m for p, m in service.items() if p.startswith(PREFIX)} == read_routes
    assert {p: m for p, m in ui.items() if p.startswith(PREFIX)} == {
        **read_routes,
        f"{PREFIX}/sessions/{{session_id}}": {"get", "delete"},
    }
    assert not any(p.startswith(f"{PREFIX}/sessions") for p in ingest)
