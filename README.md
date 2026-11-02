# agents-history

Python microservice with its own Postgres that stores the history of discarded agent sessions and serves read, search and retention over it.

The api process (`history api`) is the only writer of the tables `sessions`, `records` and `settings`. It serves three listeners, each with its own guard:

| Listener | Port | Guard | Routes |
|----------|------|-------|--------|
| UI | `HISTORY_HTTP_PORT` (8000) | source address allowlist | read, delete, settings |
| service | `HISTORY_SERVICE_PORT` (8001) | bearer service token | read and search only |
| ingest | `HISTORY_INGEST_PORT` (8002) | bearer ingest token | ingest only |

The service and ingest listeners start only when their tokens file is set. `GET /healthz` and `GET /readyz` answer on every listener without a guard.

Every API path starts with `/history/v1`. The OpenAPI documents are committed as `openapi.json` (UI listener) and `openapi-service.json` (service listener); regenerate them with `uv run python -m agents_history.api.openapi > openapi.json` and `uv run python -m agents_history.api.openapi --service > openapi-service.json`.

Every error is `{"error": {"code": "...", "message": "..."}}`. Codes shared by the listeners:

| Status | Code | When |
|--------|------|------|
| 401 | `unauthorized` | service and ingest listeners: no or an unknown bearer token |
| 403 | `forbidden` | UI listener: the socket peer is outside `HISTORY_ALLOWED_CIDRS` |
| 404 | `not_found` | no such session, or no such route |
| 405 | `method_not_allowed` | the path exists but not for this method |
| 422 | `invalid_request` | a parameter or body does not validate |

## API (UI listener)

Guarded by `HISTORY_ALLOWED_CIDRS` (default `127.0.0.1/32`). Only the socket peer is read; `Forwarded`, `X-Forwarded-For` and `X-Real-IP` are never consulted. This is the only listener with a delete route: deleting a stored session is for the user, through the web UI.

### `GET /history/v1/sessions`

Stored sessions, newest first (by internal `id`). Query parameters:

| Parameter | Meaning |
|-----------|---------|
| `q` | search text, 3 to 500 characters; matches the text of the session's records, case-insensitive. An empty or short `q` is 422 `query_too_short` |
| `regex` | `true` treats `q` as a Postgres regular expression (case-insensitive); default `false` (a literal substring, `%` and `_` are literal) |
| `from` | only sessions discarded at or after this time (ISO 8601) |
| `to` | only sessions created before this time (ISO 8601) |
| `preset` | preset name (exact) or preset id |
| `session` | session id (exact) or part of the slug (case-insensitive) |
| `limit` | 1 to 200, default 50 |
| `before_id` | page cursor: only sessions with an `id` below this; pass the `id` of the last session of the previous page |

Answers a JSON list of sessions: `id`, `session_id`, `slug`, `preset_id`, `preset_name`, `created_by_service`, `created_by_session`, `created_at`, `discarded_at`, `record_counts` (a count per record kind), `bytes`, and with `q`: `hits` (the number of matching records) and `matches` (at most 3 of `record_id`, `kind`, `ts` and a `snippet` of 160 characters centred on the match, with `...` where it was cut). Without `q`, `hits` is `null` and `matches` is empty. Every filter narrows the rows before the page is cut, so a page is never emptied by a later filter.

Errors: 422 `invalid_request`, `query_too_short`, `invalid_regex` (the pattern does not compile), `search_timeout` (the search ran past `HISTORY_SEARCH_TIMEOUT_SECONDS`; narrow it).

### `GET /history/v1/sessions/{session_id}`

One stored session by its session uuid: the list fields plus `session` (the stored session record) and `agents` (`id`, `ordinal`, `pod_name` of each stored agent record). Errors: 404 `not_found`, 422 `invalid_request`.

### `GET /history/v1/sessions/{session_id}/records`

A page of the session's records in ascending `id` order. Query parameters:

| Parameter | Meaning |
|-----------|---------|
| `kind` | comma separated record kinds (`agent`, `message`, `turn`, `command`, `check`, `event`) |
| `q`, `regex` | the same search as the list, applied to the records |
| `limit` | 1 to 1000, default 200 |
| `before_id` | the `limit` records before this record id (scroll back) |
| `after_id` | the next `limit` records after this record id; `0` starts from the first |

With neither cursor the newest `limit` records are returned. Answers `records` (`id`, `kind`, `ts`, `truncated`, `body`), `has_more_before` and `next_after_id` (set only with `after_id`, when more records follow). Errors: 400 `bad_request` (both cursors), 404 `not_found`, 422 `invalid_request`, `query_too_short`, `invalid_regex`, `search_timeout`.

### `DELETE /history/v1/sessions/{session_id}`

Deletes the stored session and its records. 204 without a body. Errors: 404 `not_found`, 422 `invalid_request`.

### `GET /history/v1/settings`

The retention setting: `retention_days`, `updated_at`, `last_sweep_at` and `last_sweep_deleted` (both `null` until the first sweep).

### `PUT /history/v1/settings`

Body `{"retention_days": 30}`, 1 to 3650; other fields are refused. Answers the same object as the get. Lowering the period wakes the sweeper so it applies at once. Error: 422 `invalid_request`.

## API (service listener)

