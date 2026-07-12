from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from job_collector import db, serpapi, theirstack
from job_collector.config import Config
from job_collector.sanitize import sanitize_text

RETRYABLE = {500, 502, 503, 504}
COLLECTION_TIMEZONE = ZoneInfo("America/Sao_Paulo")


class HttpError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _payload(response: httpx.Response) -> Any:
    if not response.text.strip():
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return {"raw_text": response.text}


def request_json(
    send: Callable[[], httpx.Response],
    retries: int,
    preserve_retry: Callable[[int, Any], None],
) -> tuple[int, Any]:
    """Retry only network failures and transient HTTP responses."""
    if not 0 <= retries <= 3:
        raise ValueError("max_retries deve estar entre 0 e 3.")

    for attempt in range(retries + 1):
        try:
            response = send()
        except httpx.RequestError:
            if attempt == retries:
                raise RuntimeError("Falha de rede ou timeout ao consultar a API.") from None
            time.sleep(0.15 * (2**attempt))
            continue

        payload = _payload(response)
        if response.status_code not in RETRYABLE or attempt == retries:
            return response.status_code, payload

        retry_after = response.headers.get("Retry-After", "")
        seconds = float(retry_after) if retry_after.isdecimal() else 0.0
        if seconds > 60:
            return response.status_code, payload
        preserve_retry(response.status_code, payload)
        time.sleep(max(seconds, 0.15 * (2**attempt)))

    raise RuntimeError("Falha ao consultar a API.")


def _extract(extractor: Callable[[int, Any], list[object]], status: int, payload: Any):
    try:
        return extractor(status, payload)
    except ValueError as error:
        if not 200 <= status < 300:
            raise HttpError(status, str(error)) from None
        raise RuntimeError(str(error)) from None


def _raw_saver(
    connection: Any,
    run_id: str,
    source: str,
    page: int,
    params: dict[str, Any],
    *,
    token: str | None = None,
    offset: int | None = None,
    secrets: Sequence[str | None] = (),
) -> Callable[[int, Any], str]:
    def save(status: int, payload: Any) -> str:
        return db.save_response(
            connection,
            run_id,
            source,
            page,
            status,
            params,
            payload,
            token=token,
            offset=offset,
            known_secrets=secrets,
        )

    return save


def _fail_run(
    connection: Any,
    run_id: str,
    pages_processed: int,
    http_status: int | None,
    error: Exception,
    secrets: Sequence[str | None],
) -> None:
    message = sanitize_text(str(error), secrets) or "Falha durante a coleta."
    status = "partial" if pages_processed else "failed"
    try:
        db.fail_run(connection, run_id, status, http_status, message, secrets)
    except RuntimeError:
        message = f"{message} Não foi possível registrar o encerramento no banco."
    if isinstance(error, HttpError):
        raise HttpError(error.status_code, message) from None
    raise RuntimeError(message) from None


def _result(
    run_id: str,
    source: str,
    http_status: int | None,
    pages: int,
    returned: int,
    persisted: int,
) -> dict[str, object]:
    return {
        "collection_run_id": run_id,
        "source": source,
        "status": "success",
        "http_status": http_status,
        "pages_processed": pages,
        "returned_count": returned,
        "persisted_count": persisted,
    }


