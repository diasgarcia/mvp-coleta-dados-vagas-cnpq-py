"""SerpApi Google Jobs request, response and job mapping helpers."""

from __future__ import annotations

from typing import Any

from job_collector.sanitize import sanitize, sanitize_text

SERPAPI_URL = "https://serpapi.com/search.json"
CANONICAL_LOCATION = "Sao Paulo,State of Sao Paulo,Brazil"
_SAO_PAULO_ALIAS = "São Paulo, SP, Brazil"
_EMPTY_MESSAGE = "google hasn't returned any results for this query."


def normalize_location(location: str) -> str:
    """Normalize the validated São Paulo alias and keep custom locations."""
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location não pode ser vazia.")
    value = location.strip()
    if value.casefold() in {_SAO_PAULO_ALIAS.casefold(), CANONICAL_LOCATION.casefold()}:
        return CANONICAL_LOCATION
    return value


def build_request(
    *,
    query: str = "software engineer",
    location: str = CANONICAL_LOCATION,
    max_pages: int = 1,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build safe parameters; authentication is added only by the collector."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query não pode ser vazia.")
    if not 1 <= max_pages <= 2:
        raise ValueError("max_pages deve estar entre 1 e 2.")

    canonical_location = normalize_location(location)
    request = {
        "engine": "google_jobs",
        "q": query.strip(),
        "location": canonical_location,
        "google_domain": "google.com.br",
        "gl": "br",
        "hl": "pt",
    }
    default_region = canonical_location == CANONICAL_LOCATION
    audit = {
        "provider": "serpapi",
        "strategy_name": ("city-origin-validation" if default_region else "custom-location-origin"),
        "sample_region": "sao-paulo-capital" if default_region else "custom-location",
        "query": query.strip(),
        "canonical_location": canonical_location,
        "max_pages": max_pages,
        "gl": "br",
        "hl": "pt",
        "google_domain": "google.com.br",
    }
    return request, audit


def extract_jobs(http_status: int, payload: object) -> list[object]:
    """Return jobs, including the provider's explicit successful empty result."""
    if not 200 <= http_status < 300:
        raise ValueError(f"A SerpApi rejeitou a requisição (HTTP {http_status}).")
    if not isinstance(payload, dict):
        raise ValueError("A SerpApi retornou uma resposta desconhecida.")

    metadata = payload.get("search_metadata")
    status = metadata.get("status") if isinstance(metadata, dict) else None
    normalized_status = status.strip().casefold() if isinstance(status, str) else None
    error = payload.get("error")
    normalized_error = error.strip().casefold() if isinstance(error, str) else None

    if normalized_status != "success":
        raise ValueError("A SerpApi retornou uma busca sem status Success.")
    if normalized_error and normalized_error != _EMPTY_MESSAGE:
        raise ValueError("A SerpApi informou um erro no corpo da resposta.")

    jobs = payload.get("jobs_results")
    if isinstance(jobs, list):
        return jobs

    information = payload.get("search_information")
    state = information.get("jobs_results_state") if isinstance(information, dict) else None
    explicit_empty = isinstance(state, str) and state.strip().casefold() == "fully empty"
    if normalized_error == _EMPTY_MESSAGE or explicit_empty:
        return []
    raise ValueError("A SerpApi retornou uma resposta desconhecida.")


def map_job(raw: object) -> dict[str, Any]:
    """Map observed common fields and retain the complete sanitized item."""
    job = raw if isinstance(raw, dict) else {}
    external_id = job.get("job_id")
    return {
        "source": "serpapi",
        "external_id": (
            sanitize_text(str(external_id).strip())
            if isinstance(external_id, (str, int)) and str(external_id).strip()
            else None
        ),
        "title": _text(job.get("title")) or _text(job.get("job_title")),
        "company": _text(job.get("company_name")),
        "location": _text(job.get("location")),
        "description": _text(job.get("description")),
        "published_at": None,
        "published_at_text": _published_at_text(job),
        "source_url": _source_url(job),
        "raw_payload": sanitize(raw),
    }


def next_page_token(payload: object) -> str | None:
    """Return the opaque next-page token without interpreting it."""
    if not isinstance(payload, dict):
        return None
    pagination = payload.get("serpapi_pagination")
    token = pagination.get("next_page_token") if isinstance(pagination, dict) else None
    return token.strip() if isinstance(token, str) and token.strip() else None


def _published_at_text(job: dict[str, Any]) -> str | None:
    detected = job.get("detected_extensions")
    if isinstance(detected, dict):
        posted_at = _text(detected.get("posted_at"))
        if posted_at:
            return posted_at

    extensions = job.get("extensions")
    if not isinstance(extensions, list):
        return None
    for value in extensions:
        text = _text(value)
        if text and (
            text.casefold().startswith("há ")
            or text.casefold() in {"hoje", "ontem", "today", "yesterday"}
            or text.casefold().endswith(" ago")
        ):
            return text
    return None


def _source_url(job: dict[str, Any]) -> str | None:
    source_link = _text(job.get("source_link"))
    if source_link:
        return source_link
    options = job.get("apply_options")
    if isinstance(options, list):
        for option in options:
            if isinstance(option, dict) and (link := _text(option.get("link"))):
                return link
    return _text(job.get("share_link"))


def _text(value: object) -> str | None:
    return sanitize_text(value.strip()) if isinstance(value, str) and value.strip() else None
