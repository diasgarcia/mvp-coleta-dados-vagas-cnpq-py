from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    database_url: str = field(repr=False)
    theirstack_api_key: str | None = field(repr=False)
    serpapi_api_key: str | None = field(repr=False)
    theirstack_location_id: int
    theirstack_limit: int
    theirstack_max_pages: int
    serpapi_query: str
    serpapi_location: str
    serpapi_max_pages: int
    http_timeout_seconds: float
    http_max_retries: int


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _number(
    name: str, default: int | float, minimum: int | float, maximum: int | float
) -> int | float:
    convert = int if isinstance(default, int) else float
    try:
        value = convert(_env(name, str(default)) or default)
    except ValueError:
        kind = "um número inteiro" if convert is int else "um número"
        raise ValueError(f"{name} deve ser {kind}.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} deve estar entre {minimum:g} e {maximum:g}.")
    return value


def _database_url() -> str:
    value = _env("DATABASE_URL")
    if value is None:
        raise ValueError("Configuração obrigatória ausente: DATABASE_URL.")
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme in {"postgres", "postgresql"}
            and parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port == 5433
            and parsed.path == "/job_market-py"
            and not (parsed.params or parsed.query or parsed.fragment)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "DATABASE_URL deve apontar para o banco Python job_market-py em localhost:5433."
        )
    return value


def load_config(required_sources: tuple[str, ...] = ()) -> Config:
    load_dotenv(override=False)
    theirstack_key = _env("THEIRSTACK_API_KEY")
    serpapi_key = _env("SERPAPI_API_KEY")
    if "theirstack" in required_sources and not theirstack_key:
        raise ValueError("Configuração obrigatória ausente: THEIRSTACK_API_KEY.")
    if "serpapi" in required_sources and not serpapi_key:
        raise ValueError("Configuração obrigatória ausente: SERPAPI_API_KEY.")

    return Config(
        database_url=_database_url(),
        theirstack_api_key=theirstack_key,
        serpapi_api_key=serpapi_key,
        theirstack_location_id=_number("THEIRSTACK_LOCATION_ID", 3_448_433, 1, 2_147_483_647),
        theirstack_limit=_number("THEIRSTACK_LIMIT", 5, 1, 10),
        theirstack_max_pages=_number("THEIRSTACK_MAX_PAGES", 1, 1, 2),
        serpapi_query=_env("SERPAPI_QUERY", "software engineer") or "software engineer",
        serpapi_location=_env("SERPAPI_LOCATION", "Sao Paulo,State of Sao Paulo,Brazil")
        or "Sao Paulo,State of Sao Paulo,Brazil",
        serpapi_max_pages=_number("SERPAPI_MAX_PAGES", 1, 1, 2),
        http_timeout_seconds=_number("HTTP_TIMEOUT_SECONDS", 30.0, 1, 120),
        http_max_retries=_number("HTTP_MAX_RETRIES", 2, 0, 3),
    )
