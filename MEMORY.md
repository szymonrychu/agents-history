# agents-history memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - helm chart (session-history A7)

- The chart is copied from agents-usage with the prober, watcher, claude and orchestrator pieces removed: one api Deployment with a `migrate` init container. The chart passes `helm lint` and the render tests (`mise run test:chart`), the first green since A1 (ruling P-16).
- The Service is named `<fullname>` (not `<fullname>-api`) so agents-helmfile and the orchestrator reach it as `http://agents-history:8001` (service) and `:8002` (ingest) with release name `agents-history`.
- The api memory limit is 512Mi (agents-usage: 256Mi): ingest streams line by line, but search results (three snippet windows per session, up to 200 sessions a page) are built in memory.
- Both token Secrets are created by agents-helmfile and referenced by name; the chart never renders either token. The init container mounts no token volume and sees no `*_TOKENS_FILE` env.

## 2026-11-02 - retention settings and sweep (session-history A5)

- The sweep runs inside the api process (`runtime._serve` starts `run_sweeper` next to the listeners and stops it before the pool closes), so the api stays the only writer of `sessions`, `records` and `settings`; there is no worker process.
- `sweep_once` takes `pg_try_advisory_xact_lock(hashtext('agents-history-sweep'))` first: with several replicas only one sweeps, the others get None and wait for their next tick. The lock is transaction-scoped, so a crashed sweep never leaves it held. All batches of one sweep run in that one transaction; the lock and the delete roll back together.
- Retention lives in the `settings` row so the UI edits it at runtime (`GET/PUT /history/v1/settings`, UI listener only). `set_retention` reads the previous value `FOR UPDATE`; only a lower value sets `app.state.sweep_wake` (a raise changes nothing already stored).
- The wake event is created in `_serve` and passed as `create_app(settings, pool, sweep_wake=wake)` (ruling T-A5); `create_app` makes its own when none is passed. The sweeper clears `wake` before each sweep, so a PUT during a sweep still triggers the next one.
- `test_read_routes_per_listener` listed the UI routes exactly; it now also lists `/settings` (get, put) because the UI app gained that route. The `app` and `db` fixtures moved from `test_read_api.py` to `conftest.py` (identical bodies) since `test_retention.py` uses them too.

## 2026-11-02 - search, read and delete (session-history A4)

