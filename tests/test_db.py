import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from job_collector import db


def cursor(*, one: object = None, many: list[object] | None = None, rowcount: int = 0) -> Mock:
    item = Mock(rowcount=rowcount)
    item.fetchone.return_value = one
    item.fetchall.return_value = many or []
    return item


def test_create_run_and_raw_response_commit_sanitized_rows() -> None:
    connection = Mock()
    connection.execute.side_effect = [cursor(one=("run-1",)), cursor(one=("response-1",))]

    run_id = db.create_run(connection, "theirstack", {"limit": 2}, 2)
    response_id = db.save_response(
        connection,
        run_id,
        "theirstack",
        1,
        200,
        {"offset": 0},
        {
            "data": [{"id": 1}],
            "api_key": "secret",
            "echo": "neutral-secret",
            "unknown": {"kept": True},
        },
        known_secrets=("neutral-secret",),
    )

    assert (run_id, response_id) == ("run-1", "response-1")
    assert connection.commit.call_count == 2
    payload = connection.execute.call_args_list[1].args[1][-1].obj
    assert payload == {
        "data": [{"id": 1}],
        "api_key": "[REDACTED]",
        "echo": "[REDACTED]",
        "unknown": {"kept": True},
    }


def test_save_page_rolls_back_jobs_and_progress_together() -> None:
    connection = Mock()
    connection.execute.side_effect = [cursor(rowcount=1), cursor(one=None)]
    job = {
        "external_id": "job-1",
        "title": "Software Engineer",
        "raw_payload": {"id": "job-1", "echo": "provider-secret"},
    }

    with pytest.raises(RuntimeError, match="vagas e progresso"):
        db.save_page(
            connection,
            "run-1",
            "response-1",
            "theirstack",
            [job],
            page=1,
            returned=1,
            http_status=200,
            known_secrets=("provider-secret",),
        )

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    assert connection.execute.call_args_list[0].args[1][-1].obj["echo"] == "[REDACTED]"


def test_export_uses_latest_success_utf8_fixed_name_and_preserves_missing_file(
    tmp_path: Path,
) -> None:
    run = {"id": "run-ts", "source": "theirstack", "status": "success"}
    response = {
        "id": "response-1",
        "collection_run_id": "run-ts",
        "response_payload": {"description": "há 18 dias", "api_key": "field-secret"},
    }
    job = {
        "id": "job-1",
        "collection_run_id": "run-ts",
        "raw_api_response_id": "response-1",
        "title": "Engenheira de Software",
    }
    sql_seen: list[str] = []
    connection = Mock()

    def execute(sql: str, params: tuple[object, ...]) -> Mock:
        sql_seen.append(sql)
        if "FROM collection_runs" in sql:
            return cursor(one=run if params == ("theirstack",) else None)
        if "FROM raw_api_responses" in sql:
            return cursor(many=[response])
        return cursor(many=[job])

    connection.execute.side_effect = execute
    previous = tmp_path / "serpapi.json"
    previous.write_text('{"previous": true}\n', encoding="utf-8")

    summaries, missing = db.export_results(connection, tmp_path, ("field-secret",))

    text = (tmp_path / "theirstack.json").read_text(encoding="utf-8")
    exported = json.loads(text)
    assert summaries[0]["run_id"] == "run-ts"
    assert missing == ["serpapi"]
    assert exported["responses"][0]["response_payload"]["api_key"] == "[REDACTED]"
    assert "há 18 dias" in text and "\\u00e1" not in text
    assert previous.read_text(encoding="utf-8") == '{"previous": true}\n'
    assert "status='success'" in sql_seen[0]
    assert "ORDER BY COALESCE(finished_at, started_at) DESC" in sql_seen[0]
