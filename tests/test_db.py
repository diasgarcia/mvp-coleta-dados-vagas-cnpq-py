import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from job_collector import db


def cursor(*, one: object = None, many: list[object] | None = None, rowcount: int = 0) -> Mock:
    item = Mock(rowcount=rowcount)
    item.fetchone.return_value = one
    item.fetchall.return_value = many or []
    return item


def test_run_migrations_applies_all_sql_files_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "002_second.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    connection = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=connection)
    context.__exit__ = Mock(return_value=False)
    connect = Mock(return_value=context)
    monkeypatch.setattr(db, "MIGRATIONS_DIRECTORY", tmp_path)
    monkeypatch.setattr(db.psycopg, "connect", connect)

    db.run_migrations("postgresql://local/test")
    db.run_migrations("postgresql://local/test")

    assert [call.args[0] for call in connection.execute.call_args_list] == [
        "SELECT 1;",
        "SELECT 2;",
        "SELECT 1;",
        "SELECT 2;",
    ]


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
        "published_date": date(2026, 7, 12),
        "publication_date_source": "theirstack_exact",
        "raw_payload": {"id": "job-1", "echo": "provider-secret"},
    }
    collected_at = datetime(2026, 7, 12, 18, 4, tzinfo=UTC)

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
            collected_at=collected_at,
            known_secrets=("provider-secret",),
        )

    connection.rollback.assert_called_once()
    connection.commit.assert_not_called()
    inserted = connection.execute.call_args_list[0].args[1]
    assert inserted[10:12] == (date(2026, 7, 12), "theirstack_exact")
    assert inserted[-2].obj["echo"] == "[REDACTED]"
    assert inserted[-1] is collected_at


def test_backfill_uses_stored_collected_at_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collected_at = datetime(2026, 7, 12, 18, 4, tzinfo=UTC)
    rows = [
        {
            "id": "their-1",
            "source": "theirstack",
            "raw_payload": {"date_posted": "2026-07-10", "unknown": True},
            "published_at_text": None,
            "collected_at": collected_at,
        },
        {
            "id": "serp-1",
            "source": "serpapi",
            "raw_payload": {"job_id": "1"},
            "published_at_text": "há 18 dias",
            "collected_at": collected_at,
        },
        {
            "id": "serp-2",
            "source": "serpapi",
            "raw_payload": {"job_id": "2"},
            "published_at_text": None,
            "collected_at": collected_at,
        },
        {
            "id": "serp-3",
            "source": "serpapi",
            "raw_payload": {"job_id": "3"},
            "published_at_text": "publicado recentemente",
            "collected_at": collected_at,
        },
    ]
    connection = Mock()
    connection.execute.side_effect = [
        cursor(many=rows),
        cursor(rowcount=1),
        cursor(rowcount=1),
        cursor(rowcount=1),
        cursor(rowcount=1),
        cursor(many=[]),
    ]
    exact = Mock(
        return_value={
            "published_date": date(2026, 7, 10),
            "publication_date_source": "theirstack_exact",
        }
    )

    def relative(text: object, reference: object) -> dict[str, object]:
        assert reference is collected_at
        values = {
            "há 18 dias": (date(2026, 6, 24), "serpapi_estimated"),
            None: (None, "missing"),
            "publicado recentemente": (None, "unrecognized"),
        }
        published_date, source = values[text]
        return {"published_date": published_date, "publication_date_source": source}

    monkeypatch.setattr(db.theirstack, "parse_exact_publication_date", exact)
    monkeypatch.setattr(db.serpapi, "parse_relative_publication_date", relative)

    summary = db.backfill_publication_dates(connection)
    repeated = db.backfill_publication_dates(connection)

    assert summary == {
        "theirstack_updated": 1,
        "serpapi_updated": 1,
        "missing": 1,
        "unrecognized": 1,
    }
    assert repeated == {
        "theirstack_updated": 0,
        "serpapi_updated": 0,
        "missing": 0,
        "unrecognized": 0,
    }
    exact.assert_called_once_with("2026-07-10")
    update_params = [call.args[1] for call in connection.execute.call_args_list if call.args[1:]]
    assert update_params[1] == (date(2026, 6, 24), "serpapi_estimated", "serp-1")
    assert connection.commit.call_count == 2
    connection.rollback.assert_not_called()


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
