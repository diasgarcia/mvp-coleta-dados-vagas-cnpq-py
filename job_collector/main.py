"""Command-line entry point for migrations and controlled collections."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from job_collector import db
from job_collector.collector import (
    CollectionError,
    CollectionResult,
    collect_serpapi,
    collect_theirstack,
)
from job_collector.config import Config, load_config
from job_collector.export_results import export_results
from job_collector.sanitize import sanitize_text

TERMINAL_HTTP_STATUSES = frozenset({401, 402, 403, 429})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m job_collector.main",
        description="Coleta raw-first de vagas via TheirStack e SerpApi.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Aplica a migration no PostgreSQL Python.")
    commands.add_parser(
        "export-results",
        help="Exporta os resultados existentes no PostgreSQL sem chamar as APIs.",
    )

    theirstack_parser = commands.add_parser("theirstack", help="Coleta a TheirStack.")
    theirstack_parser.add_argument("--query")
    theirstack_parser.add_argument("--location-id", type=_location_ids)
    theirstack_parser.add_argument("--limit", type=int)
    theirstack_parser.add_argument(
        "--posted-at-max-age-days", "--max-age-days", dest="max_age_days", type=int, default=30
    )
    theirstack_parser.add_argument("--max-pages", type=int)
    theirstack_parser.add_argument("--max-retries", type=int)
    preview = theirstack_parser.add_mutually_exclusive_group()
    preview.add_argument("--preview", action="store_true", dest="preview")
    preview.add_argument("--no-preview", action="store_false", dest="preview")
    totals = theirstack_parser.add_mutually_exclusive_group()
    totals.add_argument(
        "--include-total-results", action="store_true", dest="include_total_results"
    )
    totals.add_argument(
        "--no-include-total-results", action="store_false", dest="include_total_results"
    )
    remote = theirstack_parser.add_mutually_exclusive_group()
    remote.add_argument("--remote", action="store_true", dest="remote")
    remote.add_argument("--non-remote", action="store_false", dest="remote")
    theirstack_parser.set_defaults(preview=False, include_total_results=True, remote=None)

    serpapi_parser = commands.add_parser("serpapi", help="Coleta a SerpApi Google Jobs.")
    serpapi_parser.add_argument("--query")
    serpapi_parser.add_argument("--location")
    serpapi_parser.add_argument("--max-pages", type=int)
    serpapi_parser.add_argument("--max-retries", type=int)

    all_parser = commands.add_parser("all", help="Executa uma coleta por fonte.")
    all_parser.add_argument("--max-retries", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            config = load_config()
            db.run_migrations(config.database_url)
            print("Migration aplicada no banco Python.")
            return 0

        if args.command == "export-results":
            config = load_config()
            summaries, missing_sources = export_results(
                config.database_url,
                known_secrets=(
                    config.theirstack_api_key,
                    config.serpapi_api_key,
                    config.database_url,
                ),
            )
            _print_export_summary(summaries, missing_sources)
            return 1 if missing_sources else 0

        if args.command == "theirstack":
            config = load_config(("theirstack",))
            result = _run_theirstack(config, args)
            _print_results([result])
            return 0

        if args.command == "serpapi":
            config = load_config(("serpapi",))
            result = collect_serpapi(
                config,
                query=args.query,
                location=args.location,
                max_pages=args.max_pages,
                max_retries=args.max_retries,
            )
            _print_results([result])
            return 0

        config = load_config(("theirstack", "serpapi"))
        return _run_all(config, args.max_retries)
    except (CollectionError, db.DatabaseError, OSError, ValueError) as error:
        _print_error(error)
        return 1


def _run_theirstack(config: Config, args: argparse.Namespace) -> CollectionResult:
    return collect_theirstack(
        config,
        query=args.query,
        location_ids=args.location_id,
        limit=args.limit,
        max_age_days=args.max_age_days,
        max_pages=args.max_pages,
        max_retries=args.max_retries,
        preview=args.preview,
        include_total_results=args.include_total_results,
        remote=args.remote,
    )


def _run_all(config: Config, max_retries: int | None) -> int:
    results: list[CollectionResult] = []
    failures: list[CollectionError] = []
    collectors = (
        lambda: collect_theirstack(config, max_retries=max_retries),
        lambda: collect_serpapi(config, max_retries=max_retries),
    )
    for collect in collectors:
        try:
            results.append(collect())
        except CollectionError as error:
            failures.append(error)
            if error.http_status in TERMINAL_HTTP_STATUSES:
                break

    if results:
        _print_results(results)
    for error in failures:
        _print_error(error)
    return 1 if failures else 0


def _location_ids(value: str) -> list[int]:
    try:
        ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError("location-id deve conter IDs inteiros.") from error
    if not ids or any(item <= 0 for item in ids):
        raise argparse.ArgumentTypeError("location-id deve conter IDs inteiros positivos.")
    return ids


def _print_results(results: Sequence[CollectionResult]) -> None:
    payload: object = (
        results[0].as_dict() if len(results) == 1 else [item.as_dict() for item in results]
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _print_export_summary(
    summaries: Sequence[dict[str, object]], missing_sources: Sequence[str]
) -> None:
    if summaries:
        print("Resultados exportados:")
        labels = {"theirstack": "TheirStack", "serpapi": "SerpApi"}
        for summary in summaries:
            source = str(summary["source"])
            print(
                f"- {labels.get(source, source)}: {summary['run_id']} — "
                f"{summary['job_count']} vagas"
            )
        print("- Pasta: results")

    for source in missing_sources:
        print(
            f"Nenhuma execução bem-sucedida encontrada para {source}.",
            file=sys.stderr,
        )


def _print_error(error: Exception) -> None:
    if isinstance(error, CollectionError) and error.run_id and error.source:
        summary = CollectionResult(
            collection_run_id=error.run_id,
            source=error.source,
            status=error.status or "failed",
            http_status=error.http_status,
            pages_processed=error.pages_processed,
            returned_count=error.returned_count,
            persisted_count=error.persisted_count,
        )
        print(json.dumps(summary.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
    safe_message = sanitize_text(str(error)) or "Falha ao executar o comando."
    print(f"Erro: {safe_message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
