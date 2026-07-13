"""Catálogo e plano declarativo da coleta mensal regional."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

REGIONS: tuple[dict[str, str | int], ...] = (
    {
        "key": "sao-paulo",
        "name": "São Paulo",
        "theirstack_location_id": 3448439,
        "serpapi_location": "Sao Paulo,State of Sao Paulo,Brazil",
    },
    {
        "key": "campinas",
        "name": "Campinas",
        "theirstack_location_id": 3467865,
        "serpapi_location": "Campinas,State of Sao Paulo,Brazil",
    },
    {
        "key": "sao-jose-dos-campos",
        "name": "São José dos Campos",
        "theirstack_location_id": 3448636,
        "serpapi_location": "Sao Jose dos Campos,State of Sao Paulo,Brazil",
    },
    {
        "key": "sorocaba",
        "name": "Sorocaba",
        "theirstack_location_id": 3447399,
        "serpapi_location": "Sorocaba,State of Sao Paulo,Brazil",
    },
    {
        "key": "ribeirao-preto",
        "name": "Ribeirão Preto",
        "theirstack_location_id": 3451328,
        "serpapi_location": "Ribeirao Preto,State of Sao Paulo,Brazil",
    },
    {
        "key": "santos",
        "name": "Santos",
        "theirstack_location_id": 3449433,
        "serpapi_location": "Santos,State of Sao Paulo,Brazil",
    },
    {
        "key": "bauru",
        "name": "Bauru",
        "theirstack_location_id": 3470279,
        "serpapi_location": "Bauru,State of Sao Paulo,Brazil",
    },
    {
        "key": "sao-jose-do-rio-preto",
        "name": "São José do Rio Preto",
        "theirstack_location_id": 3448639,
        "serpapi_location": "Sao Jose do Rio Preto,State of Sao Paulo,Brazil",
    },
)

SERPAPI_QUERIES = ("software engineer", "desenvolvedor de software")
THEIRSTACK_LIMIT = 10
THEIRSTACK_BUDGET = 80
SERPAPI_BUDGET = 16
MAX_PAGES = 1
DEFAULT_MAX_RETRIES = 0

_ROUND_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_EXPECTED_LOCATIONS = {
    "sao-paulo": "Sao Paulo,State of Sao Paulo,Brazil",
    "campinas": "Campinas,State of Sao Paulo,Brazil",
    "sao-jose-dos-campos": "Sao Jose dos Campos,State of Sao Paulo,Brazil",
    "sorocaba": "Sorocaba,State of Sao Paulo,Brazil",
    "ribeirao-preto": "Ribeirao Preto,State of Sao Paulo,Brazil",
    "santos": "Santos,State of Sao Paulo,Brazil",
    "bauru": "Bauru,State of Sao Paulo,Brazil",
    "sao-jose-do-rio-preto": "Sao Jose do Rio Preto,State of Sao Paulo,Brazil",
}


def validate_regions(regions: Sequence[dict[str, Any]] = REGIONS) -> None:
    """Falha cedo quando o catálogo está incompleto ou ambíguo."""
    if len(regions) != 8:
        raise ValueError("O catálogo mensal deve conter exatamente oito polos.")

    keys: set[str] = set()
    location_ids: set[int] = set()
    canonical_locations: set[str] = set()

    for region in regions:
        key = region.get("key")
        name = region.get("name")
        location_id = region.get("theirstack_location_id")
        canonical = region.get("serpapi_location")

        if not isinstance(key, str) or key not in _EXPECTED_LOCATIONS:
            raise ValueError("Polo com chave ausente ou desconhecida.")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Nome ausente para o polo {key}.")
        if not isinstance(location_id, int) or isinstance(location_id, bool) or location_id <= 0:
            raise ValueError(f"ID TheirStack inválido para o polo {key}.")
        if canonical != _EXPECTED_LOCATIONS[key]:
            raise ValueError(f"Localização canônica SerpApi inválida para o polo {key}.")
        if key in keys or location_id in location_ids or canonical in canonical_locations:
            raise ValueError(f"Polo duplicado no catálogo: {key}.")

        keys.add(key)
        location_ids.add(location_id)
        canonical_locations.add(canonical)


def build_monthly_plan(
    round_id: str,
    *,
    theirstack_budget: int = THEIRSTACK_BUDGET,
    serpapi_budget: int = SERPAPI_BUDGET,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """Monta o plano conservador sem acessar rede ou banco."""
    validate_regions()
    if not _ROUND_PATTERN.fullmatch(round_id):
        raise ValueError("A rodada deve usar o formato AAAA-MM.")
    if not 0 <= max_retries <= 3:
        raise ValueError("max_retries deve ficar entre 0 e 3.")

    theirstack = [
        {
            "source": "theirstack",
            "sample_region": region["key"],
            "query": "validated-technology-titles",
            "requested_location_name": region["name"],
            "requested_location_ids": [region["theirstack_location_id"]],
            "limit": THEIRSTACK_LIMIT,
            "max_pages": MAX_PAGES,
            "max_retries": max_retries,
        }
        for region in REGIONS
    ]
    serpapi = [
        {
            "source": "serpapi",
            "sample_region": region["key"],
            "query_origin": region["serpapi_location"],
            "canonical_location": region["serpapi_location"],
            "query": query,
            "max_pages": MAX_PAGES,
            "max_retries": max_retries,
        }
        for region in REGIONS
        for query in SERPAPI_QUERIES
    ]

    requested_items = len(theirstack) * THEIRSTACK_LIMIT
    searches = len(serpapi)
    if theirstack_budget < requested_items:
        raise ValueError(f"Orçamento TheirStack insuficiente: {requested_items} itens planejados.")
    if serpapi_budget < searches:
        raise ValueError(f"Orçamento SerpApi insuficiente: {searches} buscas planejadas.")

    return {
        "round_id": round_id,
        "collection_kind": "monthly",
        "regions": [dict(region) for region in REGIONS],
        "theirstack": theirstack,
        "serpapi": serpapi,
        "limits": {
            "theirstack_budget": theirstack_budget,
            "theirstack_requested_items": requested_items,
            "serpapi_budget": serpapi_budget,
            "serpapi_searches": searches,
            "max_pages": MAX_PAGES,
            "max_retries": max_retries,
        },
    }
