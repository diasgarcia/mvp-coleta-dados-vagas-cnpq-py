from types import SimpleNamespace

import pytest

from job_collector import main


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