For the orchestrator admin relay (the superleader's history tools). Every request needs `Authorization: Bearer <service token>`; a missing or unknown token is 401 `unauthorized`. It serves the three read routes below and no DELETE: the superleader reads and searches, it never deletes (a DELETE answers 405 `method_not_allowed`).

### `GET /history/v1/sessions`

As on the UI listener.

### `GET /history/v1/sessions/{session_id}`

As on the UI listener.

### `GET /history/v1/sessions/{session_id}/records`

As on the UI listener.

## Ingest

The ingest listener takes the orchestrator's discard export. Its token can add sessions and nothing else; it must differ from every service token (the process exits 78 otherwise).

### `POST /history/v1/ingest`

Headers:

| Header | Meaning |
|--------|---------|
| `Authorization: Bearer <ingest token>` | required; 401 `unauthorized` otherwise |
| `X-Orch-Session-Id` | the session uuid; 422 `invalid_request` when missing or not a uuid |
| `Content-Type: application/x-ndjson` | informational; the body is read as lines whatever it says |

The body is NDJSON (one JSON object per line, empty lines skipped), streamed: at most `HISTORY_INGEST_MAX_LINE_BYTES` of one line is ever held in memory, and there is no cap on the whole body. The first line is the session record (`kind` `session`, with `id` equal to `X-Orch-Session-Id`, a `slug` and a timezone-aware `created_at`; `preset_id`, `preset_name`, `created_by_service` and `created_by_session` are optional and become `null` when missing or malformed). Every later line is a record with a `kind` (lowercase letters and `_`, at most 32); the orchestrator writes `agent`, `message`, `turn`, `command`, `check` and `event`. A record's time is the first of `ts`, `created_at`, `started_at` that is an ISO 8601 time with a zone.

Truncation: a line longer than `HISTORY_INGEST_MAX_LINE_BYTES` is stored truncated instead of refusing the export (a refused export would block the discard forever). Only its first 1048576 bytes are kept as the record text, the body becomes `{"kind", "id", "truncated": true, "bytes": <full line size>}` (kind and id are read from the kept prefix, `unknown` and `null` when absent) and the record has `truncated: true`.

Re-delivering a session replaces the stored copy in one transaction; a failed delivery leaves the previous copy intact. NUL characters and lone surrogates in strings are cleaned before storage. The body and the token are never logged.

Answers `201`:

    {"session_id": "<uuid>", "records": 42, "truncated": 0, "replaced": false}

`records` is the number of records stored (the session record excluded), `truncated` how many were truncated, `replaced` whether an earlier copy was replaced.

Errors: 401 `unauthorized`; 422 with `invalid_request` (bad `X-Orch-Session-Id`), `empty_export` (no lines), `bad_line` (a line is not a JSON object with a valid `kind`) or `bad_export` (the first record is not this request's session, or it lacks a slug or a valid `created_at`).

## Configuration

Environment variables, all prefixed `HISTORY_`. A bad value exits 78 naming each variable at fault.

| Variable | Default | Meaning |
|----------|---------|---------|
| `HISTORY_DATABASE_URL` | required | Postgres url of the service's own database |
| `HISTORY_HTTP_HOST` | `0.0.0.0` | bind address of every listener |
| `HISTORY_HTTP_PORT` | `8000` | UI listener port |
| `HISTORY_ALLOWED_CIDRS` | `127.0.0.1/32` | comma separated CIDRs allowed on the UI listener |
| `HISTORY_SERVICE_PORT` | `8001` | service listener port |
| `HISTORY_SERVICE_TOKENS_FILE` | unset | file with one service token per line; unset disables the service listener |
| `HISTORY_INGEST_PORT` | `8002` | ingest listener port |
| `HISTORY_INGEST_TOKENS_FILE` | unset | file with one ingest token per line; unset disables the ingest listener |
| `HISTORY_SEARCH_TIMEOUT_SECONDS` | `8` | statement timeout of a search, 1 to 60 |
| `HISTORY_SWEEP_INTERVAL_SECONDS` | `3600` | seconds between retention sweeps, 60 to 86400 |
| `HISTORY_SWEEP_BATCH` | `50` | sessions deleted per statement by a sweep, 1 to 1000 |
| `HISTORY_INGEST_MAX_LINE_BYTES` | `67108864` | longest line held in memory; longer lines are stored truncated, 1048576 to 268435456 |
| `HISTORY_LOG_LEVEL` | `INFO` | log level |
| `HISTORY_SHUTDOWN_GRACE_SECONDS` | `20` | time given to in-flight requests and the sweeper on SIGTERM |

The enabled listeners need distinct ports, and the ingest and service tokens must be disjoint. The retention period is not an environment variable: see Retention.

## Retention

The retention period is the `settings` row, seeded at 90 days and changed at runtime with `PUT /history/v1/settings` (1 to 3650 days), so the web UI is its only source of truth. A sweeper inside the api process deletes every session discarded longer ago than the period, `HISTORY_SWEEP_BATCH` sessions per statement, every `HISTORY_SWEEP_INTERVAL_SECONDS`; an advisory lock keeps replicas from sweeping at once. Lowering the period wakes the sweeper immediately. The outcome of the last sweep is `last_sweep_at` and `last_sweep_deleted` in `GET /history/v1/settings`.

## Commands

    history migrate    apply the schema migrations and exit
    history api        serve the listeners (run migrate first)
