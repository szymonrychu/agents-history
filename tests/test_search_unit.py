"""Pure helpers of search and the read routes per listener (no database)."""

from conftest import DownPool

from agents_history.api.app import PREFIX, create_app, create_ingest_app, create_service_app
from agents_history.db.read import escape_like, snippet


def test_escape_like():
    assert escape_like("50%_a\\b") == "50\\%\\_a\\\\b"


def span(text: str, needle: str) -> tuple[int, int]:
    start = text.index(needle)
    return start, start + len(needle)


def test_snippet_centres_match():
    text = "a" * 1000 + "NEEDLE" + "b" * 1000
    s = snippet(text, *span(text, "NEEDLE"))
    assert "NEEDLE" in s
    assert len(s) <= 166
    assert s.startswith("...") and s.endswith("...")


def test_snippet_near_the_start_and_end_has_one_marker():
    text = "NEEDLE" + "b" * 500
    assert snippet(text, *span(text, "NEEDLE")) == "NEEDLE" + "b" * 154 + "..."
    text = "a" * 500 + "NEEDLE"
    assert snippet(text, *span(text, "NEEDLE")) == "..." + "a" * 154 + "NEEDLE"


def test_snippet_short_text_is_whole():
    text = "Deploy FAILED today"
    assert snippet(text, *span(text, "FAILED")) == text


def test_snippet_window_cut_flags_add_markers():
    assert snippet("xxNEEDLExx", 2, 8, cut_left=True, cut_right=True) == "...xxNEEDLExx..."


def test_snippet_match_longer_than_the_window_is_clamped():
    s = snippet("a" * 100, 10, 5000, width=40)
    assert 0 < len(s) <= 46


def test_snippet_without_a_match_is_the_start():
    """Replaces test_snippet_regex_fallback: the user's pattern no longer runs in Python, the
    database gives no span when it found none, and the snippet is the first 160 characters."""
    assert snippet("z" * 500, None, None) == "z" * 160


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
