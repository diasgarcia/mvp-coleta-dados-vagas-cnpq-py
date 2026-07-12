"""Pure request, response and mapping helpers for SerpApi Google Jobs."""

from __future__ import annotations

import re
from typing import Any

from job_collector.sanitize import sanitize, sanitize_text

SERPAPI_URL = "https://serpapi.com/search.json"
CANONICAL_LOCATION = "Sao Paulo,State of Sao Paulo,Brazil"
_SAO_PAULO_ALIAS = "São Paulo, SP, Brazil"
_EMPTY_MESSAGE = "google hasn't returned any results for this query."
_POSTED_AT_PATTERN = re.compile(
    r"(?:\b(?:today|yesterday|hoje|ontem)\b|"
    r"(?:\bhá\s+)?\b\d+\+?\s+(?:day|days|hour|hours|week|weeks|month|months|"
    r"dia|dias|hora|horas|semana|semanas|mês|meses)\b(?:\s+ago\b)?)",
    re.IGNORECASE,
)


def normalize_location(location: str) -> str:
    """Normalize only the explicitly validated São Paulo alias."""
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location não pode ser vazia.")
    value = location.strip()
    if value.casefold() in {
        _SAO_PAULO_ALIAS.casefold(),
        CANONICAL_LOCATION.casefold(),
    }:
        return CANONICAL_LOCATION
    return value


def build_request(
    *,
    query: str = "software engineer",
    location: str = CANONICAL_LOCATION,
    max_pages: int = 1,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build safe request parameters; authentication is added only by HTTP code."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query não pode ser vazia.")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 2:
        raise ValueError("max_pages deve estar entre 1 e 2.")

    canonical_location = normalize_location(location)
    normalized_query = query.strip()
    is_sao_paulo_capital_profile = canonical_location == CANONICAL_LOCATION
    request_params = {
        "engine": "google_jobs",
        "q": normalized_query,
        "location": canonical_location,
        "google_domain": "google.com.br",
        "gl": "br",
        "hl": "pt",
    }
    audit_params: dict[str, Any] = {
        "provider": "serpapi",
        "strategy_name": (
            "city-origin-validation" if is_sao_paulo_capital_profile else "custom-location-origin"
        ),
        "sample_region": (
            "sao-paulo-capital" if is_sao_paulo_capital_profile else "custom-location"
        ),
        "query": normalized_query,
        "canonical_location": canonical_location,
        "max_pages": max_pages,
        "gl": "br",
        "hl": "pt",
        "google_domain": "google.com.br",
    }
    return request_params, audit_params


def classify_response(http_status: int, payload: object) -> tuple[str, list[object], str | None]:
    """Classify an already-preserved response and return its raw job items."""
    if not 200 <= http_status < 300:
        return (
            "provider_error",
            [],
            f"A SerpApi rejeitou a requisição (HTTP {http_status}).",
        )
    if not isinstance(payload, dict):
        return "unknown_response", [], _unknown_message(payload)

    metadata = payload.get("search_metadata")
    status_value = metadata.get("status") if isinstance(metadata, dict) else None
    status = _normalized_text(status_value)
    error = payload.get("error")
    informational_empty = _normalized_text(error) == _EMPTY_MESSAGE

    if status is not None and status != "success":
        return (
            "provider_error",
            [],
            "A SerpApi retornou uma busca com status diferente de Success.",
        )
    if _has_meaningful_value(error) and not informational_empty:
        return (
            "provider_error",
            [],
            "A SerpApi informou um erro no corpo da resposta HTTP 200.",
        )

    jobs = payload.get("jobs_results")
    if isinstance(jobs, list) and jobs and status == "success":
        return "success_with_results", jobs, None

    information = payload.get("search_information")
    jobs_state = (
        _normalized_text(information.get("jobs_results_state"))
        if isinstance(information, dict)
        else None
    )
    explicit_empty = isinstance(jobs, list) and not jobs
    absent_jobs = jobs is None
    if status == "success" and (
        explicit_empty or (absent_jobs and (jobs_state == "fully empty" or informational_empty))
    ):
        return "success_empty", [], None

    return "unknown_response", [], _unknown_message(payload)


def map_job(raw: object) -> dict[str, Any]:
    """Map optional common fields while retaining the complete sanitized item."""
    raw_payload = sanitize(raw)
    job = raw if isinstance(raw, dict) else {}
    return {
        "source": "serpapi",
        "external_id": _identifier(job, "job_id", "id", "external_id"),
        "title": _text(job, "title", "job_title"),
        "company": _text(job, "company_name", "company"),
        "location": _text(job, "location", "job_location"),
        "description": _text(job, "description", "job_description"),
        "published_at": None,
        "published_at_text": _published_at_text(job),
        "source_url": _source_url(job),
        "raw_payload": raw_payload,
    }


def next_page_token(payload: object, used_tokens: set[str] | None = None) -> str | None:
    """Read an opaque token and reject it when the caller already used it."""
    if not isinstance(payload, dict):
        return None
    pagination = payload.get("serpapi_pagination")
    token = pagination.get("next_page_token") if isinstance(pagination, dict) else None
    if not isinstance(token, str) or not token.strip():
        return None
    if used_tokens is not None and token in used_tokens:
        raise ValueError("A SerpApi repetiu um token de paginação já utilizado.")
    return token


def _source_url(job: dict[str, Any]) -> str | None:
    source_link = _text(job, "source_link")
    if source_link is not None:
        return sanitize_text(source_link)

    apply_link = _first_link(job.get("apply_options"))
    if apply_link is not None:
        return apply_link

    fallback = _text(job, "share_link", "source_url", "job_url")
    if fallback is not None:
        return sanitize_text(fallback)
    return _first_link(job.get("related_links"))


def _first_link(value: object) -> str | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, dict):
            link = _text(item, "link", "url")
            if link is not None:
                return sanitize_text(link)
    return None


def _published_at_text(job: dict[str, Any]) -> str | None:
    detected = job.get("detected_extensions")
    if isinstance(detected, dict):
        posted_at = _text(detected, "posted_at")
        if posted_at is not None:
            return posted_at

    direct = _text(job, "posted_at", "published_at_text")
    if direct is not None:
        return direct

    extensions = job.get("extensions")
    if not isinstance(extensions, list):
        return None
    for item in extensions:
        if isinstance(item, str) and _POSTED_AT_PATTERN.search(item):
            return sanitize_text(item)
    return None


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


def _normalized_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _has_meaningful_value(value: object) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _unknown_message(payload: object) -> str:
    fields = sorted(str(key)[:64] for key in payload)[:20] if isinstance(payload, dict) else []
    detail = ", ".join(fields) if fields else "nenhum campo raiz reconhecível"
    return f"A SerpApi retornou uma resposta desconhecida (campos: {detail})."