def collect_theirstack(
    config: Config,
    connection: Any,
    client: httpx.Client,
    *,
    limit: int | None = None,
    max_pages: int | None = None,
    max_retries: int | None = None,
) -> dict[str, object]:
    """Collect TheirStack pages with the raw response committed first."""
    if not config.theirstack_api_key:
        raise RuntimeError("Configuração obrigatória ausente: THEIRSTACK_API_KEY.")
    limit = config.theirstack_limit if limit is None else limit
    max_pages = config.theirstack_max_pages if max_pages is None else max_pages
    retries = config.http_max_retries if max_retries is None else max_retries
    request, audit = theirstack.build_request(
        location_id=config.theirstack_location_id,
        limit=limit,
        max_pages=max_pages,
    )
    audit["max_retries"] = retries
    run_id = db.create_run(connection, "theirstack", audit, limit)
    secrets = (config.theirstack_api_key, config.serpapi_api_key, config.database_url)
    pages = returned = persisted = 0
    last_status: int | None = None
    used_offsets: set[int] = set()
    offset = 0

    try:
        for page in range(1, max_pages + 1):
            if offset in used_offsets:
                raise RuntimeError(f"A paginação da TheirStack repetiu o offset {offset}.")
            used_offsets.add(offset)
            params = {**request, "offset": offset}
            params["include_total_results"] = page == 1
            save_raw = _raw_saver(
                connection,
                run_id,
                "theirstack",
                page,
                params,
                offset=offset,
                secrets=secrets,
            )

            last_status, payload = request_json(
                lambda: client.post(
                    theirstack.THEIRSTACK_URL,
                    json=params,
                    headers={
                        "Authorization": f"Bearer {config.theirstack_api_key}",
                        "Content-Type": "application/json",
                    },
                ),
                retries,
                save_raw,
            )
            collected_at = datetime.now(COLLECTION_TIMEZONE)
            response_id = save_raw(last_status, payload)
            raw_jobs = _extract(theirstack.extract_jobs, last_status, payload)
            next_offset = theirstack.next_offset(payload, offset, limit)
            saved = db.save_page(
                connection,
                run_id,
                response_id,
                "theirstack",
                [theirstack.map_job(job) for job in raw_jobs],
                page=page,
                returned=len(raw_jobs),
                http_status=last_status,
                next_offset=next_offset,
                collected_at=collected_at,
                known_secrets=secrets,
            )
            pages += 1
            returned += len(raw_jobs)
            persisted += saved
            if next_offset is None or page == max_pages:
                break
            offset = next_offset

        db.finish_run(connection, run_id, last_status)
    except Exception as error:
        _fail_run(connection, run_id, pages, last_status, error, secrets)
    return _result(run_id, "theirstack", last_status, pages, returned, persisted)


def collect_serpapi(
    config: Config,
    connection: Any,
    client: httpx.Client,
    *,
    max_pages: int | None = None,
    max_retries: int | None = None,
) -> dict[str, object]:
    """Collect SerpApi pages without persisting its API key."""
    if not config.serpapi_api_key:
        raise RuntimeError("Configuração obrigatória ausente: SERPAPI_API_KEY.")
    max_pages = config.serpapi_max_pages if max_pages is None else max_pages
    retries = config.http_max_retries if max_retries is None else max_retries
    request, audit = serpapi.build_request(
        query=config.serpapi_query,
        location=config.serpapi_location,
        max_pages=max_pages,
    )
    audit["max_retries"] = retries
    run_id = db.create_run(connection, "serpapi", audit, None)
    secrets = (config.theirstack_api_key, config.serpapi_api_key, config.database_url)
    pages = returned = persisted = 0
    last_status: int | None = None
    token: str | None = None
    used_tokens: set[str] = set()

    try:
        for page in range(1, max_pages + 1):
            params = dict(request)
            if token:
                params["next_page_token"] = token
                used_tokens.add(token)
            authenticated = {**params, "api_key": config.serpapi_api_key}
            save_raw = _raw_saver(
                connection,
                run_id,
                "serpapi",
                page,
                params,
                token=token,
                secrets=secrets,
            )

            last_status, payload = request_json(
                lambda: client.get(serpapi.SERPAPI_URL, params=authenticated),
                retries,
                save_raw,
            )
            collected_at = datetime.now(COLLECTION_TIMEZONE)
            response_id = save_raw(last_status, payload)
            raw_jobs = _extract(serpapi.extract_jobs, last_status, payload)
            next_token = serpapi.next_page_token(payload)
            saved = db.save_page(
                connection,
                run_id,
                response_id,
                "serpapi",
                [serpapi.map_job(job, collected_at) for job in raw_jobs],
                page=page,
                returned=len(raw_jobs),
                http_status=last_status,
                next_cursor=next_token,
                collected_at=collected_at,
                known_secrets=secrets,
            )
            pages += 1
            returned += len(raw_jobs)
            persisted += saved
            if next_token is None or page == max_pages:
                break
            if next_token in used_tokens:
                raise RuntimeError("A SerpApi repetiu um token de paginação já utilizado.")
            token = next_token

        db.finish_run(connection, run_id, last_status)
    except Exception as error:
        _fail_run(connection, run_id, pages, last_status, error, secrets)
    return _result(run_id, "serpapi", last_status, pages, returned, persisted)
