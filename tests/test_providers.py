from collections.abc import Callable
from typing import Any

import pytest

from job_collector import serpapi, theirstack


def test_theirstack_request_is_small_structured_and_safe() -> None:
    request, audit = theirstack.build_request(location_id=3_448_433, limit=2, max_pages=1)

    assert request["job_location_or"] == [{"id": 3_448_433}]
    assert request["job_country_code_or"] == ["BR"]
    assert request["limit"] == 2
    assert request["offset"] == 0
    assert "software engineer" in request["job_title_or"]
    assert "business developer" in request["job_title_not"]
    assert audit["max_pages"] == 1
    assert "api_key" not in audit


def test_theirstack_maps_observed_fields_and_complete_sanitized_raw(
    fixture_json: Callable[[str], Any],
) -> None:
    raw = fixture_json("theirstack_response.json")["data"][0]

    job = theirstack.map_job(raw)

    assert job["external_id"] == "9001"
    assert job["title"] == "Software Engineer"
    assert job["published_at"].isoformat() == "2026-07-10T12:30:00+00:00"
    assert job["raw_payload"]["technology_slugs"] == ["python", "postgresql"]
    assert job["raw_payload"]["company_object"]["technology_slugs"] == ["salesforce"]
    assert job["raw_payload"]["unknown_provider_field"] == {
        "kept": True,
        "api_key": "[REDACTED]",
    }


def test_theirstack_extracts_results_and_stops_short_or_finished_pages(
    fixture_json: Callable[[str], Any],
) -> None:
    payload = fixture_json("theirstack_response.json")

    assert len(theirstack.extract_jobs(200, payload)) == 2
    assert theirstack.next_offset(payload, 0, 2) == 2
    assert theirstack.extract_jobs(200, {"data": []}) == []
    assert theirstack.next_offset({"data": [{}]}, 0, 2) is None
    with pytest.raises(ValueError, match="HTTP 401"):
        theirstack.extract_jobs(401, {"error": "unauthorized"})


def test_serpapi_request_mapping_empty_success_and_token(
    fixture_json: Callable[[str], Any],
) -> None:
    request, audit = serpapi.build_request(location="São Paulo, SP, Brazil", max_pages=1)
    payload = fixture_json("serpapi_response.json")
    job = serpapi.map_job(payload["jobs_results"][0])

    assert request["location"] == serpapi.CANONICAL_LOCATION
    assert request["hl"] == "pt"
    assert "api_key" not in request and "api_key" not in audit
    assert job["title"] == "Engenheira de Software"
    assert job["published_at"] is None
    assert job["published_at_text"] == "há 18 dias"
    assert job["source_url"] == "https://careers.example.test/jobs/serp-fixture-1"
    assert job["raw_payload"]["unknown_provider_field"]["access_token"] == "[REDACTED]"
    assert len(serpapi.extract_jobs(200, payload)) == 2
    assert serpapi.extract_jobs(200, fixture_json("serpapi_empty_response.json")) == []
    assert serpapi.next_page_token(payload) == "opaque-fixture-token=="
    with pytest.raises(ValueError, match="status Success"):
        serpapi.extract_jobs(200, {"search_metadata": {"status": "Error"}, "error": "bad"})
