import json
from datetime import UTC, date, datetime
from pathlib import Path

from job_collector.monthly import (
    RESULT_FILENAMES,
    build_monthly_payloads,
    canonicalize_public_url,
    deduplicate_within_source,
    find_potential_cross_source_duplicates,
    write_monthly_results,
)


def _run(run_id: str, source: str, region: str, query: str) -> dict[str, object]:
    return {
        "id": run_id,
        "source": source,
        "query_params": {
            "round_id": "2026-07",
            "sample_region": region,
            "query": query,
        },
        "status": "success",
        "returned_count": 1,
        "persisted_count": 1,
        "http_status": 200,
        "started_at": datetime(2026, 7, 12, 18, 0, tzinfo=UTC),
    }


def _job(
    job_id: str,
    run_id: str,
    source: str,
    external_id: str | None,
    *,
    title: str = "Engenheira de Software",
    company: str = "Empresa Exemplo",
    location: str = "Campinas, SP",
    source_url: str | None = None,
    publication_date_source: str = "missing",
) -> dict[str, object]:
    return {
        "id": job_id,
        "collection_run_id": run_id,
        "raw_api_response_id": f"response-{run_id}",
        "source": source,
        "external_id": external_id,
        "title": title,
        "company": company,
        "location": location,
        "published_date": date(2026, 7, 10),
        "publication_date_source": publication_date_source,
        "published_at_text": "há 2 dias" if source == "serpapi" else None,
        "source_url": source_url,
        "collected_at": datetime(2026, 7, 12, 18, 2, tzinfo=UTC),
    }


def test_deduplicates_external_ids_but_keeps_jobs_without_external_id() -> None:
    runs = [_run("run-1", "theirstack", "campinas", "software engineer")]
    jobs = [
        _job("job-1", "run-1", "theirstack", "same-id"),
        _job("job-2", "run-1", "theirstack", "same-id"),
        _job("job-3", "run-1", "theirstack", None),
        _job("job-4", "run-1", "theirstack", None),
    ]

    unique = deduplicate_within_source(jobs, runs)

    assert len(unique) == 3
    repeated = next(item for item in unique if item["external_id"] == "same-id")
    assert repeated["occurrence_count"] == 2
    assert repeated["run_ids"] == ["run-1"]
    assert repeated["sample_regions"] == ["campinas"]
    assert sum(item["external_id"] is None for item in unique) == 2


def test_lists_cross_source_matches_by_url_then_normalized_fields() -> None:
    unique = [
        _job(
            "their-url",
            "run-t",
            "theirstack",
            "their-1",
            source_url="https://www.example.com/jobs/123/?utm_source=provider",
        ),
        _job(
            "serp-url",
            "run-s",
            "serpapi",
            "serp-1",
            title="Outro cargo",
            source_url="http://example.com/jobs/123",
        ),
        _job(
            "their-fields",
            "run-t",
            "theirstack",
            "their-2",
            title="Desenvolvedor Sênior",
            company="Organização Ágil",
            location="São José dos Campos - SP",
        ),
        _job(
            "serp-fields",
            "run-s",
            "serpapi",
            "serp-2",
            title="desenvolvedor senior",
            company="Organizacao Agil",
            location="sao jose dos campos sp",
        ),
    ]

    duplicates = find_potential_cross_source_duplicates(unique)

    assert [item["reason"] for item in duplicates] == [
        "canonical_url",
        "normalized_title_company_location",
    ]
    assert canonicalize_public_url("https://www.Example.com/jobs/123/?gclid=x") == (
        "example.com/jobs/123"
    )


def test_builds_grouped_source_documents_and_complete_summary() -> None:
    runs = [
        _run("run-t", "theirstack", "campinas", "software engineer"),
        _run("run-s", "serpapi", "campinas", "desenvolvedor de software"),
    ]
    responses = [
        {
            "id": "response-s",
            "collection_run_id": "run-s",
            "page_number": 1,
            "response_payload": {"jobs_results": []},
        },
        {
            "id": "response-t",
            "collection_run_id": "run-t",
            "page_number": 1,
            "response_payload": {"data": []},
        },
    ]
    jobs = [
        _job(
            "job-t",
            "run-t",
            "theirstack",
            "their-1",
            publication_date_source="theirstack_exact",
        ),
        _job(
            "job-s",
            "run-s",
            "serpapi",
            "serp-1",
            publication_date_source="serpapi_estimated",
        ),
    ]

    payloads = build_monthly_payloads(
        "2026-07",
        runs,
        responses,
        jobs,
        generated_at="2026-07-12T21:00:00Z",
        planned_regions=("sao-paulo", "campinas"),
        planned_queries={"theirstack": 8, "serpapi": 16},
        limits_used={"theirstack_requested_items": 80, "serpapi_requests": 16},
        skipped_queries=({"source": "serpapi", "sample_region": "santos"},),
    )

    summary = payloads["summary"]
    assert summary["raw_occurrences"] == 2
    assert summary["unique_within_source"] == {"serpapi": 1, "theirstack": 1}
    assert summary["publication_dates"] == {
        "exact": 1,
        "estimated": 1,
        "missing": 0,
        "unrecognized": 0,
    }
    assert summary["queries_planned"] == {"theirstack": 8, "serpapi": 16}
    assert summary["queries_skipped"] == 1
    assert payloads["theirstack"]["runs"][0]["responses"][0]["id"] == "response-t"
    assert payloads["serpapi"]["runs"][0]["jobs"][0]["id"] == "job-s"


def test_build_does_not_remove_cross_source_occurrences() -> None:
    runs = [
        _run("run-t", "theirstack", "santos", "software engineer"),
        _run("run-s", "serpapi", "santos", "software engineer"),
    ]
    jobs = [
        _job("job-t", "run-t", "theirstack", "their-1", source_url="https://jobs.test/1"),
        _job("job-s", "run-s", "serpapi", "serp-1", source_url="https://jobs.test/1"),
    ]

    payloads = build_monthly_payloads("2026-07", runs, [], jobs)

    assert payloads["summary"]["raw_occurrences"] == 2
    assert len(payloads["unique_jobs"]["jobs"]) == 2
    assert payloads["summary"]["potential_cross_source_duplicates"] == 1


def test_writes_fixed_utf8_sanitized_files_with_final_newline(tmp_path: Path) -> None:
    payloads = build_monthly_payloads(
        "2026-07",
        [],
        [],
        [],
        generated_at="2026-07-12T21:00:00Z",
    )
    payloads["summary"]["note"] = "há 18 dias"
    payloads["summary"]["api_key"] = "provider-secret"
    payloads["theirstack"]["echo"] = "provider-secret"

    paths = write_monthly_results(
        "2026-07",
        payloads,
        output_root=tmp_path,
        known_secrets=("provider-secret",),
    )

    assert {path.name for path in paths.values()} == set(RESULT_FILENAMES.values())
    for path in paths.values():
        assert path.parent == tmp_path / "2026-07"
        assert path.read_bytes().endswith(b"\n")
        json.loads(path.read_text(encoding="utf-8"))
    summary_text = paths["summary"].read_text(encoding="utf-8")
    theirstack_text = paths["theirstack"].read_text(encoding="utf-8")
    assert "há 18 dias" in summary_text and "\\u00e1" not in summary_text
    assert "provider-secret" not in summary_text + theirstack_text
    assert json.loads(summary_text)["api_key"] == "[REDACTED]"
