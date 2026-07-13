"""Small argparse entry point for migrations, collections and exports."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from job_collector import db, monthly
from job_collector.collector import HttpError, collect_serpapi, collect_theirstack
from job_collector.config import Config, load_config
from job_collector.regions import REGIONS, build_monthly_plan
from job_collector.sanitize import sanitize_text

TERMINAL_STATUSES = {401, 402, 403, 429}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m job_collector.main",
        description="Coleta raw-first de vagas via TheirStack e SerpApi.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Aplica a migration no PostgreSQL Python.")
    commands.add_parser(
        "backfill-publication-dates",
        help="Preenche datas de publicação dos registros existentes.",
    )
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

    monthly_parser = commands.add_parser(
        "monthly", help="Planeja ou executa uma rodada mensal regional."
    )
    monthly_parser.add_argument("--round", required=True, dest="round_id")
    mode = monthly_parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm-live", action="store_true")
    monthly_parser.add_argument("--theirstack-budget", type=int, default=80)
    monthly_parser.add_argument("--serpapi-budget", type=int, default=16)
    monthly_parser.add_argument("--max-retries", type=int, default=0)
    monthly_parser.add_argument("--force", action="store_true")
    return parser


def _print_monthly_plan(plan: dict[str, Any], *, dry_run: bool) -> None:
    label = "dry-run; nenhuma API ou banco acessado" if dry_run else "execução real autorizada"
    limits = plan["limits"]
    print(f"Plano mensal {plan['round_id']} ({label}):")
    print(f"- Polos: {len(plan['regions'])}")
    print(
        f"- TheirStack: {len(plan['theirstack'])} consultas, limite 10, "
        f"máximo {limits['theirstack_requested_items']} itens"
    )
    print(
        f"- SerpApi: {len(plan['serpapi'])} consultas "
        f"({len(plan['serpapi']) // len(plan['regions'])} por polo)"
    )
    print(f"- Páginas por consulta: {limits['max_pages']}")
    print(f"- Retries: {limits['max_retries']}")


def _monthly_signature(plan: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    signature = {
        "collection_kind": plan["collection_kind"],
        "round_id": plan["round_id"],
        "sample_region": item["sample_region"],
        "query": item["query"],
    }
    if item["source"] == "theirstack":
        signature["requested_location_ids"] = item["requested_location_ids"]
    return signature


def _collect_monthly_plan(
    plan: dict[str, Any],
    config: Config,
    connection: Any,
    client: httpx.Client,
    *,
    force: bool = False,
) -> tuple[list[dict[str, Any]], int | None]:
    """Execute planned queries once, stopping the whole round on terminal HTTP."""
    actions: list[dict[str, Any]] = []
    terminal_status: int | None = None
    for item in [*plan["theirstack"], *plan["serpapi"]]:
        signature = _monthly_signature(plan, item)
        previous = db.find_matching_round_run(connection, item["source"], signature)
        previous_status = previous.get("status") if previous else None
        if previous and not force and previous_status in {"success", "partial", "running"}:
            action = {**signature, "status": f"skipped_{previous_status}", "run_id": previous["id"]}
            actions.append(action)
            print(
                f"- Pulada: {item['source']} / {item['sample_region']} / {item['query']} "
                f"({previous_status})."
            )
            continue

        context = dict(signature)
        if item["source"] == "theirstack":
            context.update(
                {
                    "strategy_name": "monthly-regional-city",
                    "requested_location_name": item["requested_location_name"],
                }
            )
        else:
            context.update(
                {
                    "strategy_name": "monthly-regional-origin",
                    "query_origin": item["query_origin"],
                }
            )
        try:
            if item["source"] == "theirstack":
                result = collect_theirstack(
                    config,
                    connection,
                    client,
                    location_id=item["requested_location_ids"][0],
                    limit=item["limit"],
                    max_pages=1,
                    max_retries=item["max_retries"],
                    audit_context=context,
                )
            else:
                result = collect_serpapi(
                    config,
                    connection,
                    client,
                    query=item["query"],
                    location=item["canonical_location"],
                    max_pages=1,
                    max_retries=item["max_retries"],
                    audit_context=context,
                )
            actions.append({**signature, **result})
        except (RuntimeError, ValueError) as error:
            status = error.status_code if isinstance(error, HttpError) else None
            actions.append({**signature, "status": "failed", "http_status": status})
            _print_error(error, item["source"])
            if status in TERMINAL_STATUSES:
                terminal_status = status
                break
    return actions, terminal_status


def _round_limits(plan: dict[str, Any], runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    theirstack_runs = [run for run in runs if run.get("source") == "theirstack"]
    serpapi_runs = [run for run in runs if run.get("source") == "serpapi"]
    return {
        **plan["limits"],
        "theirstack_requests_executed": len(theirstack_runs),
        "theirstack_items_requested": sum(
            int(run.get("requested_limit") or 0) for run in theirstack_runs
        ),
        "serpapi_requests_executed": len(serpapi_runs),
    }


def _run_monthly(config: Config, args: argparse.Namespace, plan: dict[str, Any]) -> int:
    _print_monthly_plan(plan, dry_run=False)
    with (
        psycopg.connect(config.database_url) as connection,
        httpx.Client(
            timeout=config.http_timeout_seconds, headers={"Accept": "application/json"}
        ) as client,
    ):
        actions, terminal_status = _collect_monthly_plan(
            plan, config, connection, client, force=args.force
        )

    with psycopg.connect(config.database_url, row_factory=dict_row) as connection:
        data = db.load_monthly_data(connection, args.round_id)
    skipped = [item for item in actions if str(item["status"]).startswith("skipped_")]
    payloads = monthly.build_monthly_payloads(
        args.round_id,
        data["runs"],
        data["responses"],
        data["jobs"],
        planned_regions=[str(region["key"]) for region in REGIONS],
        planned_queries={"theirstack": 8, "serpapi": 16},
        limits_used=_round_limits(plan, data["runs"]),
        skipped_queries=skipped,
    )
    paths = monthly.write_monthly_results(
        args.round_id,
        payloads,
        output_root=Path("results/monthly"),
        known_secrets=(
            config.theirstack_api_key,
            config.serpapi_api_key,
            config.database_url,
        ),
    )
    summary = payloads["summary"]
    print("Rodada mensal registrada:")
    print(f"- Execuções no banco: {summary['total_collection_runs']}")
    print(f"- Respostas brutas: {summary['total_raw_responses']}")
    print(f"- Ocorrências: {summary['raw_occurrences']}")
    print(f"- Resultados: {paths['summary'].parent}")
    failed = any(item["status"] == "failed" for item in actions)
    return 1 if failed or terminal_status else 0


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


def _run_backfill(config: Config) -> int:
    with psycopg.connect(config.database_url, row_factory=dict_row) as connection:
        summary = db.backfill_publication_dates(connection)
    print("Backfill de datas de publicação:")
    print(f"- TheirStack atualizados: {summary['theirstack_updated']}")
    print(f"- SerpApi atualizados: {summary['serpapi_updated']}")
    print(f"- Sem informação: {summary['missing']}")
    print(f"- Textos não reconhecidos: {summary['unrecognized']}")
    return 0


def _print_error(error: Exception, source: str | None = None) -> None:
    prefix = f"{source}: " if source else ""
    message = sanitize_text(str(error)) or "Falha ao executar o comando."
    print(f"Erro: {prefix}{message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "monthly":
            plan = build_monthly_plan(
                args.round_id,
                theirstack_budget=args.theirstack_budget,
                serpapi_budget=args.serpapi_budget,
                max_retries=args.max_retries,
            )
            if args.dry_run:
                if args.force:
                    raise ValueError("--force exige --confirm-live.")
                _print_monthly_plan(plan, dry_run=True)
                return 0
            if not args.confirm_live:
                raise ValueError("A coleta mensal real exige --confirm-live.")
            return _run_monthly(load_config(("theirstack", "serpapi")), args, plan)
        if args.command == "migrate":
            db.run_migrations(load_config().database_url)
            print("Migration aplicada no banco Python.")
            return 0
        if args.command == "backfill-publication-dates":
            return _run_backfill(load_config())
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
