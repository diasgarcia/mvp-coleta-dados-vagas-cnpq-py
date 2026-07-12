"""Export the latest successful collection for each provider as sanitized JSON."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from job_collector import db
from job_collector.sanitize import REDACTED, sanitize

SOURCES = ("theirstack", "serpapi")
RESULT_FILENAMES = {
    "theirstack": "theirstack.json",
    "serpapi": "serpapi.json",
}

_LATEST_RUN_SQL = """
    SELECT
        id,
        source,
        query_params,
        requested_limit,
        started_at,
        finished_at,
        status,
        returned_count,
        persisted_count,
        pages_processed,
        http_status,
        error_message,
        last_page,
        last_cursor,
        last_offset
    FROM collection_runs
    WHERE source = %s
      AND status = 'success'
    ORDER BY COALESCE(finished_at, started_at) DESC, started_at DESC, id DESC
    LIMIT 1
"""

_RESPONSES_SQL = """
    SELECT
        id,
        collection_run_id,
        source,
        page_number,
        http_status,
        request_params,
        response_payload,
        pagination_token,
        pagination_offset,
        collected_at
    FROM raw_api_responses
    WHERE collection_run_id = %s
    ORDER BY page_number, collected_at, id
"""

_JOBS_SQL = """
    SELECT
        id,
        collection_run_id,
        raw_api_response_id,
        external_id,
        source,
        title,
        company,
        location,
        description,
        published_at,
        published_at_text,
        source_url,
        collected_at
    FROM raw_jobs
    WHERE collection_run_id = %s
    ORDER BY collected_at, id
"""


def fetch_latest_successful_run(connection: Any, source: str) -> dict[str, Any] | None:
    """Return the latest successful run for a source using the audit timestamps."""

    row = connection.execute(_LATEST_RUN_SQL, (source,)).fetchone()
    return dict(row) if row is not None else None


def fetch_responses(connection: Any, run_id: object) -> list[dict[str, Any]]:
    rows = connection.execute(_RESPONSES_SQL, (run_id,)).fetchall()
    return [dict(row) for row in rows]


def fetch_jobs(connection: Any, run_id: object) -> list[dict[str, Any]]:
    rows = connection.execute(_JOBS_SQL, (run_id,)).fetchall()
    return [dict(row) for row in rows]


def build_export(
    run: Mapping[str, Any],
    responses: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
    *,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Build the provider export without duplicating each job's raw payload."""

    timestamp = exported_at or datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    return {
        "exported_at": timestamp,
        "repository": "python",
        "source": run["source"],
        "run": dict(run),
        "responses": [dict(response) for response in responses],
        "jobs": [dict(job) for job in jobs],
    }


def sanitize_for_export(value: Any, known_secrets: Sequence[str | None] = ()) -> Any:
    """Apply the shared sanitizer and redact configured API keys wherever echoed."""

    sanitized = sanitize(value)
    secrets = tuple(secret for secret in known_secrets if secret)
    return _redact_known_secrets(sanitized, secrets)


def _redact_known_secrets(value: Any, known_secrets: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            _redact_string(key, known_secrets) if isinstance(key, str) else key: (
                _redact_known_secrets(item, known_secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_known_secrets(item, known_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_known_secrets(item, known_secrets) for item in value)
    if isinstance(value, str):
        return _redact_string(value, known_secrets)
    return value


def _redact_string(value: str, known_secrets: tuple[str, ...]) -> str:
    for secret in known_secrets:
        value = value.replace(secret, REDACTED)
    return value


def write_export_file(
    path: Path,
    payload: Mapping[str, Any],
    known_secrets: Sequence[str | None] = (),
) -> None:
    """Write UTF-8 JSON atomically so a previous valid export survives a failure."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
                sanitize_for_export(payload, known_secrets),
                file,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            file.write("\n")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def export_results_from_connection(
    connection: Any,
    *,
    output_dir: Path = Path("results"),
    known_secrets: Sequence[str | None] = (),
    exported_at: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Export both providers, continuing when one has no successful collection."""

    summaries: list[dict[str, Any]] = []
    missing_sources: list[str] = []

    for source in SOURCES:
        run = fetch_latest_successful_run(connection, source)
        if run is None:
            missing_sources.append(source)
            continue

        run_id = run["id"]
        responses = fetch_responses(connection, run_id)
        jobs = fetch_jobs(connection, run_id)
        payload = build_export(run, responses, jobs, exported_at=exported_at)
        path = output_dir / RESULT_FILENAMES[source]
        write_export_file(path, payload, known_secrets)
        summaries.append(
            {
                "source": source,
                "run_id": str(run_id),
                "response_count": len(responses),
                "job_count": len(jobs),
                "path": str(path),
            }
        )

    return summaries, missing_sources


def export_results(
    database_url: str,
    *,
    output_dir: Path = Path("results"),
    known_secrets: Sequence[str | None] = (),
    connection_factory: Callable[..., Any] = psycopg.connect,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Open the Python database once and export the two independent providers."""

    try:
        with connection_factory(database_url, row_factory=dict_row) as connection:
            return export_results_from_connection(
                connection,
                output_dir=output_dir,
                known_secrets=known_secrets,
            )
    except psycopg.Error:
        raise db.DatabaseError("Falha ao exportar os resultados do PostgreSQL Python.") from None
