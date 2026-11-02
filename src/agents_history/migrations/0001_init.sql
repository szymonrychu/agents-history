CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS sessions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id uuid NOT NULL UNIQUE,
    slug text NOT NULL CHECK (char_length(slug) BETWEEN 1 AND 128),
    preset_id uuid,
    preset_name text,
    created_by_service text,
    created_by_session uuid,
    created_at timestamptz NOT NULL,
    discarded_at timestamptz NOT NULL DEFAULT now(),
    session jsonb NOT NULL,
    record_counts jsonb NOT NULL DEFAULT '{}',
    bytes bigint NOT NULL DEFAULT 0 CHECK (bytes >= 0)
);
CREATE INDEX IF NOT EXISTS sessions_discarded_at_idx ON sessions (discarded_at);
CREATE INDEX IF NOT EXISTS sessions_created_at_idx ON sessions (created_at);
CREATE INDEX IF NOT EXISTS sessions_preset_id_idx ON sessions (preset_id);
CREATE TABLE IF NOT EXISTS records (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_pk bigint NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind ~ '^[a-z_]{1,32}$'),
    ts timestamptz,
    body jsonb NOT NULL,
    text text NOT NULL,
    truncated boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS records_session_kind_idx ON records (session_pk, kind, id);
CREATE INDEX IF NOT EXISTS records_session_idx ON records (session_pk, id);
CREATE INDEX IF NOT EXISTS records_text_trgm_idx ON records USING gin (text gin_trgm_ops);
CREATE TABLE IF NOT EXISTS settings (
    id smallint PRIMARY KEY CHECK (id = 1),
    retention_days integer NOT NULL CHECK (retention_days BETWEEN 1 AND 3650),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_sweep_at timestamptz,
    last_sweep_deleted integer
);
INSERT INTO settings (id, retention_days) VALUES (1, 90) ON CONFLICT DO NOTHING;
