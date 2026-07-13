"""PostgreSQL persistence with explicit raw-first commits."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from job_collector import serpapi, theirstack
from job_collector.sanitize import sanitize, sanitize_text

MIGRATIONS_DIRECTORY = Path(__file__).resolve().parent.parent / "migrations"
SOURCES = ("theirstack", "serpapi")

ROUND_RUNS_SQL = """
    SELECT id, source, query_params, requested_limit, started_at, finished_at,
        status, returned_count, persisted_count, pages_processed, http_status,
        error_message, last_page, last_cursor, last_offset
    FROM collection_runs
    WHERE query_params->>'collection_kind'='monthly'
        AND query_params->>'round_id'=%s
    ORDER BY started_at, source, id
"""
ROUND_RESPONSES_SQL = """
    SELECT response.id, response.collection_run_id, response.source,
        response.page_number, response.http_status, response.request_params,
        response.response_payload, response.pagination_token,
        response.pagination_offset, response.collected_at
    FROM raw_api_responses AS response
    JOIN collection_runs AS run ON run.id=response.collection_run_id
    WHERE run.query_params->>'collection_kind'='monthly'
        AND run.query_params->>'round_id'=%s
    ORDER BY response.source, response.collection_run_id,
        response.page_number, response.collected_at, response.id
"""
ROUND_JOBS_SQL = """
    SELECT job.id, job.collection_run_id, job.raw_api_response_id,
        job.external_id, job.source, job.title, job.company, job.location,
        job.description, job.published_at, job.published_at_text,
        job.published_date, job.publication_date_source, job.source_url,
        job.collected_at
    FROM raw_jobs AS job
    JOIN collection_runs AS run ON run.id=job.collection_run_id
    WHERE run.query_params->>'collection_kind'='monthly'
        AND run.query_params->>'round_id'=%s
    ORDER BY job.source, job.collection_run_id, job.collected_at, job.id
