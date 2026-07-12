from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from job_collector.sanitize import sanitize, sanitize_text


class DatabaseError(RuntimeError):
    """Database failure with a message safe to show in the CLI."""


def _migration_path() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations" / "001_initial.sql"


def _required_row(row: tuple[Any, ...] | None, operation: str) -> tuple[Any, ...]:
    if row is None:
        raise DatabaseError(f"Falha ao {operation}: registro nao encontrado ou estado invalido.")
    return row


def _safe_error_message(message: str) -> str:
    text = sanitize_text(message.strip()) or "Falha durante a coleta."
    return text[:2_000]


def _sanitized_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return sanitize_text(value if isinstance(value, str) else str(value))


def run_migrations(database_url: str) -> None:
    migration_path = _migration_path()
    if not migration_path.is_file():
        raise DatabaseError("Migration inicial nao encontrada.")

    try:
        migration_sql = migration_path.read_text(encoding="utf-8")
        with psycopg.connect(database_url) as connection:
            connection.execute(migration_sql)
    except (OSError, psycopg.Error):
        raise DatabaseError("Falha ao executar a migration no PostgreSQL.") from None


def create_collection_run(
    database_url: str,
    source: str,
    query_params: Mapping[str, Any],
    requested_limit: int | None,
) -> str:
    sql = """
        INSERT INTO collection_runs (source, query_params, requested_limit, status)
        VALUES (%s, %s, %s, 'running')
        RETURNING id
    """
    try:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                sql,
                (source, Jsonb(sanitize(dict(query_params))), requested_limit),
            ).fetchone()
        return str(_required_row(row, "criar a execucao")[0])
    except DatabaseError:
        raise
    except psycopg.Error:
        raise DatabaseError("Falha ao criar a execucao no PostgreSQL.") from None


def save_raw_response(
    database_url: str,
    run_id: str,
    source: str,
    page_number: int,
    http_status: int | None,
    pagination_token: str | None,
    pagination_offset: int | None,
    request_params: Mapping[str, Any],
    response_payload: Any,
) -> str:
    sql = """
        INSERT INTO raw_api_responses (
            collection_run_id,
            source,
            page_number,
            http_status,
            pagination_token,
            pagination_offset,
            request_params,
            response_payload
        )
        SELECT
            collection_runs.id,
            collection_runs.source,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s
        FROM collection_runs
        WHERE collection_runs.id = %s
          AND collection_runs.source = %s
          AND collection_runs.status = 'running'
        RETURNING id
    """
    try:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                sql,
                (
                    page_number,
                    http_status,
                    pagination_token,
                    pagination_offset,
                    Jsonb(sanitize(dict(request_params))),
                    Jsonb(sanitize(response_payload)),
                    run_id,
                    source,
                ),
            ).fetchone()
        return str(_required_row(row, "salvar a resposta bruta")[0])
    except DatabaseError:
        raise
    except psycopg.Error:
        raise DatabaseError("Falha ao salvar a resposta bruta no PostgreSQL.") from None


def save_jobs_and_progress(
    database_url: str,
    run_id: str,
    raw_response_id: str,
    source: str,
    jobs: Sequence[Mapping[str, Any]],
    returned_count: int,
    page_number: int,
    http_status: int | None,
    next_cursor: str | None,
    next_offset: int | None,
) -> int:
    if returned_count < 0 or page_number < 1:
        raise DatabaseError("Contadores de pagina invalidos.")
    if len(jobs) > returned_count:
        raise DatabaseError("O numero de vagas mapeadas excede o total retornado.")

    validate_response_sql = """
        SELECT id
        FROM raw_api_responses
        WHERE id = %s
          AND collection_run_id = %s
          AND source = %s
          AND page_number = %s
        FOR SHARE
    """
    insert_job_sql = """
        INSERT INTO raw_jobs (
            collection_run_id,
            raw_api_response_id,
            source,
            external_id,
            title,
            company,
            location,
            description,
            published_at,
            published_at_text,
            source_url,
            raw_payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (collection_run_id, source, external_id)
            WHERE external_id IS NOT NULL
        DO NOTHING
    """
    update_progress_sql = """
        UPDATE collection_runs
        SET
            pages_processed = pages_processed + 1,
            returned_count = returned_count + %s,
            persisted_count = persisted_count + %s,
            last_page = %s,
            last_cursor = %s,
            last_offset = %s,
            http_status = %s
        WHERE id = %s
          AND source = %s
          AND status = 'running'
          AND (
              (last_page IS NULL AND %s = 1)
              OR last_page + 1 = %s
          )
        RETURNING id
    """

    try:
        with psycopg.connect(database_url) as connection:
            response_row = connection.execute(
                validate_response_sql,
                (raw_response_id, run_id, source, page_number),
            ).fetchone()
            _required_row(response_row, "validar a resposta bruta")

            persisted_count = 0
            for job in jobs:
                if "raw_payload" not in job:
                    raise DatabaseError("Uma vaga mapeada nao possui raw_payload.")
                cursor = connection.execute(
                    insert_job_sql,
                    (
                        run_id,
                        raw_response_id,
                        source,
                        _sanitized_optional_text(job.get("external_id")),
                        _sanitized_optional_text(job.get("title")),
                        _sanitized_optional_text(job.get("company")),
                        _sanitized_optional_text(job.get("location")),
                        _sanitized_optional_text(job.get("description")),
                        job.get("published_at"),
                        _sanitized_optional_text(job.get("published_at_text")),
                        _sanitized_optional_text(job.get("source_url")),
                        Jsonb(sanitize(job["raw_payload"])),
                    ),
                )
                persisted_count += cursor.rowcount

            progress_row = connection.execute(
                update_progress_sql,
                (
                    returned_count,
                    persisted_count,
                    page_number,
                    next_cursor,
                    next_offset,
                    http_status,
                    run_id,
                    source,
                    page_number,
                    page_number,
                ),
            ).fetchone()
            _required_row(progress_row, "registrar o progresso da pagina")
        return persisted_count
    except DatabaseError:
        raise
    except psycopg.Error:
        raise DatabaseError("Falha ao salvar vagas e progresso no PostgreSQL.") from None


def finish_collection_run(
    database_url: str,
    run_id: str,
    http_status: int | None,
) -> None:
    sql = """
        UPDATE collection_runs
        SET
            status = 'success',
            finished_at = NOW(),
            http_status = COALESCE(%s, http_status),
            error_message = NULL
        WHERE id = %s
          AND status = 'running'
        RETURNING id
    """
    try:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(sql, (http_status, run_id)).fetchone()
        _required_row(row, "finalizar a execucao")
    except DatabaseError:
        raise
    except psycopg.Error:
        raise DatabaseError("Falha ao finalizar a execucao no PostgreSQL.") from None


def fail_collection_run(
    database_url: str,
    run_id: str,
    status: str,
    http_status: int | None,
    message: str,
) -> None:
    if status not in {"failed", "partial"}:
        raise ValueError("O status de falha deve ser 'failed' ou 'partial'.")

    sql = """
        UPDATE collection_runs
        SET
            status = %s,
            finished_at = NOW(),
            http_status = COALESCE(%s, http_status),
            error_message = %s
        WHERE id = %s
          AND status = 'running'
        RETURNING id
    """
    try:
        with psycopg.connect(database_url) as connection:
            row = connection.execute(
                sql,
                (status, http_status, _safe_error_message(message), run_id),
            ).fetchone()
        _required_row(row, "registrar a falha da execucao")
    except DatabaseError:
        raise
    except psycopg.Error:
        raise DatabaseError("Falha ao registrar o erro no PostgreSQL.") from None
