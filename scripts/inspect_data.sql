SELECT
    id,
    source,
    status,
    requested_limit,
    returned_count,
    persisted_count,
    pages_processed,
    last_page,
    last_cursor,
    last_offset,
    http_status,
    error_message,
    started_at,
    finished_at
FROM collection_runs
ORDER BY started_at DESC;

SELECT
    id,
    collection_run_id,
    source,
    page_number,
    http_status,
    pagination_offset,
    pagination_token,
    request_params,
    collected_at
FROM raw_api_responses
ORDER BY collected_at DESC;

SELECT
    collection_run_id,
    source,
    external_id,
    title,
    company,
    location,
    published_at,
    published_at_text,
    published_date,
    publication_date_source,
    source_url,
    collected_at
FROM raw_jobs
ORDER BY collected_at DESC;

SELECT
    source,
    COUNT(*) AS total
FROM raw_jobs
GROUP BY source
ORDER BY source;

SELECT
    collection_run_id,
    source,
    external_id,
    COUNT(*) AS occurrences
FROM raw_jobs
WHERE external_id IS NOT NULL
GROUP BY collection_run_id, source, external_id
HAVING COUNT(*) > 1
ORDER BY occurrences DESC;

SELECT
    source,
    jsonb_object_keys(
        CASE
            WHEN jsonb_typeof(raw_payload) = 'object' THEN raw_payload
            ELSE '{}'::jsonb
        END
    ) AS field
FROM raw_jobs
GROUP BY source, field
ORDER BY source, field;
