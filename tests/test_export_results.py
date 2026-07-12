from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from job_collector import main
from job_collector.export_results import (
    RESULT_FILENAMES,
    build_export,
    export_results_from_connection,
    fetch_latest_successful_run,
    write_export_file,
)


def _cursor(*, one: object = None, many: list[object] | None = None) -> Mock:
    cursor = Mock()
    cursor.fetchone.return_value = one
    cursor.fetchall.return_value = many or []
    return cursor


def _run(source: str, run_id: str) -> dict[str, object]:
    return {
        "id": run_id,
        "source": source,
        "query_params": {"query": "engenheira de software"},
        "requested_limit": 5,
        "started_at": "2026-07-12T20:00:00+00:00",
        "finished_at": "2026-07-12T20:01:00+00:00",
        "status": "success",
        "returned_count": 1,
        "persisted_count": 1,
        "pages_processed": 1,
        "http_status": 200,
        "error_message": None,
        "last_page": 1,
        "last_cursor": None,
        "last_offset": 1,
    }


def test_fetch_latest_successful_run_uses_success_and_audit_timestamp_order() -> None:
    connection = Mock()
    connection.execute.return_value = _cursor(one=_run("theirstack", "run-new"))

    selected = fetch_latest_successful_run(connection, "theirstack")

    assert selected is not None
    assert selected["id"] == "run-new"
    sql, parameters = connection.execute.call_args.args
    assert "status = 'success'" in sql
    assert "ORDER BY COALESCE(finished_at, started_at) DESC" in sql
    assert parameters == ("theirstack",)


def test_build_export_keeps_full_response_and_normalized_job_relations() -> None:
    run = _run("serpapi", "run-serp")
    response = {
        "id": "response-1",
        "collection_run_id": "run-serp",
        "source": "serpapi",
        "response_payload": {
            "jobs_results": [{"job_id": "job-1", "unknown_field": {"kept": True}}]
        },
    }
    job = {
        "id": "row-1",
        "collection_run_id": "run-serp",
        "raw_api_response_id": "response-1",
        "external_id": "job-1",
        "source": "serpapi",
        "title": "Engenheira de Software",
        "published_at_text": "há 18 dias",
    }

    payload = build_export(
        run,
        [response],
        [job],
        exported_at="2026-07-12T21:00:00Z",
    )

    assert payload["repository"] == "python"
    assert payload["source"] == "serpapi"
    assert payload["responses"][0]["response_payload"] == response["response_payload"]
    assert payload["jobs"][0]["collection_run_id"] == "run-serp"
    assert payload["jobs"][0]["raw_api_response_id"] == "response-1"
    assert "raw_payload" not in payload["jobs"][0]


def test_write_export_is_utf8_sanitized_and_uses_fixed_provider_names(tmp_path: Path) -> None:
    secret = "fixture-real-provider-key"
    payload = build_export(
        _run("serpapi", "run-serp"),
        [
            {
                "id": "response-1",
                "response_payload": {
                    "api_key": "field-secret",
                    "innocent_field": f"prefix-{secret}-suffix",
                    "Authorization": "Bearer bearer-secret",
                    "next_page_token": "opaque-token",
                },
            }
        ],
        [{"published_at_text": "há 18 dias"}],
        exported_at="2026-07-12T21:00:00Z",
    )
    path = tmp_path / RESULT_FILENAMES["serpapi"]

    write_export_file(path, payload, (secret,))

    raw_text = path.read_text(encoding="utf-8")
    decoded = json.loads(raw_text)
    assert path.name == "serpapi.json"
    assert RESULT_FILENAMES["theirstack"] == "theirstack.json"
    assert "há 18 dias" in raw_text
    assert "h\\u00e1 18 dias" not in raw_text
    assert raw_text.endswith("\n")
    assert secret not in raw_text
    assert "bearer-secret" not in raw_text
    assert decoded["responses"][0]["response_payload"]["api_key"] == "[REDACTED]"
    assert decoded["responses"][0]["response_payload"]["next_page_token"] == "opaque-token"


def test_missing_source_keeps_previous_file_and_continues_other_export(tmp_path: Path) -> None:
    previous = tmp_path / "theirstack.json"
    previous.write_text('{"previous": true}\n', encoding="utf-8")
    connection = Mock()
    connection.execute.side_effect = [
        _cursor(one=None),
        _cursor(one=_run("serpapi", "run-serp")),
        _cursor(
            many=[
                {
                    "id": "response-1",
                    "collection_run_id": "run-serp",
                    "source": "serpapi",
                    "response_payload": {"jobs_results": []},
                }
            ]
        ),
        _cursor(
            many=[
                {
                    "id": "job-row-1",
                    "collection_run_id": "run-serp",
                    "raw_api_response_id": "response-1",
                    "source": "serpapi",
                    "title": "Software Engineer",
                }
            ]
        ),
    ]

    summaries, missing = export_results_from_connection(
        connection,
        output_dir=tmp_path,
        exported_at="2026-07-12T21:00:00Z",
    )

    assert missing == ["theirstack"]
    assert summaries[0]["source"] == "serpapi"
    assert summaries[0]["job_count"] == 1
    assert previous.read_text(encoding="utf-8") == '{"previous": true}\n'
    assert json.loads((tmp_path / "serpapi.json").read_text(encoding="utf-8"))["source"] == (
        "serpapi"
    )


def test_cli_returns_nonzero_when_a_source_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = Mock(
        database_url="postgresql://fixture",
        theirstack_api_key="their-secret",
        serpapi_api_key="serp-secret",
    )
    monkeypatch.setattr(main, "load_config", lambda: config)
    monkeypatch.setattr(
        main,
        "export_results",
        lambda database_url, known_secrets: (
            [
                {
                    "source": "theirstack",
                    "run_id": "run-ts",
                    "job_count": 5,
                    "response_count": 1,
                    "path": "results/theirstack.json",
                }
            ],
            ["serpapi"],
        ),
    )

    exit_code = main.main(["export-results"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "TheirStack: run-ts — 5 vagas" in captured.out
    assert "Pasta: results" in captured.out
    assert "Nenhuma execução bem-sucedida encontrada para serpapi." in captured.err
