"""Pure request, response and mapping helpers for TheirStack."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from job_collector.sanitize import sanitize, sanitize_text

THEIRSTACK_URL = "https://api.theirstack.com/v1/jobs/search"
DEFAULT_LOCATION_ID = 3_448_433

DEFAULT_JOB_TITLES = [
    "desenvolvedor de software",
    "software developer",
    "software engineer",
    "engenheiro de software",
    "programador",
    "backend developer",
    "frontend developer",
    "full stack developer",
    "devops engineer",
    "quality assurance",
    "qa engineer",
    "analista de testes",
    "data engineer",
    "engenheiro de dados",
    "cientista de dados",
]

DEFAULT_EXCLUDED_JOB_TITLES = [
    "business developer",
    "desenvolvedor de negócios",
    "account executive",
    "sales developer",
    "sales development",
]

# Explicit aliases keep the public names self-documenting for callers without
# duplicating the lists themselves.
DEFAULT_THEIRSTACK_LOCATION_ID = DEFAULT_LOCATION_ID
DEFAULT_THEIRSTACK_JOB_TITLES = DEFAULT_JOB_TITLES
DEFAULT_THEIRSTACK_EXCLUDED_JOB_TITLES = DEFAULT_EXCLUDED_JOB_TITLES


def build_request(
    *,
    query: str | Sequence[str] | None = None,
    location_id: int | Sequence[int] = DEFAULT_LOCATION_ID,
    limit: int = 5,
    max_pages: int = 1,
    max_age_days: int = 30,
    preview: bool = False,
    remote: bool | None = None,
    include_total_results: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the provider payload and the safe parameters stored for auditing."""
    _bounded_integer("limit", limit, 1, 10)
    _bounded_integer("max_pages", max_pages, 1, 2)
    _bounded_integer("max_age_days", max_age_days, 0, 3_650)
    if not isinstance(preview, bool):
        raise ValueError("preview deve ser booleano.")
    if remote is not None and not isinstance(remote, bool):
        raise ValueError("remote deve ser booleano quando informado.")

    titles = _job_titles(query)
    location_ids = _location_ids(location_id)
    excluded_titles = list(DEFAULT_EXCLUDED_JOB_TITLES)
    is_sao_paulo_state_profile = location_ids == [DEFAULT_LOCATION_ID]

    payload: dict[str, Any] = {
        "job_country_code_or": ["BR"],
        "job_location_or": [{"id": item} for item in location_ids],
        "job_title_or": titles,
        "job_title_not": excluded_titles,
        "posted_at_max_age_days": max_age_days,
        "is_closed": False,
        "limit": limit,
        "offset": 0,
        "include_total_results": include_total_results,
        "blur_company_data": preview,
    }
    if remote is not None:
        payload["remote"] = remote

    audit_params: dict[str, Any] = {
        "provider": "theirstack",
        "strategy_name": (
            "state-validation" if is_sao_paulo_state_profile else "structured-location"
        ),
        "sample_region": (
            "sao-paulo-state-validation" if is_sao_paulo_state_profile else "custom-location-ids"
        ),
        "requested_location_ids": location_ids,
        "remote_filter": _remote_filter(remote),
        "job_country_code_or": ["BR"],
        "job_title_or": titles,
        "job_title_not": excluded_titles,
        "posted_at_max_age_days": max_age_days,
        "is_closed": False,
        "limit": limit,
        "max_pages": max_pages,
        "preview": preview,
        "blur_company_data": preview,
        "include_total_results": include_total_results,
    }
    return payload, audit_params


def classify_response(http_status: int, payload: object) -> tuple[str, list[object], str | None]:
    """Classify an already-preserved response and return its raw job items."""
    if not 200 <= http_status < 300:
        return (
            "provider_error",
            [],
            f"A TheirStack rejeitou a requisição (HTTP {http_status}).",
        )

    if not isinstance(payload, dict):
        return (
            "unknown_response",
            [],
            "A TheirStack retornou uma resposta com estrutura desconhecida.",
        )

    if _has_meaningful_value(payload.get("error")):
        return (
            "provider_error",
            [],
            "A TheirStack informou um erro no corpo da resposta.",
        )

    jobs = payload.get("data")
    if not isinstance(jobs, list):
        return (
            "unknown_response",
            [],
            "A TheirStack retornou uma resposta com estrutura desconhecida.",
        )

    if not jobs:
        return "success_empty", [], None
    return "success_with_results", jobs, None