"""


def run_migrations(database_url: str) -> None:
    """Apply every SQL migration in filename order."""
    try:
        with psycopg.connect(database_url) as connection:
            for path in sorted(MIGRATIONS_DIRECTORY.glob("*.sql")):
                connection.execute(path.read_text(encoding="utf-8"))
    except (OSError, psycopg.Error):
        raise RuntimeError("Falha ao executar as migrations no PostgreSQL.") from None


def _committed_row(
    connection: Any, sql: str, params: tuple[object, ...], error_message: str
) -> tuple[Any, ...]:
    try:
        row = connection.execute(sql, params).fetchone()
        if row is None:
            raise RuntimeError(error_message)
        connection.commit()
        return row
    except (psycopg.Error, RuntimeError):
        connection.rollback()
        raise RuntimeError(error_message) from None


def create_run(
    connection: Any,
    source: str,
    query_params: Mapping[str, Any],
    requested_limit: int | None,
) -> str:
    row = _committed_row(
        connection,
        """
        INSERT INTO collection_runs (source, query_params, requested_limit, status)
        VALUES (%s, %s, %s, 'running') RETURNING id
        """,
        (source, Jsonb(sanitize(dict(query_params))), requested_limit),
        "Falha ao criar a execução no PostgreSQL.",
    )
    return str(row[0])


def find_matching_round_run(
    connection: Any,
    source: str,
    signature: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return the newest run matching one monthly query signature."""
    row = connection.execute(
        """
        SELECT id, source, status, query_params, started_at, finished_at
        FROM collection_runs
        WHERE source=%s AND query_params @> %s
        ORDER BY
            CASE status
                WHEN 'success' THEN 0
                WHEN 'partial' THEN 1
                WHEN 'running' THEN 2
                ELSE 3
            END,
            COALESCE(finished_at, started_at) DESC,
            id DESC
        LIMIT 1
        """,
        (source, Jsonb(sanitize(dict(signature)))),
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    keys = ("id", "source", "status", "query_params", "started_at", "finished_at")
    return dict(zip(keys, row, strict=True))


def load_monthly_data(connection: Any, round_id: str) -> dict[str, list[dict[str, Any]]]:
    """Load every persisted run, raw response and normalized job for a round."""
    return {
        "runs": [dict(row) for row in connection.execute(ROUND_RUNS_SQL, (round_id,)).fetchall()],
        "responses": [
            dict(row) for row in connection.execute(ROUND_RESPONSES_SQL, (round_id,)).fetchall()
        ],
        "jobs": [dict(row) for row in connection.execute(ROUND_JOBS_SQL, (round_id,)).fetchall()],
    }


def save_response(
    connection: Any,
    run_id: str,
    source: str,
    page: int,
    http_status: int,
    request_params: Mapping[str, Any],
    payload: Any,
    *,
    token: str | None = None,
    offset: int | None = None,
    known_secrets: Sequence[str | None] = (),
) -> str:
    row = _committed_row(
        connection,
        """
        INSERT INTO raw_api_responses (
            collection_run_id, source, page_number, http_status,
            pagination_token, pagination_offset, request_params, response_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (
            run_id,
            source,
            page,
            http_status,
            token,
            offset,
            Jsonb(sanitize(dict(request_params))),
            Jsonb(sanitize(payload, known_secrets)),
        ),
        "Falha ao salvar a resposta bruta no PostgreSQL.",
    )
    return str(row[0])


INSERT_JOB_SQL = """
    INSERT INTO raw_jobs (
        collection_run_id, raw_api_response_id, source, external_id, title,
        company, location, description, published_at, published_at_text,
        published_date, publication_date_source, source_url, raw_payload, collected_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (collection_run_id, source, external_id)
        WHERE external_id IS NOT NULL DO NOTHING
"""


def _optional_text(value: object, secrets: Sequence[str | None]) -> str | None:
    return None if value is None else sanitize_text(str(value), secrets)


def save_page(
    connection: Any,
    run_id: str,
    response_id: str,
    source: str,
    jobs: Sequence[Mapping[str, Any]],
    *,
    page: int,
    returned: int,
    http_status: int,
    collected_at: datetime,
    next_cursor: str | None = None,
    next_offset: int | None = None,
    known_secrets: Sequence[str | None] = (),
) -> int:
    """Commit mapped jobs and progress together, after the raw commit."""
    try:
        persisted = 0
        for job in jobs:
            cursor = connection.execute(
                INSERT_JOB_SQL,
                (
                    run_id,
                    response_id,
                    source,
                    _optional_text(job.get("external_id"), known_secrets),
                    _optional_text(job.get("title"), known_secrets),
                    _optional_text(job.get("company"), known_secrets),
                    _optional_text(job.get("location"), known_secrets),
                    _optional_text(job.get("description"), known_secrets),
                    job.get("published_at"),
                    _optional_text(job.get("published_at_text"), known_secrets),
                    job.get("published_date"),
                    job.get("publication_date_source"),
                    _optional_text(job.get("source_url"), known_secrets),
                    Jsonb(sanitize(job["raw_payload"], known_secrets)),
                    collected_at,
                ),
            )
            persisted += cursor.rowcount

        row = connection.execute(
            """
            UPDATE collection_runs SET
                pages_processed = pages_processed + 1,
                returned_count = returned_count + %s,
                persisted_count = persisted_count + %s,
                last_page = %s, last_cursor = %s, last_offset = %s,
                http_status = %s
            WHERE id = %s AND source = %s AND status = 'running'
            RETURNING id
            """,
            (
                returned,
                persisted,
                page,
                next_cursor,
                next_offset,
                http_status,
                run_id,
                source,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError
        connection.commit()
        return persisted
    except (KeyError, psycopg.Error, RuntimeError):
        connection.rollback()
        raise RuntimeError("Falha ao salvar vagas e progresso no PostgreSQL.") from None


def finish_run(connection: Any, run_id: str, http_status: int | None) -> None:
    _committed_row(
        connection,
        """
        UPDATE collection_runs SET status='success', finished_at=NOW(),
            http_status=COALESCE(%s, http_status), error_message=NULL
        WHERE id=%s AND status='running' RETURNING id
        """,
        (http_status, run_id),
        "Falha ao finalizar a execução no PostgreSQL.",
    )


def fail_run(
    connection: Any,
    run_id: str,
    status: str,
    http_status: int | None,
    message: str,
    known_secrets: Sequence[str | None] = (),
) -> None:
    connection.rollback()
    _committed_row(
        connection,
        """
        UPDATE collection_runs SET status=%s, finished_at=NOW(),
            http_status=COALESCE(%s, http_status), error_message=%s
        WHERE id=%s AND status='running' RETURNING id
        """,
        (
            status,
            http_status,
            (sanitize_text(message, known_secrets) or "Falha durante a coleta.")[:2000],
            run_id,
        ),
        "Falha ao registrar o erro no PostgreSQL.",
    )


LATEST_RUN_SQL = """
    SELECT id, source, query_params, requested_limit, started_at, finished_at,
        status, returned_count, persisted_count, pages_processed, http_status,
        error_message, last_page, last_cursor, last_offset
    FROM collection_runs WHERE source=%s AND status='success'
    ORDER BY COALESCE(finished_at, started_at) DESC, started_at DESC, id DESC LIMIT 1
"""
RESPONSES_SQL = """
    SELECT id, collection_run_id, source, page_number, http_status, request_params,
        response_payload, pagination_token, pagination_offset, collected_at
    FROM raw_api_responses WHERE collection_run_id=%s
    ORDER BY page_number, collected_at, id
"""
JOBS_SQL = """
    SELECT id, collection_run_id, raw_api_response_id, external_id, source, title,
        company, location, description, published_at, published_at_text,
        published_date, publication_date_source, source_url, collected_at
    FROM raw_jobs WHERE collection_run_id=%s ORDER BY collected_at, id
"""

BACKFILL_SQL = """
    SELECT id, source, raw_payload, published_at_text, collected_at
    FROM raw_jobs
    WHERE publication_date_source IS NULL
    ORDER BY collected_at, id
"""


def backfill_publication_dates(connection: Any) -> dict[str, int]:
    """Classify historical publication dates using each row's collection time."""
    summary = {
        "theirstack_updated": 0,
        "serpapi_updated": 0,
        "missing": 0,
        "unrecognized": 0,
    }
    try:
        rows = connection.execute(BACKFILL_SQL).fetchall()
        for row in rows:
            record = dict(row)
            source = record["source"]
            if source == "theirstack":
                raw_payload = record["raw_payload"]
                date_posted = (
                    raw_payload.get("date_posted") if isinstance(raw_payload, Mapping) else None
                )
                publication = theirstack.parse_exact_publication_date(date_posted)
            else:
                publication = serpapi.parse_relative_publication_date(
                    record["published_at_text"], record["collected_at"]
                )

            cursor = connection.execute(
                """
                UPDATE raw_jobs SET
                    published_date = COALESCE(published_date, %s),
                    publication_date_source = %s
                WHERE id = %s AND publication_date_source IS NULL
                """,
                (
                    publication["published_date"],
                    publication["publication_date_source"],
                    record["id"],
                ),
            )
            if not cursor.rowcount:
                continue
            date_source = publication["publication_date_source"]
            if date_source == "theirstack_exact":
                summary["theirstack_updated"] += 1
            elif date_source == "serpapi_estimated":
                summary["serpapi_updated"] += 1
            else:
                summary[date_source] += 1
        connection.commit()
        return summary
    except (KeyError, TypeError, psycopg.Error):
        connection.rollback()
        raise RuntimeError("Falha ao preencher datas de publicação no PostgreSQL.") from None


def _write_json(path: Path, payload: Mapping[str, Any], secrets: Sequence[str | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(sanitize(payload, secrets), file, ensure_ascii=False, indent=2, default=str)
            file.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_results(
    connection: Any,
    output_dir: Path = Path("results"),
    known_secrets: Sequence[str | None] = (),
) -> tuple[list[dict[str, object]], list[str]]:
    """Export the latest successful run from each source without calling APIs."""
    summaries: list[dict[str, object]] = []
    missing: list[str] = []
    exported_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    for source in SOURCES:
        row = connection.execute(LATEST_RUN_SQL, (source,)).fetchone()
        if row is None:
            missing.append(source)
            continue
        run = dict(row)
        run_id = run["id"]
        responses = [dict(item) for item in connection.execute(RESPONSES_SQL, (run_id,)).fetchall()]
        jobs = [dict(item) for item in connection.execute(JOBS_SQL, (run_id,)).fetchall()]
        path = output_dir / f"{source}.json"
        _write_json(
            path,
            {
                "exported_at": exported_at,
                "repository": "python",
                "source": source,
                "run": run,
                "responses": responses,
                "jobs": jobs,
            },
            known_secrets,
        )
        summaries.append(
            {
                "source": source,
                "run_id": str(run_id),
                "response_count": len(responses),
                "job_count": len(jobs),
                "path": str(path),
            }
        )
    return summaries, missing
