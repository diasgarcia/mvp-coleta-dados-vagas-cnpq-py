from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from job_collector import db, theirstack
from job_collector.collector import (
    CollectionError,
    collect_serpapi,
    collect_theirstack,
    request_with_retry,
)
from job_collector.config import Config, load_config

FIXTURES = Path(__file__).parent / "fixtures"


class FakeDatabase:
    def __init__(self, *, fail_on_jobs: bool = False) -> None:
        self.events: list[str] = []
        self.raw_responses: list[dict[str, Any]] = []
        self.job_batches: list[list[Mapping[str, Any]]] = []
        self.failure: dict[str, Any] | None = None
        self.fail_on_jobs = fail_on_jobs

    def create_collection_run(
        self,
        database_url: str,
        source: str,
        query_params: Mapping[str, Any],
        requested_limit: int | None,
    ) -> str:
        del database_url, source, query_params, requested_limit
        self.events.append("create_run")
        return "00000000-0000-0000-0000-000000000001"

    def save_raw_response(
        self,
        database_url: str,
        run_id: str,
        source: str,
        page_number: int,
        http_status: int | None,
        pagination_token: str | None,
        pagination_offset: int | None,
        request_params: Mapping[str, Any],
        response_payload: Any,
    ) -> str:
        del database_url, run_id
        self.events.append("save_raw")
        self.raw_responses.append(
            {
                "source": source,
                "page_number": page_number,
                "http_status": http_status,
                "pagination_token": pagination_token,
                "pagination_offset": pagination_offset,
                "request_params": dict(request_params),
                "response_payload": response_payload,
            }
        )
        return f"00000000-0000-0000-0000-{len(self.raw_responses):012d}"

    def save_jobs_and_progress(
        self,
        database_url: str,
        run_id: str,
        raw_response_id: str,
        source: str,
        jobs: Sequence[Mapping[str, Any]],
        returned_count: int,
        page_number: int,
        http_status: int | None,
        next_cursor: str | None,
        next_offset: int | None,
    ) -> int:
        del (
            database_url,
            run_id,
            raw_response_id,
            source,
            returned_count,
            page_number,
            http_status,
            next_cursor,
            next_offset,
        )
        self.events.append("save_jobs")
        if self.fail_on_jobs:
            raise db.DatabaseError("Falha simulada ao salvar vagas.")
        self.job_batches.append(list(jobs))
        return len(jobs)

    def finish_collection_run(
        self, database_url: str, run_id: str, http_status: int | None
    ) -> None:
        del database_url, run_id, http_status
        self.events.append("finish")

    def fail_collection_run(
        self,
        database_url: str,
        run_id: str,
        status: str,
        http_status: int | None,
        message: str,
    ) -> None:
        del database_url, run_id
        self.events.append("fail")
        self.failure = {"status": status, "http_status": http_status, "message": message}


def make_config() -> Config:
    return Config(
        database_url="postgresql://postgres:postgres@localhost:5433/job_market-py",
        theirstack_api_key="fixture-theirstack-key",
        serpapi_api_key="fixture-serpapi-key",
        theirstack_location_id=3_448_433,
        theirstack_limit=5,
        theirstack_max_pages=1,
        serpapi_query="software engineer",
        serpapi_location="Sao Paulo,State of Sao Paulo,Brazil",
        serpapi_max_pages=1,
        http_timeout_seconds=30,
        http_max_retries=0,
    )


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_config_rejects_the_historical_typescript_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/job_market")

    with pytest.raises(ValueError, match="job_market-py.*5433"):
        load_config()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5433/job_market-py"
        "?host=localhost&port=5432&dbname=job_market",
    )
    with pytest.raises(ValueError, match="job_market-py.*5433"):
        load_config()

    config_repr = repr(make_config())
    assert "fixture-theirstack-key" not in config_repr
    assert "fixture-serpapi-key" not in config_repr
    assert "postgresql://" not in config_repr


def test_raw_response_is_saved_before_theirstack_mapper_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=fixture("theirstack_response.json"))
        )
    )

    def fail_mapper(raw: object) -> dict[str, Any]:
        del raw
        raise ValueError("Falha simulada no mapper.")

    monkeypatch.setattr(theirstack, "map_job", fail_mapper)

    with pytest.raises(CollectionError, match="Falha simulada no mapper"):
        collect_theirstack(
            make_config(),
            client=client,
            database=database,
            max_retries=0,
        )

    assert database.events == ["create_run", "save_raw", "fail"]
    assert len(database.raw_responses[0]["response_payload"]["data"]) == 2
    assert (
        database.raw_responses[0]["response_payload"]["data"][0]["unknown_provider_field"][
            "api_key"
        ]
        == "[REDACTED]"
    )
    assert database.failure is not None
    assert database.failure["status"] == "failed"


