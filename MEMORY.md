# agents-history memory

Lessons that outlive a session. Newest first.

## 2026-11-02 - service skeleton (session-history A1)

- The skeleton is copied from agents-usage (settings, cli, runtime, the three listener factories, CI workflow with the release job) with the prober, watcher, claude and orchestrator pieces removed; the Dockerfile comes from agents-schedule (no node, no claude, entrypoint `history`). `uv.lock` is generated with `uv lock`, not copied.
- One writer: the api process writes `sessions`, `records` and `settings`. Ingest, the user's delete, the settings change and the retention sweep all run in it, so no table has a second writer and there is no worker process.
- Retention lives in the `settings` row (seeded 90 days by `0001_init.sql`), not in an env var: the user changes it in the web UI at runtime, so a deploy-time value would be a second source of truth. `Settings` therefore has no `retention_days` (a test pins it).
- `pg_trgm` is created in `0001_init.sql`: it is a trusted extension since PG 13, so the CNPG database owner may create it without superuser rights. The schema is whole in 0001 because nothing has been released yet.
- The ingest and service token files must hold disjoint tokens (exit 78 naming `HISTORY_INGEST_TOKENS_FILE`): the ingest token is the orchestrator's discard delivery, the service token the read side, so one leaked token never opens both.
- The three listener factories only build the shared app (error shape, probes, `search_timeout_ms`) and the guard; later tasks `include_router` into them, disjointly per listener.