def map_job(raw: object) -> dict[str, Any]:
    """Map optional common fields while retaining the complete sanitized item."""
    raw_payload = sanitize(raw)
    job = raw if isinstance(raw, dict) else {}

    return {
        "source": "theirstack",
        "external_id": _identifier(job, "id", "job_id", "external_id"),
        "title": _text(job, "job_title", "title", "name"),
        "company": _company(job),
        "location": _location(job),
        "description": _text(job, "description", "job_description", "description_text"),
        "published_at": _published_at(
            job, "date_posted", "published_at", "job_posted_at", "created_at"
        ),
        "published_at_text": None,
        "source_url": _url(job, "source_url", "url", "final_url", "job_url"),
        "raw_payload": raw_payload,
    }


def next_offset(payload: object, current_offset: int, limit: int) -> int | None:
    """Return the next safe offset, or None when this page ends the walk."""
    if (
        isinstance(current_offset, bool)
        or not isinstance(current_offset, int)
        or current_offset < 0
    ):
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None

    returned_count = len(payload["data"])
    if returned_count == 0 or returned_count < limit:
        return None

    candidate = current_offset + returned_count
    if candidate <= current_offset:
        return None

    metadata = payload.get("metadata")
    total_results = metadata.get("total_results") if isinstance(metadata, dict) else None
    if (
        isinstance(total_results, (int, float))
        and not isinstance(total_results, bool)
        and candidate >= total_results
    ):
        return None
    return candidate


def _job_titles(query: str | Sequence[str] | None) -> list[str]:
    if query is None:
        return list(DEFAULT_JOB_TITLES)
    values: Sequence[str] = [query] if isinstance(query, str) else query
    result = [item.strip() for item in values if isinstance(item, str) and item.strip()]
    if not result:
        raise ValueError("query deve conter ao menos um título não vazio.")
    return result


def _location_ids(location_id: int | Sequence[int]) -> list[int]:
    values = (
        [location_id]
        if isinstance(location_id, int) and not isinstance(location_id, bool)
        else location_id
    )
    if isinstance(values, (str, bytes)):
        raise ValueError("location_id deve conter IDs inteiros positivos.")
    try:
        result = list(values)
    except TypeError as error:
        raise ValueError("location_id deve conter IDs inteiros positivos.") from error
    if not result or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in result
    ):
        raise ValueError("location_id deve conter IDs inteiros positivos.")
    return result


def _bounded_integer(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{name} deve estar entre {minimum} e {maximum}.")


def _remote_filter(remote: bool | None) -> str:
    if remote is True:
        return "remote"
    if remote is False:
        return "non-remote"
    return "all"


def _has_meaningful_value(value: object) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _identifier(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        identifier = str(value).strip()
        if identifier:
            return sanitize_text(identifier)
    return None


def _text(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_text(value.strip())
    return None


def _company(job: dict[str, Any]) -> str | None:
    direct = _text(job, "company", "company_name")
    if direct is not None:
        return direct
    company = job.get("company_object")
    if not isinstance(company, dict):
        company = job.get("company")
    if isinstance(company, dict):
        return _text(company, "name", "company_name", "display_name")
    return None


def _location(job: dict[str, Any]) -> str | None:
    direct = _text(job, "location", "job_location", "short_location")
    if direct is not None:
        return direct

    for candidate in (job.get("location"), job.get("job_location")):
        if not isinstance(candidate, dict):
            continue
        display = _text(candidate, "display_name", "name", "short_location")
        if display is not None:
            return display
        parts = [_text(candidate, name) for name in ("city", "state", "country")]
        if any(parts):
            return ", ".join(part for part in parts if part is not None)

    parts = [_text(job, name) for name in ("city", "state", "country")]
    return ", ".join(part for part in parts if part is not None) or None


def _url(source: dict[str, Any], *keys: str) -> str | None:
    value = _text(source, *keys)
    return sanitize_text(value) if value is not None else None


def _published_at(source: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        parsed = _parse_datetime(source.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1_000 if abs(value) >= 100_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith(("Z", "z")):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
