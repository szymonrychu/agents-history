import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from agents_history.export import (
    TRUNCATED_TEXT_BYTES,
    ExportError,
    Line,
    iter_lines,
    parse_line,
    record_text,
    record_ts,
    session_head,
)

SESSION_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")


async def chunked(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def line(data: bytes, number: int = 1) -> Line:
    return Line(data, len(data), False, number)


def session_line(**overrides) -> Line:
    body = {
        "kind": "session",
        "id": str(SESSION_ID),
        "slug": "brisk-otter",
        "created_at": "2026-11-01T10:00:00+00:00",
        "preset_id": "22222222-2222-4222-8222-222222222222",
        "preset_name": "reviewer",
        "created_by_service": "webhook",
        "created_by_session": "33333333-3333-4333-8333-333333333333",
        "spec": {"agents": []},
    }
    body.update(overrides)
    return line(json.dumps(body).encode())


def test_text_collects_string_leaves_without_keys():
    body = {
        "kind": "event",
        "type": "tool_result",
        "data": {"output": "Hello", "n": 3, "items": ["a", {"b": "c"}]},
    }
    assert record_text(body) == "event\ntool_result\nHello\na\nc"


def test_text_removes_nul():
    assert record_text({"x": "a\x00b"}) == "ab"


def test_text_replaces_lone_surrogates():
    body = json.loads('{"x": "a\\ud800b", "y": "\\udc00", "ok": "\\ud83d\\ude00"}')
    text = record_text(body)
    assert text == "a\ufffdb\n\ufffd\n\U0001f600"
    text.encode("utf-8")


def test_parsed_line_text_with_lone_surrogate_encodes():
    record = parse_line(line(b'{"kind":"event","data":"x\\ud800y\\u0000z"}'))
    assert record.text == "event\nx\ufffdyz"
    record.text.encode("utf-8")


def test_text_without_strings_is_empty():
    assert record_text({"n": 1, "ok": True, "none": None, "list": []}) == ""


def test_ts_precedence():
    body = {
        "started_at": "2026-01-03T00:00:00+00:00",
        "created_at": "2026-01-02T00:00:00+00:00",
        "ts": "2026-01-01T00:00:00Z",
    }
    assert record_ts(body) == datetime(2026, 1, 1, tzinfo=UTC)
    del body["ts"]
    assert record_ts(body) == datetime(2026, 1, 2, tzinfo=UTC)
    del body["created_at"]
    assert record_ts(body) == datetime(2026, 1, 3, tzinfo=UTC)


def test_ts_skips_unparseable_and_naive_values():
    assert record_ts({"ts": "garbage"}) is None
    assert record_ts({"ts": "2026-01-01T00:00:00"}) is None
    assert record_ts({"ts": 12345}) is None
    assert record_ts({}) is None
    fallback = {"ts": "garbage", "created_at": "2026-01-02T00:00:00+00:00"}
    assert record_ts(fallback) == datetime(2026, 1, 2, tzinfo=UTC)


async def test_lines_split_across_chunks():
    chunks = chunked(b'{"kind":"a"}\n{"ki', b'nd":"b"}\n\n{"kind":"c"}')
    lines = [item async for item in iter_lines(chunks, 1048576)]
    assert [(item.data, item.number, item.over) for item in lines] == [
        (b'{"kind":"a"}', 1, False),
        (b'{"kind":"b"}', 2, False),
        (b'{"kind":"c"}', 3, False),
    ]
    assert [item.size for item in lines] == [12, 12, 12]


async def test_lines_handle_newlines_alone_in_chunks_and_empty_input():
    chunks = chunked(b"abc", b"\n", b"\n", b"def\n", b"")
    assert [item.data async for item in iter_lines(chunks, 1048576)] == [b"abc", b"def"]
    assert [item async for item in iter_lines(chunked(), 1048576)] == []
    assert [item async for item in iter_lines(chunked(b"\n\n"), 1048576)] == []


async def test_over_line_is_capped():
    big = b"x" * (3 * 1024 * 1024)
    chunks = chunked(big[:1_000_000], big[1_000_000:2_500_000], big[2_500_000:] + b"\nnext\n")
    lines = [item async for item in iter_lines(chunks, 1048576)]
    assert [item.over for item in lines] == [True, False]
    assert lines[0].size == 3145728
    assert len(lines[0].data) == TRUNCATED_TEXT_BYTES
    assert lines[0].number == 1
    assert (lines[1].data, lines[1].number) == (b"next", 2)


async def test_line_of_exactly_the_limit_is_not_over():
    lines = [item async for item in iter_lines(chunked(b"y" * 1048576), 1048576)]
    assert [(item.over, item.size, len(item.data)) for item in lines] == [(False, 1048576, 1048576)]


async def test_over_line_over_many_chunks_keeps_the_prefix_and_counts_all_bytes():
    chunks = chunked(*([b"z" * 100_000] * 30 + [b"\n"]))
    lines = [item async for item in iter_lines(chunks, 1048576)]
    assert [(item.over, item.size, item.data) for item in lines] == [
        (True, 3_000_000, b"z" * TRUNCATED_TEXT_BYTES)
    ]


def test_over_line_record_shape():
    prefix = b'{"kind":"event","id":42,"data":"' + b"x" * 1000
    record = parse_line(Line(prefix, 3145728, True, 7))
    assert record.body == {"kind": "event", "id": 42, "truncated": True, "bytes": 3145728}
    assert record.kind == "event"
    assert record.truncated is True
    assert record.size == 3145728
    assert record.text.startswith('{"kind":"event","id":42,"data":"xxx')


def test_over_line_without_kind_is_unknown_and_text_replaces_bad_bytes():
    record = parse_line(Line(b"\xff\x00garbage", 99, True, 1))
    assert record.kind == "unknown"
    assert record.body == {"kind": "unknown", "id": None, "truncated": True, "bytes": 99}
    assert record.text == "\ufffdgarbage"


def test_over_line_string_id_is_kept_as_string():
    record = parse_line(Line(b'{"id": "abc", "kind": "turn", "x', 70_000_000, True, 2))
    assert record.body == {"kind": "turn", "id": "abc", "truncated": True, "bytes": 70_000_000}


def test_over_line_huge_numeric_id_is_none():
    record = parse_line(Line(b'{"kind":"event","id":' + b"9" * 5000 + b',"x', 80_000_000, True, 4))
    assert record.body == {"kind": "event", "id": None, "truncated": True, "bytes": 80_000_000}


@pytest.mark.parametrize("ident", [b'"a\tb"', b'"\xff"'])
def test_over_line_undecodable_string_id_is_none(ident):
    record = parse_line(Line(b'{"kind":"turn","id":' + ident + b',"x', 70_000_000, True, 2))
    assert record.body == {"kind": "turn", "id": None, "truncated": True, "bytes": 70_000_000}


def test_parse_line_ok():
    record = parse_line(
        line(b'{"kind":"event","ts":"2026-01-01T00:00:00Z","data":{"a":"b\\u0000c"}}')
    )
    assert record.kind == "event"
    assert record.ts == datetime(2026, 1, 1, tzinfo=UTC)
    assert record.text == "event\n2026-01-01T00:00:00Z\nbc"
    assert record.truncated is False
    assert record.size == len(
        b'{"kind":"event","ts":"2026-01-01T00:00:00Z","data":{"a":"b\\u0000c"}}'
    )


def test_bad_json_names_line_number():
    with pytest.raises(ExportError) as exc:
        parse_line(line(b'{"kind": ', number=5))
    assert exc.value.code == "bad_line"
    assert "line 5" in exc.value.message
    assert str(exc.value) == exc.value.message


def test_invalid_utf8_is_bad_line():
    with pytest.raises(ExportError) as exc:
        parse_line(line(b'{"kind":"a","x":"\xff"}', number=2))
    assert exc.value.code == "bad_line"


@pytest.mark.parametrize(
    "data",
    [b"[1]", b'{"kind":"Bad"}', b"{}", b'{"kind":5}', b'{"kind":"%s"}' % (b"a" * 33)],
)
def test_non_object_and_bad_kind_refused(data):
    with pytest.raises(ExportError) as exc:
        parse_line(line(data, number=3))
    assert exc.value.code == "bad_line"
    assert "line 3" in exc.value.message


def test_session_head_ok():
    head = session_head(parse_line(session_line()), SESSION_ID)
    assert head.session_id == SESSION_ID
    assert head.slug == "brisk-otter"
    assert head.preset_id == uuid.UUID("22222222-2222-4222-8222-222222222222")
    assert head.preset_name == "reviewer"
    assert head.created_by_service == "webhook"
    assert head.created_by_session == uuid.UUID("33333333-3333-4333-8333-333333333333")
    assert head.created_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert head.raw["spec"] == {"agents": []}


def test_session_head_missing_optional_fields_are_none():
    record = parse_line(
        line(
            b'{"kind":"session","id":"%s","slug":"s","created_at":"2026-11-01T10:00:00Z"}'
            % str(SESSION_ID).encode()
        )
    )
    head = session_head(record, SESSION_ID)
    assert (head.preset_id, head.preset_name) == (None, None)
    assert (head.created_by_service, head.created_by_session) == (None, None)


def test_session_head_mismatch():
    with pytest.raises(ExportError) as exc:
        session_head(parse_line(session_line()), uuid.uuid4())
    assert exc.value.code == "bad_export"


def test_session_head_wrong_kind():
    with pytest.raises(ExportError) as exc:
        session_head(parse_line(line(b'{"kind":"agent","id":"x"}')), SESSION_ID)
    assert exc.value.code == "bad_export"


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": None},
        {"slug": ""},
        {"slug": 5},
        {"created_at": None},
        {"created_at": "garbage"},
        {"created_at": "2026-11-01T10:00:00"},
        {"id": "not-a-uuid"},
        {"id": None},
    ],
)
def test_session_head_bad_fields(overrides):
    with pytest.raises(ExportError) as exc:
        session_head(parse_line(session_line(**overrides)), SESSION_ID)
    assert exc.value.code == "bad_export"


def test_session_head_truncated_session_record_is_refused():
    record = parse_line(
        Line(b'{"kind":"session","id":"%s"' % str(SESSION_ID).encode(), 99, True, 1)
    )
    with pytest.raises(ExportError) as exc:
        session_head(record, SESSION_ID)
    assert exc.value.code == "bad_export"
