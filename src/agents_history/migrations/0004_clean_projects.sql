-- 0003 copied extra_projects from the stored session JSON as is: a JSON null element became a
-- NULL array element (GET /sessions then failed validation) and blank names were kept. Drop
-- them, strip the rest, and turn a blank project into NULL (not known yet).
UPDATE sessions
SET extra_projects = ARRAY(
    SELECT btrim(e) FROM unnest(extra_projects) AS e WHERE e IS NOT NULL AND btrim(e) <> ''
)
WHERE EXISTS (SELECT 1 FROM unnest(extra_projects) AS e WHERE e IS NULL OR e <> btrim(e) OR e = '');
UPDATE sessions SET project = NULLIF(btrim(project), '')
WHERE project <> btrim(project) OR project = '';
