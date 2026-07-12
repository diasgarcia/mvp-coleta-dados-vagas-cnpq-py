import json
from datetime import UTC, datetime
from pathlib import Path

from job_collector import theirstack

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_build_request_uses_validated_defaults_and_safe_audit_params() -> None:
    payload, audit = theirstack.build_request()

    assert payload["job_location_or"] == [{"id": 3_448_433}]
    assert payload["job_country_code_or"] == ["BR"]
    assert payload["job_title_or"] == theirstack.DEFAULT_JOB_TITLES
    assert payload["job_title_not"] == theirstack.DEFAULT_EXCLUDED_JOB_TITLES
    assert payload["limit"] == 5
    assert payload["offset"] == 0
    assert payload["blur_company_data"] is False
    assert payload["include_total_results"] is True
    assert audit["requested_location_ids"] == [3_448_433]
    assert audit["strategy_name"] == "state-validation"
    assert audit["remote_filter"] == "all"
    assert "api_key" not in audit

    _, custom_audit = theirstack.build_request(location_id=123_456)
    assert custom_audit["strategy_name"] == "structured-location"
    assert custom_audit["sample_region"] == "custom-location-ids"


def test_build_request_honors_preview_query_and_remote_filter() -> None:
    for preview, remote, expected_filter in [
        (False, True, "remote"),
        (True, False, "non-remote"),
    ]:
        payload, audit = theirstack.build_request(
            query="software engineer", preview=preview, remote=remote
        )

        assert payload["job_title_or"] == ["software engineer"]
        assert payload["blur_company_data"] is preview
        assert payload["remote"] is remote
        assert audit["preview"] is preview
        assert audit["remote_filter"] == expected_filter


def test_map_job_preserves_raw_fields_and_maps_optional_values() -> None:
    response = load_fixture("theirstack_response.json")
    assert isinstance(response, dict)
    raw = response["data"][0]

    job = theirstack.map_job(raw)

    assert job["source"] == "theirstack"
    assert job["external_id"] == "9001"
    assert job["title"] == "Software Engineer"
    assert job["company"] == "Empresa Exemplo"
    assert job["location"] == "São Paulo, SP"
    assert job["description"] == "Desenvolvimento de serviços internos."
    assert job["published_at"] == datetime(2026, 7, 10, 12, 30, tzinfo=UTC)
    assert job["published_at_text"] is None
    assert job["source_url"] == "https://jobs.example.test/jobs/9001"
    assert job["raw_payload"]["technology_slugs"] == ["python", "postgresql"]
    assert job["raw_payload"]["company_object"]["technology_slugs"] == ["salesforce"]
    assert job["raw_payload"]["state_code"] == "RJ"
    assert job["raw_payload"]["locations"][0]["id"] == 3_448_433
    assert job["raw_payload"]["unknown_provider_field"]["kept"] is True
    assert job["raw_payload"]["unknown_provider_field"]["api_key"] == "[REDACTED]"

    minimal = theirstack.map_job(response["data"][1])
    assert minimal["external_id"] == "job-minimal-2"
    assert minimal["title"] == "QA Engineer"
    assert minimal["company"] is None
    assert minimal["location"] is None
    assert minimal["published_at"] is None


def test_classify_response_covers_results_empty_error_and_unknown() -> None:
    response = load_fixture("theirstack_response.json")
    empty = load_fixture("theirstack_empty_response.json")

    result_kind, jobs, error = theirstack.classify_response(200, response)
    assert (result_kind, len(jobs), error) == ("success_with_results", 2, None)
    assert theirstack.classify_response(200, empty) == ("success_empty", [], None)
    assert theirstack.classify_response(200, {"metadata": {}})[0] == "unknown_response"
    assert theirstack.classify_response(401, {"error": "fixture"})[0] == "provider_error"
    assert theirstack.classify_response(200, {"error": "fixture"})[0] == "provider_error"


def test_next_offset_stops_on_empty_short_or_known_total() -> None:
    full_page = {"data": [{}, {}], "metadata": {"total_results": 10}}
    assert theirstack.next_offset(full_page, current_offset=0, limit=2) == 2
    assert theirstack.next_offset({"data": []}, current_offset=0, limit=2) is None
    assert theirstack.next_offset({"data": [{}]}, current_offset=0, limit=2) is None
    assert (
        theirstack.next_offset(
            {"data": [{}, {}], "metadata": {"total_results": 2}},
            current_offset=0,
            limit=2,
        )
        is None
    )
