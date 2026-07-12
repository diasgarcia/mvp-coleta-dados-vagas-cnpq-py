"""Small argparse entry point for migrations, collections and exports."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

import httpx
import psycopg
from psycopg.rows import dict_row

from job_collector import db
from job_collector.collector import HttpError, collect_serpapi, collect_theirstack
from job_collector.config import Config, load_config
from job_collector.sanitize import sanitize_text

TERMINAL_STATUSES = {401, 402, 403, 429}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m job_collector.main",
        description="Coleta raw-first de vagas via TheirStack e SerpApi.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Aplica a migration no PostgreSQL Python.")
    commands.add_parser("export-results", help="Exporta resultados existentes sem chamar APIs.")

    theirstack = commands.add_parser("theirstack", help="Coleta a TheirStack.")
    theirstack.add_argument("--limit", type=int)
    theirstack.add_argument("--max-pages", type=int)
    theirstack.add_argument("--max-retries", type=int)

    serpapi = commands.add_parser("serpapi", help="Coleta a SerpApi Google Jobs.")
    serpapi.add_argument("--max-pages", type=int)
    serpapi.add_argument("--max-retries", type=int)

    all_sources = commands.add_parser("all", help="Executa uma coleta por fonte.")
    all_sources.add_argument("--max-retries", type=int)
    return parser


def _run_collections(command: str, config: Config, args: argparse.Namespace) -> int:
    results: list[dict[str, object]] = []
    failed = False
    with (
        psycopg.connect(config.database_url) as connection,
        httpx.Client(
            timeout=config.http_timeout_seconds,
            headers={"Accept": "application/json"},
        ) as client,
    ):
        if command == "theirstack":
            results.append(
                collect_theirstack(
                    config,
                    connection,
                    client,
                    limit=args.limit,
                    max_pages=args.max_pages,
                    max_retries=args.max_retries,
                )
            )
        elif command == "serpapi":
            results.append(
                collect_serpapi(
                    config,
                    connection,
                    client,
                    max_pages=args.max_pages,
                    max_retries=args.max_retries,
                )
            )
        else:
            for source, collect in (
                ("theirstack", collect_theirstack),
                ("serpapi", collect_serpapi),
            ):
                try:
                    results.append(
                        collect(
                            config,
                            connection,
                            client,
                            max_retries=args.max_retries,
                        )
                    )
                except (RuntimeError, ValueError) as error:
                    failed = True
                    _print_error(error, source)
                    if isinstance(error, HttpError) and error.status_code in TERMINAL_STATUSES:
                        break
    if results:
        print(json.dumps(results[0] if len(results) == 1 else results, ensure_ascii=False))
    return 1 if failed else 0


def _run_export(config: Config) -> int:
    with psycopg.connect(config.database_url, row_factory=dict_row) as connection:
        summaries, missing = db.export_results(
            connection,
            known_secrets=(
                config.theirstack_api_key,
                config.serpapi_api_key,
                config.database_url,
            ),
        )
    if summaries:
        print("Resultados exportados:")
        labels = {"theirstack": "TheirStack", "serpapi": "SerpApi"}
        for item in summaries:
            source = str(item["source"])
            print(f"- {labels[source]}: {item['run_id']} — {item['job_count']} vagas")
        print("- Pasta: results")
    for source in missing:
        print(f"Nenhuma execução bem-sucedida encontrada para {source}.", file=sys.stderr)
    return 1 if missing else 0


def _print_error(error: Exception, source: str | None = None) -> None:
    prefix = f"{source}: " if source else ""
    message = sanitize_text(str(error)) or "Falha ao executar o comando."
    print(f"Erro: {prefix}{message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            db.run_migrations(load_config().database_url)
            print("Migration aplicada no banco Python.")
            return 0
        if args.command == "export-results":
            return _run_export(load_config())
        required = ("theirstack", "serpapi") if args.command == "all" else (args.command,)
        return _run_collections(args.command, load_config(required), args)
    except psycopg.Error:
        _print_error(RuntimeError("Falha ao conectar ao PostgreSQL Python."))
    except (OSError, RuntimeError, ValueError) as error:
        _print_error(error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
