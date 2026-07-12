from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
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
    assert job["published_date"] == date(2026, 7, 10)
    assert job["publication_date_source"] == "theirstack_exact"
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
    job = serpapi.map_job(
        payload["jobs_results"][0],
        datetime(2026, 7, 12, 18, 4, 12, tzinfo=timezone(timedelta(hours=-3))),
    )

    assert request["location"] == serpapi.CANONICAL_LOCATION
    assert request["hl"] == "pt"
    assert "api_key" not in request and "api_key" not in audit
    assert job["title"] == "Engenheira de Software"
    assert job["published_at"] is None
    assert job["published_date"] == date(2026, 6, 24)
    assert job["publication_date_source"] == "serpapi_estimated"
    assert job["published_at_text"] == "há 18 dias"
    assert job["source_url"] == "https://careers.example.test/jobs/serp-fixture-1"
    assert job["raw_payload"]["unknown_provider_field"]["access_token"] == "[REDACTED]"
    assert len(serpapi.extract_jobs(200, payload)) == 2
    assert serpapi.extract_jobs(200, fixture_json("serpapi_empty_response.json")) == []
    assert serpapi.next_page_token(payload) == "opaque-fixture-token=="
    with pytest.raises(ValueError, match="status Success"):
        serpapi.extract_jobs(200, {"search_metadata": {"status": "Error"}, "error": "bad"})


@pytest.mark.parametrize(
    ("text", "collected_at", "expected"),
    [
        ("hoje", datetime(2026, 7, 12, 23, 59), date(2026, 7, 12)),
        (
            "hoje",
            datetime(2026, 7, 13, 1, 30, tzinfo=UTC),
            date(2026, 7, 12),
        ),
        ("TODAY", datetime(2035, 1, 4, 2, 0), date(2035, 1, 4)),
        ("ontem", datetime(2026, 7, 12, 0, 1), date(2026, 7, 11)),
        ("yesterday", date(2026, 1, 1), date(2025, 12, 31)),
        ("há 1 hora", date(2026, 7, 12), date(2026, 7, 12)),
        ("ha 12 horas", date(2026, 7, 12), date(2026, 7, 12)),
        ("  HÁ   30   MINUTOS ", date(2026, 7, 12), date(2026, 7, 12)),
        ("3 hours ago", date(2026, 7, 12), date(2026, 7, 12)),
        ("15 minutes ago", date(2026, 7, 12), date(2026, 7, 12)),
        ("há 1 dia", date(2026, 7, 12), date(2026, 7, 11)),
        ("há 18 dias", date(2026, 7, 12), date(2026, 6, 24)),
        ("5 days ago", date(2026, 7, 12), date(2026, 7, 7)),
        ("há 1 semana", date(2026, 7, 12), date(2026, 7, 5)),
        ("há 2 semanas", date(2026, 7, 12), date(2026, 6, 28)),
        ("2 weeks ago", date(2026, 7, 12), date(2026, 6, 28)),
        ("há 1 mês", date(2026, 7, 12), date(2026, 6, 12)),
        ("há 2 meses", date(2026, 7, 12), date(2026, 5, 12)),
        ("2 months ago", date(2026, 1, 15), date(2025, 11, 15)),
        ("há 1 mês", date(2025, 3, 31), date(2025, 2, 28)),
        ("há 1 mês", date(2024, 3, 31), date(2024, 2, 29)),
    ],
)
def test_serpapi_parses_relative_publication_day_from_explicit_collection_date(
    text: str, collected_at: date | datetime, expected: date
) -> None:
    result = serpapi.parse_relative_publication_date(text, collected_at)

    assert result == {
        "published_date": expected,
        "publication_date_source": "serpapi_estimated",
    }


@pytest.mark.parametrize("text", [None, "", "   "])
def test_serpapi_marks_absent_publication_text_as_missing(text: object) -> None:
    result = serpapi.parse_relative_publication_date(text, date(1999, 12, 31))

    assert result == {"published_date": None, "publication_date_source": "missing"}


def test_serpapi_preserves_unknown_publication_text_without_dropping_job() -> None:
    raw = {
        "job_id": "unknown-date",
        "title": "Software Engineer",
        "detected_extensions": {"posted_at": "publicada recentemente"},
    }

    job = serpapi.map_job(raw, datetime(1987, 4, 3, 15, 30))

    assert job["published_at_text"] == "publicada recentemente"
    assert job["published_date"] is None
    assert job["publication_date_source"] == "unrecognized"
    for text in ("há 999999999999 dias", "999999999999 months ago"):
        assert serpapi.parse_relative_publication_date(text, date(2026, 7, 12)) == {
            "published_date": None,
            "publication_date_source": "unrecognized",
        }


@pytest.mark.parametrize(
    ("value", "expected_date", "expected_source"),
    [
        ("2026-07-12", date(2026, 7, 12), "theirstack_exact"),
        ("2026-07-12T23:59:59-12:00", date(2026, 7, 12), "theirstack_exact"),
        (None, None, "missing"),
        ("", None, "missing"),
        ("not-a-date", None, "unrecognized"),
        ("2026-07-12Trash", None, "unrecognized"),
        ("2026-02-30T10:00:00Z", None, "unrecognized"),
    ],
)
def test_theirstack_uses_literal_publication_day_without_timezone_conversion(
    value: object, expected_date: date | None, expected_source: str
) -> None:
    result = theirstack.parse_exact_publication_date(value)

    assert result == {
        "published_date": expected_date,
        "publication_date_source": expected_source,
    }