def test_retry_response_and_empty_serpapi_result_are_both_persisted() -> None:
    database = FakeDatabase()
    responses = iter(
        [
            httpx.Response(503, json={"error": "temporarily unavailable"}),
            httpx.Response(200, json=fixture("serpapi_empty_response.json")),
        ]
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))

    result = collect_serpapi(
        make_config(),
        client=client,
        database=database,
        max_retries=1,
        sleep=lambda delay: None,
    )

    assert result.status == "success"
    assert result.returned_count == 0
    assert result.persisted_count == 0
    assert [item["http_status"] for item in database.raw_responses] == [503, 200]
    assert database.events == ["create_run", "save_raw", "save_raw", "save_jobs", "finish"]
    assert "api_key" not in database.raw_responses[1]["request_params"]


def test_nonempty_theirstack_collection_reports_positive_counters() -> None:
    database = FakeDatabase()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=fixture("theirstack_response.json"))
        )
    )

    result = collect_theirstack(make_config(), client=client, database=database, max_retries=0)

    assert result.status == "success"
    assert result.pages_processed == 1
    assert result.returned_count == 2
    assert result.persisted_count == 2
    assert len(database.job_batches[0]) == 2


def test_retry_policy_allowlists_statuses_and_stops_long_retry_after() -> None:
    for status in (400, 401, 402, 403, 429, 501):
        calls = 0

        def terminal_send(status_code: int = status) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(status_code, json={"error": "fixture"})

        result = request_with_retry(terminal_send, max_retries=3, sleep=lambda delay: None)
        assert result.status_code == status
        assert calls == 1

    for status in (500, 502, 503, 504):
        calls = 0
        preserved: list[int] = []

        def transient_send(status_code: int = status) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                status_code if calls == 1 else 200,
                json={"attempt": calls},
            )

        result = request_with_retry(
            transient_send,
            max_retries=1,
            on_retry_response=lambda item: preserved.append(item.status_code),
            sleep=lambda delay: None,
        )
        assert result.status_code == 200
        assert calls == 2
        assert preserved == [status]

    calls = 0

    def long_wait() -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, headers={"Retry-After": "61"}, json={"error": "wait"})

    result = request_with_retry(long_wait, max_retries=3, sleep=lambda delay: None)
    assert result.status_code == 503
    assert calls == 1

    attempts = 0
    delays: list[float] = []
    preserved: list[int] = []

    def send() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout(
                "fixture timeout", request=httpx.Request("GET", "https://example.test")
            )
        if attempts == 2:
            return httpx.Response(503, headers={"Retry-After": "2"}, json={"retry": True})
        return httpx.Response(200, json={"ok": True})

    result = request_with_retry(
        send,
        max_retries=2,
        on_retry_response=lambda item: preserved.append(item.status_code),
        sleep=delays.append,
    )

    assert result.payload == {"ok": True}
    assert attempts == 3
    assert delays == [0.15, 2.0]
    assert preserved == [503]

    text_result = request_with_retry(
        lambda: httpx.Response(200, text="plain fixture body"), max_retries=0
    )
    assert text_result.payload == {"raw_text": "plain fixture body"}


def test_jobs_transaction_failure_keeps_committed_raw_response() -> None:
    database = FakeDatabase(fail_on_jobs=True)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=fixture("serpapi_response.json"))
        )
    )

    with pytest.raises(CollectionError, match="Falha simulada"):
        collect_serpapi(make_config(), client=client, database=database, max_retries=0)

    assert len(database.raw_responses) == 1
    assert database.events == ["create_run", "save_raw", "save_jobs", "fail"]
    assert database.failure is not None
    assert database.failure["status"] == "failed"


def test_repeated_serpapi_token_stops_before_a_third_request_after_two_saved_pages() -> None:
    database = FakeDatabase()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "search_metadata": {"status": "Success"},
                "jobs_results": [{"job_id": f"fixture-{calls}", "title": "Software Engineer"}],
                "serpapi_pagination": {"next_page_token": "same-opaque-token"},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(CollectionError, match="repetiu um token"):
        collect_serpapi(
            make_config(),
            client=client,
            database=database,
            max_pages=2,
            max_retries=0,
        )

    assert calls == 2
    assert len(database.raw_responses) == 2
    assert len(database.job_batches) == 2
    assert database.failure is not None
    assert database.failure["status"] == "partial"
