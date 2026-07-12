BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS collection_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL CHECK (source IN ('theirstack', 'serpapi')),
    query_params JSONB NOT NULL,
    requested_limit INTEGER,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    returned_count INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error_message TEXT,
    pages_processed INTEGER NOT NULL DEFAULT 0,
    persisted_count INTEGER NOT NULL DEFAULT 0,
    last_page INTEGER,
    last_cursor TEXT,
    last_offset INTEGER
);

CREATE TABLE IF NOT EXISTS raw_api_responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_run_id UUID NOT NULL
        REFERENCES collection_runs(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('theirstack', 'serpapi')),
    page_number INTEGER NOT NULL DEFAULT 1,
    request_params JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    http_status INTEGER,
    pagination_token TEXT,
    pagination_offset INTEGER
);

CREATE TABLE IF NOT EXISTS raw_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_run_id UUID NOT NULL
        REFERENCES collection_runs(id) ON DELETE CASCADE,
    raw_api_response_id UUID
        REFERENCES raw_api_responses(id) ON DELETE SET NULL,
    source TEXT NOT NULL CHECK (source IN ('theirstack', 'serpapi')),
    external_id TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    description TEXT,
    published_at TIMESTAMPTZ,
    published_at_text TEXT,
    source_url TEXT,
    raw_payload JSONB NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raw_jobs_source
    ON raw_jobs(source);

CREATE INDEX IF NOT EXISTS idx_raw_jobs_external_id
    ON raw_jobs(source, external_id);

CREATE INDEX IF NOT EXISTS idx_raw_jobs_collected_at
    ON raw_jobs(collected_at);

CREATE INDEX IF NOT EXISTS idx_raw_jobs_location
    ON raw_jobs(location);

CREATE INDEX IF NOT EXISTS idx_raw_jobs_raw_payload_gin
    ON raw_jobs USING GIN(raw_payload);

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_jobs_run_source_external_id_unique
    ON raw_jobs(collection_run_id, source, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_api_responses_run_page
    ON raw_api_responses(collection_run_id, page_number, collected_at);

CREATE INDEX IF NOT EXISTS idx_raw_api_responses_run_pagination_token
    ON raw_api_responses(collection_run_id, pagination_token)
    WHERE pagination_token IS NOT NULL;

COMMIT;