- `GET /sessions` answers a bare JSON list, not an envelope: the orchestrator relay filters lists by `preset_id` and needs one. Paging is by `before_id` (newest id first).
- Every filter, `q` included, narrows the sessions before the keyset page is cut (one statement: filtered sessions, per-session hit counts, then `LATERAL ... ORDER BY id LIMIT 3` for the matches). A page is therefore never emptied by a filter applied afterwards (ruling S-R1); the relay must pass `preset` and `session` through as query params.
- `HISTORY_SEARCH_TIMEOUT_SECONDS` (8 s) is applied with `set_config('statement_timeout', ..., true)` (SET LOCAL) inside the query's transaction, below the relay's 10 s; a cancel is 422 `search_timeout`. A broken regex is `invalid_regex` even when no row would evaluate it: the planner compiles the constant pattern (tested with nothing to scan), so there is no up-front compile query.
- `q` must be 3 to 500 characters (one error code, `query_too_short`; an empty `q` is 422 too): under 3 characters the trigram index cannot be used and every record would be scanned.
- Snippets (fix round 1): the user's pattern never runs in Python. In the one timed statement SQL finds the match span (`strpos(lower(..))` + `length(q)` for substring, `regexp_instr` start and end for regex, PG 15+; production is PG 16) and returns a window of three snippet widths centred on it with the span inside the window; `snippet()` is a pure slice that adds the "..." markers. Python `re` took 25 s on `(a+)+c` over 26 characters (GIL held, event loop frozen) while Postgres' engine answers in under 1 ms and is stopped by `statement_timeout` when it does backtrack (back-references).
- Search statement shape: the page is picked with `WHERE EXISTS (match)` (stops at the first hit per session), then `count(*)` and the three matches run only for the page's sessions. `records_page` with `after_id` is one statement (page plus an `EXISTS` for `has_more_before`), so one 8 s timeout, not two.
- `agents` of the detail come from the `agent` records (`id`, `ordinal`, `pod_name` in the body); an agent record without integer id and ordinal is left out.
- Delete is mounted on the UI listener only (`read.delete_router`); the service listener answers 405 to DELETE on the same path. User decision Q6 (mx-99 #691): the superleader gets read and search only. `delete_session` is a write, so it lives in `db/sessions.py`; `db/read.py` stays read-only.

## 2026-11-02 - openapi documents and readme (session-history A6)

- Two committed documents, one per listener that has a spec: `openapi.json` (UI: read, delete, settings) and `openapi-service.json` (service: exactly the three GET routes, no delete anywhere, Q6). The ingest route is documented in the README only: nothing vendors it, and its raw streaming body has no FastAPI schema.
- The documents hold only `/history/v1` paths (the probes are left out) so the orchestrator relay contract test, which binds every relay entry to the upstream spec in both directions, sees exactly the API. The guard error is per listener (403 on the UI, 401 on the service); the route errors are the shared `ErrorResponse` shape, as agents-usage.
- Routes of a listener are listed from the generated openapi, not `app.routes` (FastAPI 0.162 includes routers lazily).

## 2026-11-02 - ingest (session-history A3)

- A re-delivery replaces: one transaction deletes the stored session (records cascade), inserts the session row, COPYs the records and sets the counts. An ExportError mid-stream or a dropped connection rolls it all back, so the previous copy stays intact and nothing half-stored is ever visible.
- `discarded_at` is the server receive time (the column default), not a field of the export: the orchestrator sends the export at discard, and the retention sweep counts from when this service got it.
- Records are written with psycopg `COPY ... FROM STDIN`, not row INSERTs: an export can hold tens of thousands of event lines and COPY streams them in one round trip per buffer.
- There is no total body cap: the body is streamed line by line (at most `HISTORY_INGEST_MAX_LINE_BYTES` of one line in memory) and an over line is stored truncated, so a cap would only refuse a long session, and a refused export blocks the discard forever.
- Postgres jsonb refuses NUL, lone surrogates and NaN/Infinity, which `json.loads` accepts. `db.sessions.jsonb_safe` copies every body (records and the session record) with strings and keys through `export.clean_str` (NUL removed, lone surrogate to U+FFFD) and non-finite numbers as null; the A2 parser keeps the body as parsed.
- `SessionHead.size` carries the session line size so `sessions.bytes` counts every line, while `replace_session` keeps the `(conn, head, records)` signature later tasks seed with.
- Two concurrent deliveries of one session serialise on `pg_advisory_xact_lock(hashtextextended(session_id, 0))` taken first in the ingest transaction: the second waits, then replaces the first copy. Without it the second hit the `session_id` unique index and answered 500 (A3 review finding 1), which made the orchestrator record a spurious `discard_hook_failed` and back off. (2026-11-02, supersedes the earlier no-lock note.)

## 2026-11-02 - export line parser (session-history A2)

- Oversize lines (above `HISTORY_INGEST_MAX_LINE_BYTES`) are stored truncated instead of refusing the export: a refused export would block the orchestrator's discard forever. `iter_lines` therefore buffers at most `max_line_bytes` of one line and keeps only the first 1 MiB of an over line; `parse_line` builds its body from `kind` and `id` found by regex in that prefix.
- The optional session fields (`preset_id`, `preset_name`, `created_by_service`, `created_by_session`) that are missing or do not parse become None in `session_head`, for the same reason; only `id`, `slug` and `created_at` can make the export `bad_export`.
- `record_text` is iterative, so a deeply nested record cannot overflow the stack after `json.loads` accepted it.

## 2026-11-02 - service skeleton (session-history A1)

- The skeleton is copied from agents-usage (settings, cli, runtime, the three listener factories, CI workflow with the release job) with the prober, watcher, claude and orchestrator pieces removed; the Dockerfile comes from agents-schedule (no node, no claude, entrypoint `history`). `uv.lock` is generated with `uv lock`, not copied.
- One writer: the api process writes `sessions`, `records` and `settings`. Ingest, the user's delete, the settings change and the retention sweep all run in it, so no table has a second writer and there is no worker process.
- Retention lives in the `settings` row (seeded 90 days by `0001_init.sql`), not in an env var: the user changes it in the web UI at runtime, so a deploy-time value would be a second source of truth. `Settings` therefore has no `retention_days` (a test pins it).
- `pg_trgm` is created in `0001_init.sql`: it is a trusted extension since PG 13, so the CNPG database owner may create it without superuser rights. The schema is whole in 0001 because nothing has been released yet.
- The ingest and service token files must hold disjoint tokens (exit 78 naming `HISTORY_INGEST_TOKENS_FILE`): the ingest token is the orchestrator's discard delivery, the service token the read side, so one leaked token never opens both.
- The three listener factories only build the shared app (error shape, probes, `search_timeout_ms`) and the guard; later tasks `include_router` into them, disjointly per listener.
