BEGIN;

ALTER TABLE raw_jobs
    ADD COLUMN IF NOT EXISTS published_date DATE;

ALTER TABLE raw_jobs
    ADD COLUMN IF NOT EXISTS publication_date_source TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'raw_jobs_publication_date_source_check'
          AND conrelid = 'raw_jobs'::regclass
    ) THEN
        ALTER TABLE raw_jobs
            ADD CONSTRAINT raw_jobs_publication_date_source_check
            CHECK (
                publication_date_source IN (
                    'theirstack_exact',
                    'serpapi_estimated',
                    'missing',
                    'unrecognized'
                )
            );
    END IF;
END
$$;

COMMIT;
