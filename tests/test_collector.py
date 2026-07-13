from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from job_collector import collector, db, serpapi, theirstack
from job_collector.collector import collect_serpapi, collect_theirstack, request_json
from job_collector.config import Config


def fake_database(monkeypatch: pytest.MonkeyPatch, *, fail_page: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = {
        "events": [],
        "raw": [],
        "jobs": [],
        "runs": [],
        "page_kwargs": [],
    }

    def create(_connection: object, source: str, params: object, limit: int | None) -> str:
        state["events"].append("create")
        state["runs"].append((source, params, limit))
        return "run-1"

    def save_response(*args: object, **kwargs: object) -> str:
        state["events"].append("raw")
        state["raw"].append((args, kwargs))
        return f"response-{len(state['raw'])}"

    def save_page(*args: object, **kwargs: object) -> int:
        state["events"].append("page")
        if fail_page:
            raise RuntimeError("page failure")
        jobs = list(args[4])
        state["jobs"].extend(jobs)
        state["page_kwargs"].append(kwargs)
        return len(jobs)

    monkeypatch.setattr(db, "create_run", create)
    monkeypatch.setattr(db, "save_response", save_response)
    monkeypatch.setattr(db, "save_page", save_page)
    monkeypatch.setattr(db, "finish_run", lambda *args: state["events"].append("finish"))

    def fail_run(*args: object) -> None:
        state["events"].append("fail")
        state["failure"] = args

    monkeypatch.setattr(db, "fail_run", fail_run)
    return state


def test_request_json_retries_503_and_preserves_the_failed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [httpx.Response(503, json={"attempt": 1}), httpx.Response(200, json={"ok": True})]
    )
    preserved: list[tuple[int, object]] = []
    monkeypatch.setattr(collector.time, "sleep", lambda _seconds: None)

    status, payload = request_json(lambda: next(responses), 1, lambda *item: preserved.append(item))

    assert (status, payload) == (200, {"ok": True})
    assert preserved == [(503, {"attempt": 1})]


def test_request_json_does_not_retry_terminal_4xx() -> None:
    for status in (400, 401, 402, 403, 429):
        calls: list[int] = []

        def send(code: int = status) -> httpx.Response:
            calls.append(code)
            return httpx.Response(code, json={"error": "terminal"})

        assert request_json(send, 3, lambda *_: None)[0] == status
        assert calls == [status]


def test_theirstack_collection_is_raw_first_and_uses_safe_audit_params(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    fixture_json: Callable[[str], Any],
) -> None:
    state = fake_database(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer their-secret"
        assert json.loads(request.content)["job_location_or"] == [{"id": 3_448_433}]
        return httpx.Response(200, json=fixture_json("theirstack_response.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = collect_theirstack(config, object(), client, limit=5, max_pages=1, max_retries=0)

    assert state["events"] == ["create", "raw", "page", "finish"]
    assert result["returned_count"] == result["persisted_count"] == 2
    assert "their-secret" not in repr(state["runs"])
    assert "Authorization" not in repr(state["raw"])


def test_monthly_overrides_are_used_and_audited(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    fixture_json: Callable[[str], Any],
) -> None:
    state = fake_database(monkeypatch)

    def theirstack_handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["job_location_or"] == [{"id": 3_467_865}]
        return httpx.Response(200, json=fixture_json("theirstack_response.json"))

    with httpx.Client(transport=httpx.MockTransport(theirstack_handler)) as client:
        collect_theirstack(
            config,
            object(),
            client,
            location_id=3_467_865,
            limit=10,
            max_pages=1,
            max_retries=0,
            audit_context={
                "collection_kind": "monthly",
                "round_id": "2026-07",
                "sample_region": "campinas",
                "requested_location_ids": [999],
                "limit": 1,
                "max_pages": 2,
            },
        )

    audit = state["runs"][0][1]
    assert audit["round_id"] == "2026-07"
    assert audit["sample_region"] == "campinas"
    assert audit["requested_location_ids"] == [3_467_865]
    assert audit["limit"] == 10
    assert audit["max_pages"] == 1

    state = fake_database(monkeypatch)

    def serpapi_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "desenvolvedor de software"
        assert request.url.params["location"] == "Campinas,State of Sao Paulo,Brazil"
        return httpx.Response(200, json=fixture_json("serpapi_empty_response.json"))

    with httpx.Client(transport=httpx.MockTransport(serpapi_handler)) as client:
        collect_serpapi(
            config,
            object(),
            client,
            query="desenvolvedor de software",
            location="Campinas,State of Sao Paulo,Brazil",
            max_pages=1,
            max_retries=0,
            audit_context={
                "collection_kind": "monthly",
                "round_id": "2026-07",
                "sample_region": "campinas",
                "query_origin": "Campinas",
                "query": "consulta falsa",
                "canonical_location": "local falso",
                "max_pages": 2,
            },
        )

    audit = state["runs"][0][1]
    assert audit["round_id"] == "2026-07"
    assert audit["sample_region"] == "campinas"
    assert audit["query"] == "desenvolvedor de software"
    assert audit["canonical_location"] == "Campinas,State of Sao Paulo,Brazil"
    assert audit["max_pages"] == 1


def test_serpapi_empty_response_is_a_success_and_key_is_not_audited(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    fixture_json: Callable[[str], Any],
) -> None:
    state = fake_database(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "serp-secret"
        return httpx.Response(200, json=fixture_json("serpapi_empty_response.json"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = collect_serpapi(config, object(), client, max_pages=1, max_retries=0)

    assert result["status"] == "success"
    assert result["returned_count"] == result["persisted_count"] == 0
    assert state["events"] == ["create", "raw", "page", "finish"]
    assert "api_key" not in state["raw"][0][0][5]


def test_serpapi_collection_reuses_one_collection_time_for_mapping_and_insert(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    fixture_json: Callable[[str], Any],
) -> None:
    state = fake_database(monkeypatch)
    references: list[object] = []
    original_map_job = serpapi.map_job

    def map_job(raw: object, collected_at: object) -> dict[str, Any]:
        references.append(collected_at)
        return original_map_job(raw, collected_at)

    monkeypatch.setattr(serpapi, "map_job", map_job)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=fixture_json("serpapi_response.json"))
    )

    with httpx.Client(transport=transport) as client:
        collect_serpapi(config, object(), client, max_pages=1, max_retries=0)

    persisted_reference = state["page_kwargs"][0]["collected_at"]
    assert references
    assert all(reference is persisted_reference for reference in references)


def test_terminal_http_error_is_saved_before_the_run_fails(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
) -> None:
    state = fake_database(monkeypatch)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(401, json={"error": "unauthorized"})
    )

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(collector.HttpError, match="HTTP 401"),
    ):
        collect_theirstack(config, object(), client, max_retries=3)

    assert state["events"] == ["create", "raw", "fail"]
    assert len(state["raw"]) == 1


def test_mapper_failure_happens_after_raw_commit_and_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    fixture_json: Callable[[str], Any],
) -> None:
    state = fake_database(monkeypatch)

    def fail_mapper(_raw: object) -> dict[str, Any]:
        raise ValueError("map their-secret")

    monkeypatch.setattr(theirstack, "map_job", fail_mapper)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=fixture_json("theirstack_response.json"))
    )

    with httpx.Client(transport=transport) as client, pytest.raises(RuntimeError, match="map"):
        collect_theirstack(config, object(), client, max_retries=0)

    assert state["events"] == ["create", "raw", "fail"]
    assert "their-secret" not in state["failure"][4]
