-- Project scoping (S-83): the project a session ran in and the other projects it touched, so the
-- service listener can serve a hive only its own projects. NULL project = not known yet; the
-- orchestrator backfills those through POST /history/v1/ingest/projects.
ALTER TABLE sessions ADD COLUMN project text, ADD COLUMN extra_projects text[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS sessions_project_idx ON sessions (project);
CREATE INDEX IF NOT EXISTS sessions_extra_projects_idx ON sessions USING gin (extra_projects);
-- Sessions already stored whose export carried the fields (NULL-only: nothing is overwritten).
UPDATE sessions
SET project = session ->> 'project'
WHERE project IS NULL AND jsonb_typeof(session -> 'project') = 'string';
UPDATE sessions
SET extra_projects = ARRAY(SELECT jsonb_array_elements_text(session -> 'extra_projects'))
WHERE extra_projects = '{}' AND jsonb_typeof(session -> 'extra_projects') = 'array';
