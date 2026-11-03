-- sha256 (hex) of the raw export body a session was stored from: a re-delivery of the same body
-- is answered without rewriting anything. NULL for sessions stored before this column existed.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS content_sha256 text;
