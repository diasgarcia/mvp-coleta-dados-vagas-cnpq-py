from types import SimpleNamespace
from typing import Any

import pytest

from job_collector import main
from job_collector.collector import HttpError


def test_cli_dispatches_migrate_collectors_all_and_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[str] = []
    config = SimpleNamespace(database_url="postgresql://fixture")
    monkeypatch.setattr(main, "load_config", lambda *_required: config)
    monkeypatch.setattr(main.db, "run_migrations", lambda _url: actions.append("migrate"))
    monkeypatch.setattr(
        main,
        "_run_collections",
        lambda command, _config, _args: actions.append(command) or 0,
    )
    monkeypatch.setattr(
        main,
        "_run_backfill",
        lambda _config: actions.append("backfill-publication-dates") or 0,
    )
    monkeypatch.setattr(main, "_run_export", lambda _config: actions.append("export-results") or 0)

    commands = [
        ["migrate"],
        ["backfill-publication-dates"],
        ["theirstack", "--limit", "2", "--max-pages", "1", "--max-retries", "0"],
        ["serpapi", "--max-pages", "1", "--max-retries", "0"],
        ["all", "--max-retries", "0"],
        ["export-results"],
    ]
    assert [main.main(command) for command in commands] == [0, 0, 0, 0, 0, 0]
    assert actions == [
        "migrate",
        "backfill-publication-dates",
        "theirstack",
        "serpapi",
        "all",
        "export-results",
    ]


def test_monthly_dry_run_does_not_load_config_or_open_external_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "load_config",
        lambda *_args: pytest.fail("dry-run não deve carregar credenciais"),
    )

    assert main.main(["monthly", "--round", "2026-07", "--dry-run"]) == 0


def test_monthly_live_requires_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "load_config",
        lambda *_args: pytest.fail("execução sem confirmação deve parar antes da configuração"),
    )

    assert main.main(["monthly", "--round", "2026-07"]) == 1


def _small_plan() -> dict[str, Any]:
    return {
        "round_id": "2026-07",
        "collection_kind": "monthly",
        "theirstack": [
            {
                "source": "theirstack",
                "sample_region": "campinas",
                "query": "validated-technology-titles",
                "requested_location_name": "Campinas",
                "requested_location_ids": [3_467_865],
                "limit": 10,
                "max_retries": 0,
            }
        ],
        "serpapi": [
            {
                "source": "serpapi",
                "sample_region": "campinas",
                "query": "software engineer",
                "query_origin": "Campinas,State of Sao Paulo,Brazil",
                "canonical_location": "Campinas,State of Sao Paulo,Brazil",
                "max_retries": 0,
            }
        ],
    }


def test_monthly_skips_success_and_retries_a_failed_query(
    monkeypatch: pytest.MonkeyPatch, config: Any
) -> None:
    plan = _small_plan()

    def find(_connection: object, source: str, _signature: object) -> dict[str, str]:
        status = "success" if source == "theirstack" else "failed"
        return {"id": f"previous-{source}", "status": status}

    calls: list[str] = []
    monkeypatch.setattr(main.db, "find_matching_round_run", find)
    monkeypatch.setattr(
        main,
        "collect_theirstack",
        lambda *_args, **_kwargs: calls.append("theirstack") or {},
    )
    monkeypatch.setattr(
        main,
        "collect_serpapi",
        lambda *_args, **_kwargs: (
            calls.append("serpapi")
            or {"collection_run_id": "run-new", "source": "serpapi", "status": "success"}
        ),
    )

    actions, terminal = main._collect_monthly_plan(plan, config, object(), object())

    assert calls == ["serpapi"]
    assert actions[0]["status"] == "skipped_success"
    assert actions[1]["collection_run_id"] == "run-new"
    assert terminal is None


def test_monthly_terminal_http_stops_following_queries(
    monkeypatch: pytest.MonkeyPatch, config: Any
) -> None:
    plan = _small_plan()
    plan["theirstack"] = []
    plan["serpapi"].append({**plan["serpapi"][0], "query": "desenvolvedor de software"})
    calls: list[str] = []
    monkeypatch.setattr(main.db, "find_matching_round_run", lambda *_args: None)

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("serpapi")
        raise HttpError(401, "HTTP 401")

    monkeypatch.setattr(main, "collect_serpapi", fail)

    actions, terminal = main._collect_monthly_plan(plan, config, object(), object())

    assert len(calls) == 1
    assert actions[0]["status"] == "failed"
    assert terminal == 401


def test_monthly_limits_are_derived_from_persisted_runs() -> None:
    plan = {
        "limits": {
            "theirstack_budget": 80,
            "serpapi_budget": 16,
            "max_pages": 1,
            "max_retries": 0,
        }
    }
    runs = [
        {"source": "theirstack", "requested_limit": 10},
        {"source": "theirstack", "requested_limit": 10},
        {"source": "serpapi", "requested_limit": None},
    ]

    limits = main._round_limits(plan, runs)

    assert limits["theirstack_requests_executed"] == 2
    assert limits["theirstack_items_requested"] == 20
    assert limits["serpapi_requests_executed"] == 1
