"""Pure aggregation and JSON export for a monthly collection round."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_collector.sanitize import sanitize

SOURCES = ("theirstack", "serpapi")
RESULT_FILENAMES = {
    "summary": "summary.json",
    "theirstack": "theirstack.json",
    "serpapi": "serpapi.json",
    "unique_jobs": "unique_jobs.json",
}
_TRACKING_QUERY_PARAMS = {
    "fbclid",
    "gclid",
    "ref",
    "referrer",
    "source",
}


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sortable(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_id(row: Mapping[str, Any]) -> str:
    return _sortable(row.get("id"))


def _run_sort_key(run: Mapping[str, Any]) -> tuple[str, str]:
    return (_sortable(run.get("started_at")), _row_id(run))


def _response_sort_key(response: Mapping[str, Any]) -> tuple[int, str, str]:
    page = response.get("page_number")
    return (
        page if isinstance(page, int) else 0,
        _sortable(response.get("collected_at")),
        _row_id(response),
    )


def _job_sort_key(job: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        _sortable(job.get("source")),
        _sortable(job.get("external_id")),
        _sortable(job.get("collected_at")),
        _row_id(job),
    )


def _query_params(run: Mapping[str, Any]) -> Mapping[str, Any]:
    params = run.get("query_params")
    return params if isinstance(params, Mapping) else {}


def normalize_match_text(value: object) -> str:
    """Normalize a common job field for conservative duplicate comparison."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value).casefold())
    without_accents = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", without_accents).strip()


def canonicalize_public_url(value: object) -> str | None:
    """Canonicalize a public HTTP URL without discarding functional query fields."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname.casefold()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        port = parsed.port
        if port and not (
            (parsed.scheme.casefold() == "http" and port == 80)
            or (parsed.scheme.casefold() == "https" and port == 443)
        ):
            hostname = f"{hostname}:{port}"
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        if path != "/":
            path = path.rstrip("/")
        query = sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in _TRACKING_QUERY_PARAMS
        )
        return urlunsplit(("", hostname, path, urlencode(query), ""))[2:]
    except ValueError:
        return None


def _unique_job_key(job: Mapping[str, Any], position: int) -> tuple[str, str, str]:
    source = _sortable(job.get("source"))
    external_id = job.get("external_id")
    if external_id is not None and str(external_id).strip():
        return (source, "external_id", str(external_id))
    row_id = job.get("id")
    return (source, "row", str(row_id) if row_id is not None else f"position:{position}")


def deduplicate_within_source(
    jobs: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return one deterministic view per source/external ID without changing occurrences."""
    region_by_run = {str(run.get("id")): _query_params(run).get("sample_region") for run in runs}
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for position, job in enumerate(sorted(jobs, key=_job_sort_key)):
        groups[_unique_job_key(job, position)].append(job)

    unique_jobs: list[dict[str, Any]] = []
    for key in sorted(groups):
        occurrences = sorted(groups[key], key=_job_sort_key)
        representative = occurrences[0]
        run_ids = sorted({str(item.get("collection_run_id")) for item in occurrences})
        regions = sorted(
            {
                str(region_by_run[run_id])
                for run_id in run_ids
                if region_by_run.get(run_id) is not None
            }
        )
        unique_jobs.append(
            {
                "source": representative.get("source"),
                "external_id": representative.get("external_id"),
                "title": representative.get("title"),
                "company": representative.get("company"),
                "location": representative.get("location"),
                "published_date": representative.get("published_date"),
                "publication_date_source": representative.get("publication_date_source"),
                "published_at_text": representative.get("published_at_text"),
                "source_url": representative.get("source_url"),
                "sample_region": regions[0] if regions else None,
                "collection_run_id": representative.get("collection_run_id"),
                "collected_at": representative.get("collected_at"),
                "run_ids": run_ids,
                "sample_regions": regions,
                "occurrence_count": len(occurrences),
            }
        )
    return unique_jobs


def _job_reference(job: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "collection_run_id": job.get("collection_run_id"),
        "external_id": job.get("external_id"),
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "source_url": job.get("source_url"),
    }


