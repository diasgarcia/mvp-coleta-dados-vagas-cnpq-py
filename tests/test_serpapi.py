import json
from pathlib import Path

import pytest

from job_collector import serpapi

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_build_request_normalizes_only_confirmed_alias_and_omits_key() -> None:
    params, audit = serpapi.build_request(location="São Paulo, SP, Brazil")

    assert params == {
        "engine": "google_jobs",
        "q": "software engineer",
        "location": serpapi.CANONICAL_LOCATION,
        "google_domain": "google.com.br",
        "gl": "br",
        "hl": "pt",
    }
    assert audit["canonical_location"] == serpapi.CANONICAL_LOCATION
    assert audit["strategy_name"] == "city-origin-validation"
    assert audit["max_pages"] == 1
    assert "api_key" not in params
    assert "api_key" not in audit
    assert serpapi.normalize_location("Campinas,State of Sao Paulo,Brazil") == (
        "Campinas,State of Sao Paulo,Brazil"
    )
    _, custom_audit = serpapi.build_request(location="Campinas,State of Sao Paulo,Brazil")
    assert custom_audit["strategy_name"] == "custom-location-origin"
    assert custom_audit["sample_region"] == "custom-location"


def test_map_job_uses_primary_fields_and_preserves_relative_date_and_raw() -> None:
    response = load_fixture("serpapi_response.json")
    assert isinstance(response, dict)
    raw = response["jobs_results"][0]

    job = serpapi.map_job(raw)

    assert job["source"] == "serpapi"
    assert job["external_id"] == "serp-fixture-1"
    assert job["title"] == "Engenheira de Software"
    assert job["company"] == "Companhia Exemplo"
    assert job["location"] == "São Paulo, SP"
    assert job["description"] == "Construção de aplicações internas."
    assert job["published_at"] is None
    assert job["published_at_text"] == "há 18 dias"
    assert job["source_url"] == "https://careers.example.test/jobs/serp-fixture-1"
    assert job["raw_payload"]["unknown_provider_field"]["kept"] is True
    assert job["raw_payload"]["unknown_provider_field"]["access_token"] == "[REDACTED]"

    apply_job = serpapi.map_job(
        {
            "job_id": "apply",
            "job_title": "QA Engineer",
            "apply_options": [{"link": "https://apply.example.test/job"}],
            "share_link": "https://share.example.test/job",
            "extensions": ["há 5 dias"],
        }
    )
    share_job = serpapi.map_job(
        {"job_id": "share", "share_link": "https://share.example.test/only"}
    )
    shift_job = serpapi.map_job({"job_id": "shift", "extensions": ["Day shift"]})

    assert apply_job["title"] == "QA Engineer"
    assert apply_job["source_url"] == "https://apply.example.test/job"
    assert apply_job["published_at_text"] == "há 5 dias"
    assert share_job["source_url"] == "https://share.example.test/only"
    assert shift_job["published_at_text"] is None


def test_classify_response_covers_results_empty_provider_error_and_unknown() -> None:
    response = load_fixture("serpapi_response.json")
    empty = load_fixture("serpapi_empty_response.json")
    provider_error = load_fixture("serpapi_provider_error.json")

    result_kind, jobs, error = serpapi.classify_response(200, response)
    assert (result_kind, len(jobs), error) == ("success_with_results", 2, None)
    assert serpapi.classify_response(200, empty) == ("success_empty", [], None)
    assert serpapi.classify_response(200, provider_error)[0] == "provider_error"
    assert serpapi.classify_response(429, provider_error)[0] == "provider_error"
    assert serpapi.classify_response(200, {"search_metadata": {}})[0] == ("unknown_response")


def test_next_page_token_is_opaque_and_repeated_token_is_rejected() -> None:
    response = load_fixture("serpapi_response.json")
    token = serpapi.next_page_token(response)

    assert token == "opaque-fixture-token=="
    with pytest.raises(ValueError, match="repetiu um token"):
        serpapi.next_page_token(response, {"opaque-fixture-token=="})
    assert serpapi.next_page_token({"serpapi_pagination": {}}) is None
