-- Allow unknown holdout-access evidence. Fail-closed views (028) hide NULL.
-- Existing FALSE/TRUE rows are unchanged. New inserts that omit the column become NULL
-- (hidden), not FALSE (which previously masqueraded as proven non-access).

ALTER TABLE research_runs
    ALTER COLUMN holdout_accessed DROP NOT NULL,
    ALTER COLUMN holdout_accessed DROP DEFAULT;

COMMENT ON COLUMN research_runs.holdout_accessed IS
    'NULL means unknown/unproven holdout access and must be treated as unsafe (hidden). FALSE is proven non-access. TRUE is accessed.';