def find_potential_cross_source_duplicates(
    unique_jobs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """List conservative cross-source matches; never remove or merge a job."""
    theirstack = [item for item in unique_jobs if item.get("source") == "theirstack"]
    serpapi = [item for item in unique_jobs if item.get("source") == "serpapi"]
    duplicates: list[dict[str, Any]] = []

    for their_job in theirstack:
        their_url = canonicalize_public_url(their_job.get("source_url"))
        their_fields = tuple(
            normalize_match_text(their_job.get(field)) for field in ("title", "company", "location")
        )
        for serp_job in serpapi:
            serp_url = canonicalize_public_url(serp_job.get("source_url"))
            reason: str | None = None
            if their_url and their_url == serp_url:
                reason = "canonical_url"
            else:
                serp_fields = tuple(
                    normalize_match_text(serp_job.get(field))
                    for field in ("title", "company", "location")
                )
                if all(their_fields) and their_fields == serp_fields:
                    reason = "normalized_title_company_location"
            if reason:
                duplicates.append(
                    {
                        "reason": reason,
                        "theirstack": _job_reference(their_job),
                        "serpapi": _job_reference(serp_job),
                    }
                )
    return sorted(
        duplicates,
        key=lambda item: (
            item["reason"],
            _sortable(item["theirstack"].get("external_id")),
            _sortable(item["serpapi"].get("external_id")),
        ),
    )


def _query_status(run: Mapping[str, Any]) -> dict[str, Any]:
    params = _query_params(run)
    return {
        "run_id": run.get("id"),
        "source": run.get("source"),
        "sample_region": params.get("sample_region"),
        "query": params.get("query"),
        "requested_location_ids": params.get("requested_location_ids"),
        "status": run.get("status"),
        "returned_count": run.get("returned_count", 0),
        "persisted_count": run.get("persisted_count", 0),
        "http_status": run.get("http_status"),
        "error_message": run.get("error_message"),
    }


def _completed_regions(
    runs: Sequence[Mapping[str, Any]],
    planned_regions: Sequence[str],
    planned_queries: Mapping[str, int],
) -> list[str]:
    successful: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for run in runs:
        params = _query_params(run)
        region = params.get("sample_region")
        if region is not None and run.get("status") == "success":
            successful[str(region)].add(
                (
                    _sortable(run.get("source")),
                    _sortable(params.get("query")),
                    _sortable(params.get("requested_location_ids")),
                )
            )
    expected = sum(planned_queries.values()) // len(planned_regions) if planned_regions else 0
    if expected:
        return sorted(region for region in planned_regions if len(successful[region]) >= expected)
    return sorted(successful)


def _source_payload(
    source: str,
    round_id: str,
    generated_at: str,
    runs: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for run in sorted((item for item in runs if item.get("source") == source), key=_run_sort_key):
        run_id = str(run.get("id"))
        params = _query_params(run)
        entries.append(
            {
                "run": dict(run),
                "sample_region": params.get("sample_region"),
                "query": params.get("query"),
                "responses": sorted(
                    [
                        dict(item)
                        for item in responses
                        if str(item.get("collection_run_id")) == run_id
                    ],
                    key=_response_sort_key,
                ),
                "jobs": sorted(
                    [dict(item) for item in jobs if str(item.get("collection_run_id")) == run_id],
                    key=_job_sort_key,
                ),
            }
        )
    return {
        "round_id": round_id,
        "generated_at": generated_at,
        "repository": "python",
        "source": source,
        "runs": entries,
    }


def build_monthly_payloads(
    round_id: str,
    runs: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
    jobs: Sequence[Mapping[str, Any]],
    *,
    generated_at: str | None = None,
    timezone: str = "America/Sao_Paulo",
    planned_regions: Sequence[str] = (),
    planned_queries: Mapping[str, int] | None = None,
    limits_used: Mapping[str, Any] | None = None,
    skipped_queries: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Build all four monthly result documents from rows already read from PostgreSQL."""
    timestamp = generated_at or _iso_now()
    sorted_runs = sorted((dict(item) for item in runs), key=_run_sort_key)
    sorted_responses = sorted((dict(item) for item in responses), key=_response_sort_key)
    sorted_jobs = sorted((dict(item) for item in jobs), key=_job_sort_key)
    unique_jobs = deduplicate_within_source(sorted_jobs, sorted_runs)
    potential_duplicates = find_potential_cross_source_duplicates(unique_jobs)

    run_statuses = [_query_status(run) for run in sorted_runs]
    source_occurrences = Counter(_sortable(job.get("source")) for job in sorted_jobs)
    source_unique = Counter(_sortable(job.get("source")) for job in unique_jobs)
    date_sources = Counter(
        _sortable(job.get("publication_date_source")) or "missing" for job in sorted_jobs
    )
    failed_runs = [item for item in run_statuses if item["status"] in {"failed", "partial"}]
    requested_queries = {key: value for key, value in sorted((planned_queries or {}).items())}
    region_names = sorted(str(region) for region in planned_regions)
    skipped_details = sorted(
        (dict(item) for item in skipped_queries),
        key=lambda item: (
            _sortable(item.get("source")),
            _sortable(item.get("sample_region")),
            _sortable(item.get("query")),
        ),
    )

    summary = {
        "round_id": round_id,
        "generated_at": timestamp,
        "timezone": timezone,
        "regions_planned": region_names,
        "regions_completed": _completed_regions(sorted_runs, region_names, requested_queries),
        "queries_planned": requested_queries,
        "queries_executed": {
            source: sum(run.get("source") == source for run in sorted_runs) for source in SOURCES
        },
        "queries_skipped": len(skipped_queries),
        "skipped_query_details": skipped_details,
        "queries_failed": len(failed_runs),
        "limits_used": {key: value for key, value in sorted((limits_used or {}).items())},
        "total_collection_runs": len(sorted_runs),
        "total_raw_responses": len(sorted_responses),
        "raw_occurrences": len(sorted_jobs),
        "raw_occurrences_by_source": {source: source_occurrences[source] for source in SOURCES},
        "unique_within_source": {source: source_unique[source] for source in SOURCES},
        "repetitions_within_round": len(sorted_jobs) - len(unique_jobs),
        "potential_cross_source_duplicates": len(potential_duplicates),
        "publication_dates": {
            "exact": date_sources["theirstack_exact"],
            "estimated": date_sources["serpapi_estimated"],
            "missing": date_sources["missing"],
            "unrecognized": date_sources["unrecognized"],
        },
        "run_ids": [_row_id(run) for run in sorted_runs],
        "query_statuses": run_statuses,
    }
    unique_payload = {
        "round_id": round_id,
        "generated_at": timestamp,
        "repository": "python",
        "deduplication_key": "source + external_id",
        "jobs": unique_jobs,
        "potential_cross_source_duplicates": potential_duplicates,
    }
    return {
        "summary": summary,
        "theirstack": _source_payload(
            "theirstack", round_id, timestamp, sorted_runs, sorted_responses, sorted_jobs
        ),
        "serpapi": _source_payload(
            "serpapi", round_id, timestamp, sorted_runs, sorted_responses, sorted_jobs
        ),
        "unique_jobs": unique_payload,
    }


def _write_json(path: Path, payload: Any, known_secrets: Sequence[str | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
                sanitize(payload, known_secrets),
                file,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            file.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_monthly_results(
    round_id: str,
    payloads: Mapping[str, Any],
    *,
    output_root: Path = Path("results/monthly"),
    known_secrets: Sequence[str | None] = (),
) -> dict[str, Path]:
    """Write fixed monthly filenames atomically after a second sanitization pass."""
    round_directory = output_root / round_id
    paths: dict[str, Path] = {}
    for name, filename in RESULT_FILENAMES.items():
        if name not in payloads:
            raise ValueError(f"Payload mensal ausente: {name}.")
        path = round_directory / filename
        _write_json(path, payloads[name], known_secrets)
        paths[name] = path
    return paths
