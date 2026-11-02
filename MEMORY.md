# agents-history memory

Lessons that outlive a session. Newest first.

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
