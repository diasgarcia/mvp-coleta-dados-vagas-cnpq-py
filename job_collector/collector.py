"""Synchronous raw-first orchestration for both supported providers."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from job_collector import db, serpapi, theirstack
from job_collector.config import Config
from job_collector.sanitize import sanitize, sanitize_text

RETRYABLE_HTTP_STATUSES = frozenset({500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0
DEFAULT_RETRY_DELAY_SECONDS = 0.15


@dataclass(frozen=True, slots=True)
class HttpResult:
    status_code: int
    payload: Any


@dataclass(frozen=True, slots=True)
class CollectionResult:
    collection_run_id: str
    source: str
    status: str
    http_status: int | None
    pages_processed: int
    returned_count: int
    persisted_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CollectionError(RuntimeError):
    """A collection failure with only safe, user-facing context."""

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        http_status: int | None = None,
        pages_processed: int = 0,
        returned_count: int = 0,
        persisted_count: int = 0,
    ) -> None:
        safe_message = sanitize_text(message) or "Falha durante a coleta."
        super().__init__(safe_message)
        self.source = source
        self.run_id = run_id
        self.status = status
        self.http_status = http_status
        self.pages_processed = pages_processed
        self.returned_count = returned_count
        self.persisted_count = persisted_count


def request_with_retry(
    send: Callable[[], httpx.Response],
    *,
    max_retries: int,
    on_retry_response: Callable[[HttpResult], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
) -> HttpResult:
    """Send one request with the explicit, credit-conscious retry policy."""
    _validate_max_retries(max_retries)
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds deve ser maior ou igual a zero.")

    for attempt in range(max_retries + 1):
        try:
            response = send()
        except httpx.RequestError:
            if attempt >= max_retries:
                raise CollectionError("Falha de rede ou timeout ao consultar a API.") from None
            sleep(retry_delay_seconds * (2**attempt))
            continue

        result = HttpResult(
            status_code=response.status_code,
            payload=_read_response_payload(response),
        )
        if response.status_code not in RETRYABLE_HTTP_STATUSES or attempt >= max_retries:
            return result

        retry_after = _retry_after_seconds(response.headers.get("Retry-After"), now())
        if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
            return result

        if on_retry_response is not None:
            on_retry_response(result)
        backoff = retry_delay_seconds * (2**attempt)
        sleep(max(backoff, retry_after or 0.0))

    raise CollectionError("Falha de rede ou timeout ao consultar a API.")


def collect_theirstack(
    config: Config,
    *,
    query: str | Sequence[str] | None = None,
    location_ids: int | Sequence[int] | None = None,
    limit: int | None = None,
    max_age_days: int = 30,
    max_pages: int | None = None,
    max_retries: int | None = None,
    preview: bool = False,
    include_total_results: bool = True,
    remote: bool | None = None,
    client: httpx.Client | None = None,
    database: Any = db,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectionResult:
    """Collect TheirStack pages, committing every raw response before mapping."""
    if not config.theirstack_api_key:
        raise CollectionError("Configuração obrigatória ausente: THEIRSTACK_API_KEY.")

    resolved_limit = config.theirstack_limit if limit is None else limit
    resolved_pages = config.theirstack_max_pages if max_pages is None else max_pages
    resolved_retries = config.http_max_retries if max_retries is None else max_retries
    resolved_locations = config.theirstack_location_id if location_ids is None else location_ids
    _validate_max_retries(resolved_retries)

    base_request, audit_params = theirstack.build_request(
        query=query,
        location_id=resolved_locations,
        limit=resolved_limit,
        max_pages=resolved_pages,
        max_age_days=max_age_days,
        preview=preview,
        remote=remote,
        include_total_results=include_total_results,
    )
    audit_params["max_retries"] = resolved_retries
    safe_audit = _safe_mapping(audit_params)
    run_id = database.create_collection_run(
        config.database_url,
        "theirstack",
        safe_audit,
        resolved_limit,
    )

    pages_processed = 0
    returned_total = 0
    persisted_total = 0
    last_http_status: int | None = None
    visited_offsets: set[int] = set()
    offset = 0

    try:
        with _http_client(config, client) as http_client:
            for page_number in range(1, resolved_pages + 1):
                if offset in visited_offsets:
                    raise CollectionError(f"A paginação da TheirStack repetiu o offset {offset}.")
                visited_offsets.add(offset)

                request_params = dict(base_request)
                request_params["offset"] = offset
                request_params["include_total_results"] = include_total_results and page_number == 1
                safe_request = _safe_mapping(request_params)

                def preserve_retry(result: HttpResult) -> None:
                    database.save_raw_response(
                        config.database_url,
                        run_id,
                        "theirstack",
                        page_number,
                        result.status_code,
                        None,
                        offset,
                        safe_request,
                        sanitize(result.payload),
                    )

                result = request_with_retry(
                    lambda: http_client.post(
                        theirstack.THEIRSTACK_URL,
                        json=request_params,
                        headers={
                            "Authorization": f"Bearer {config.theirstack_api_key}",
                            "Content-Type": "application/json",
                        },
                    ),
                    max_retries=resolved_retries,
                    on_retry_response=preserve_retry,
                    sleep=sleep,
                )
                last_http_status = result.status_code
                raw_response_id = database.save_raw_response(
                    config.database_url,
                    run_id,
                    "theirstack",
                    page_number,
                    result.status_code,
                    None,
                    offset,
                    safe_request,
                    sanitize(result.payload),
                )

                kind, raw_items, classification_error = theirstack.classify_response(
                    result.status_code, result.payload
                )
                if kind not in {"success_with_results", "success_empty"}:
                    raise CollectionError(
                        classification_error or "Resposta da TheirStack não reconhecida.",
                        http_status=result.status_code,
                    )

                jobs = [theirstack.map_job(item) for item in raw_items]
                candidate_offset = theirstack.next_offset(
                    result.payload, current_offset=offset, limit=resolved_limit
                )
                persisted = database.save_jobs_and_progress(
                    config.database_url,
                    run_id,
                    raw_response_id,
                    "theirstack",
                    jobs,
                    len(raw_items),
                    page_number,
                    result.status_code,
                    None,
                    candidate_offset,
                )
                pages_processed += 1
                returned_total += len(raw_items)
                persisted_total += persisted

                if candidate_offset is None:
                    break
                if candidate_offset in visited_offsets:
                    raise CollectionError(
                        f"A paginação da TheirStack repetiu o offset {candidate_offset}."
                    )
                if page_number >= resolved_pages:
                    break
                offset = candidate_offset

        database.finish_collection_run(config.database_url, run_id, last_http_status)
    except Exception as error:
        failure = _finish_failure(
            database=database,
            config=config,
            source="theirstack",
            run_id=run_id,
            pages_processed=pages_processed,
            returned_count=returned_total,
            persisted_count=persisted_total,
            http_status=_error_http_status(error, last_http_status),
            error=error,
        )
        raise failure from None

    return CollectionResult(
        collection_run_id=run_id,
        source="theirstack",
        status="success",
        http_status=last_http_status,
        pages_processed=pages_processed,
        returned_count=returned_total,
        persisted_count=persisted_total,
    )


def collect_serpapi(
    config: Config,
    *,
    query: str | None = None,
    location: str | None = None,
    max_pages: int | None = None,
    max_retries: int | None = None,
    client: httpx.Client | None = None,
    database: Any = db,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectionResult:
    """Collect SerpApi pages without ever persisting its API key."""
    if not config.serpapi_api_key:
        raise CollectionError("Configuração obrigatória ausente: SERPAPI_API_KEY.")

    resolved_query = config.serpapi_query if query is None else query
    resolved_location = config.serpapi_location if location is None else location
    resolved_pages = config.serpapi_max_pages if max_pages is None else max_pages
    resolved_retries = config.http_max_retries if max_retries is None else max_retries
    _validate_max_retries(resolved_retries)

    base_request, audit_params = serpapi.build_request(
        query=resolved_query,
        location=resolved_location,
        max_pages=resolved_pages,
    )
    audit_params["max_retries"] = resolved_retries
    safe_audit = _safe_mapping(audit_params)
    run_id = database.create_collection_run(
        config.database_url,
        "serpapi",
        safe_audit,
        None,
    )

    pages_processed = 0
    returned_total = 0
    persisted_total = 0
    last_http_status: int | None = None
    current_token: str | None = None
    used_tokens: set[str] = set()

    try:
        with _http_client(config, client) as http_client:
            for page_number in range(1, resolved_pages + 1):
                if current_token is not None:
                    if current_token in used_tokens:
                        raise CollectionError(
                            "A SerpApi repetiu um token de paginação já utilizado."
                        )
                    used_tokens.add(current_token)

                request_params = dict(base_request)
                if current_token is not None:
                    request_params["next_page_token"] = current_token
                safe_request = _safe_mapping(request_params)
                authenticated_params = dict(request_params)
                authenticated_params["api_key"] = config.serpapi_api_key

                def preserve_retry(result: HttpResult) -> None:
                    database.save_raw_response(
                        config.database_url,
                        run_id,
                        "serpapi",
                        page_number,
                        result.status_code,
                        current_token,
                        None,
                        safe_request,
                        sanitize(result.payload),
                    )

                result = request_with_retry(
                    lambda: http_client.get(
                        serpapi.SERPAPI_URL,
                        params=authenticated_params,
                    ),
                    max_retries=resolved_retries,
                    on_retry_response=preserve_retry,
                    sleep=sleep,
                )
                last_http_status = result.status_code
                raw_response_id = database.save_raw_response(
                    config.database_url,
                    run_id,
                    "serpapi",
                    page_number,
                    result.status_code,
                    current_token,
                    None,
                    safe_request,
                    sanitize(result.payload),
                )

                kind, raw_items, classification_error = serpapi.classify_response(
                    result.status_code, result.payload
                )
                if kind not in {"success_with_results", "success_empty"}:
                    raise CollectionError(
                        classification_error or "Resposta da SerpApi não reconhecida.",
                        http_status=result.status_code,
                    )

                jobs = [serpapi.map_job(item) for item in raw_items]
                candidate_token = serpapi.next_page_token(result.payload)
                persisted = database.save_jobs_and_progress(
                    config.database_url,
                    run_id,
                    raw_response_id,
                    "serpapi",
                    jobs,
                    len(raw_items),
                    page_number,
                    result.status_code,
                    candidate_token,
                    None,
                )
                pages_processed += 1
                returned_total += len(raw_items)
                persisted_total += persisted

                if candidate_token is None:
                    break
                if candidate_token in used_tokens:
                    raise CollectionError("A SerpApi repetiu um token de paginação já utilizado.")
                if page_number >= resolved_pages:
                    break
                current_token = candidate_token

        database.finish_collection_run(config.database_url, run_id, last_http_status)
    except Exception as error:
        failure = _finish_failure(
            database=database,
            config=config,
            source="serpapi",
            run_id=run_id,
            pages_processed=pages_processed,
            returned_count=returned_total,
            persisted_count=persisted_total,
            http_status=_error_http_status(error, last_http_status),
            error=error,
        )
        raise failure from None

    return CollectionResult(
        collection_run_id=run_id,
        source="serpapi",
        status="success",
        http_status=last_http_status,
        pages_processed=pages_processed,
        returned_count=returned_total,
        persisted_count=persisted_total,
    )


def _read_response_payload(response: httpx.Response) -> Any:
    try:
        body = response.text
    except Exception:
        return {"body_unavailable": True}
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw_text": body}


def _retry_after_seconds(value: str | None, now_timestamp: float) -> float | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    if candidate.isdecimal():
        return float(candidate)
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, parsed.timestamp() - now_timestamp)


def _validate_max_retries(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise ValueError("max_retries deve estar entre 0 e 3.")


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    safe = sanitize(dict(value))
    if not isinstance(safe, dict):
        raise CollectionError("Falha ao sanitizar os parâmetros da coleta.")
    return safe


@contextmanager
def _http_client(config: Config, client: httpx.Client | None):
    if client is not None:
        yield client
        return
    with httpx.Client(
        timeout=config.http_timeout_seconds,
        headers={"Accept": "application/json"},
    ) as owned_client:
        yield owned_client


def _error_http_status(error: Exception, fallback: int | None) -> int | None:
    if isinstance(error, CollectionError) and error.http_status is not None:
        return error.http_status
    return fallback


def _safe_failure_message(error: Exception) -> str:
    if isinstance(error, (CollectionError, db.DatabaseError, ValueError)):
        return sanitize_text(str(error)) or "Falha durante a coleta."
    return "Falha inesperada durante a coleta."


def _finish_failure(
    *,
    database: Any,
    config: Config,
    source: str,
    run_id: str,
    pages_processed: int,
    returned_count: int,
    persisted_count: int,
    http_status: int | None,
    error: Exception,
) -> CollectionError:
    status = "partial" if pages_processed > 0 else "failed"
    message = _safe_failure_message(error)
    try:
        database.fail_collection_run(
            config.database_url,
            run_id,
            status,
            http_status,
            message,
        )
    except Exception:
        message = f"{message} Não foi possível registrar o encerramento no banco."
    return CollectionError(
        message,
        source=source,
        run_id=run_id,
        status=status,
        http_status=http_status,
        pages_processed=pages_processed,
        returned_count=returned_count,
        persisted_count=persisted_count,
    )
