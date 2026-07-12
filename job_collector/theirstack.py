"""TheirStack request, response and job mapping helpers."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
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


def build_request(
    *,
    location_id: int = DEFAULT_LOCATION_ID,
    limit: int = 5,
    max_pages: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the provider payload and safe parameters stored for auditing."""
    if location_id <= 0:
        raise ValueError("location_id deve ser um inteiro positivo.")
    if not 1 <= limit <= 10:
        raise ValueError("limit deve estar entre 1 e 10.")
    if not 1 <= max_pages <= 2:
        raise ValueError("max_pages deve estar entre 1 e 2.")

    request: dict[str, Any] = {
        "job_country_code_or": ["BR"],
        "job_location_or": [{"id": location_id}],
        "job_title_or": list(DEFAULT_JOB_TITLES),
        "job_title_not": list(DEFAULT_EXCLUDED_JOB_TITLES),
        "posted_at_max_age_days": 30,
        "is_closed": False,
        "limit": limit,
        "offset": 0,
        "include_total_results": True,
        "blur_company_data": False,
    }
    default_region = location_id == DEFAULT_LOCATION_ID
    audit = {
        "provider": "theirstack",
        "strategy_name": "state-validation" if default_region else "structured-location",
        "sample_region": ("sao-paulo-state-validation" if default_region else "custom-location-id"),
        "requested_location_ids": [location_id],
        "remote_filter": "all",
        "job_country_code_or": ["BR"],
        "job_title_or": list(DEFAULT_JOB_TITLES),
        "job_title_not": list(DEFAULT_EXCLUDED_JOB_TITLES),
        "posted_at_max_age_days": 30,
        "is_closed": False,
        "limit": limit,
        "max_pages": max_pages,
        "preview": False,
        "blur_company_data": False,
        "include_total_results": True,
    }
    return request, audit


def extract_jobs(http_status: int, payload: object) -> list[object]:
    """Return TheirStack jobs from an already-persisted response."""
    if not 200 <= http_status < 300:
        raise ValueError(f"A TheirStack rejeitou a requisição (HTTP {http_status}).")
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("A TheirStack retornou um erro ou uma resposta desconhecida.")
    jobs = payload.get("data")
    if not isinstance(jobs, list):
        raise ValueError("A TheirStack retornou uma resposta desconhecida.")
    return jobs


def map_job(raw: object) -> dict[str, Any]:
    """Map observed common fields and retain the complete sanitized item."""
    job = raw if isinstance(raw, dict) else {}
    external_id = job.get("id")
    publication = parse_exact_publication_date(job.get("date_posted"))
    return {
        "source": "theirstack",
        "external_id": (
            sanitize_text(str(external_id).strip())
            if isinstance(external_id, (str, int)) and str(external_id).strip()
            else None
        ),
        "title": _text(job.get("job_title")),
        "company": _text(job.get("company")),
        "location": _text(job.get("location")),
        "description": _text(job.get("description")),
        "published_at": _date(job.get("date_posted")),
        "published_at_text": None,
        **publication,
        "source_url": _text(job.get("source_url")) or _text(job.get("url")),
        "raw_payload": sanitize(raw),
    }


def parse_exact_publication_date(value: object) -> dict[str, date | str | None]:
    """Read the provider's literal calendar day without timezone conversion."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"published_date": None, "publication_date_source": "missing"}
    if not isinstance(value, str):
        return {"published_date": None, "publication_date_source": "unrecognized"}

    text = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", text):
        return {"published_date": None, "publication_date_source": "unrecognized"}
    try:
        published_date = date.fromisoformat(text[:10])
        if "T" in text:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return {"published_date": None, "publication_date_source": "unrecognized"}
    return {
        "published_date": published_date,
        "publication_date_source": "theirstack_exact",
    }


def next_offset(payload: object, current_offset: int, limit: int) -> int | None:
    """Return the next offset unless this is the last observed page."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return None
    returned = len(payload["data"])
    if returned < limit:
        return None
    candidate = current_offset + returned
    metadata = payload.get("metadata")
    total = metadata.get("total_results") if isinstance(metadata, dict) else None
    if isinstance(total, (int, float)) and candidate >= total:
        return None
    return candidate


def _text(value: object) -> str | None:
    return sanitize_text(value.strip()) if isinstance(value, str) and value.strip() else None


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
