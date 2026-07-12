"""Environment configuration for the job collector."""

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


def _text(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_text(name: str) -> str:
    value = _text(name)
    if value is None:
        raise ValueError(f"Configuração obrigatória ausente: {name}.")
    return value


def _database_url() -> str:
    value = _required_text("DATABASE_URL")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ValueError("DATABASE_URL possui formato inválido.") from None

    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or port != 5433
        or parsed.path != "/job_market-py"
        or bool(parsed.params)
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError(
            "DATABASE_URL deve apontar para o banco Python job_market-py em localhost:5433."
        )
    return value


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _text(name, str(default))
    try:
        value = int(raw) if raw is not None else default
    except ValueError:
        raise ValueError(f"{name} deve ser um número inteiro.") from None

    if not minimum <= value <= maximum:
        raise ValueError(f"{name} deve estar entre {minimum} e {maximum}.")
    return value


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _text(name, str(default))
    try:
        value = float(raw) if raw is not None else default
    except ValueError:
        raise ValueError(f"{name} deve ser um número.") from None

    if not minimum <= value <= maximum:
        raise ValueError(f"{name} deve estar entre {minimum:g} e {maximum:g}.")
    return value


def load_config(required_sources: tuple[str, ...] = ()) -> Config:
    """Load and validate configuration without exposing any configured value.

    API keys remain optional for commands that do not call their provider, such as
    migrations. Pass ``("theirstack",)``, ``("serpapi",)`` or both when a command
    requires credentials.
    """

    load_dotenv(override=False)

    supported_sources = {"theirstack", "serpapi"}
    unknown_sources = sorted(set(required_sources) - supported_sources)
    if unknown_sources:
        names = ", ".join(unknown_sources)
        raise ValueError(f"Fonte obrigatória desconhecida: {names}.")

    theirstack_api_key = _text("THEIRSTACK_API_KEY")
    serpapi_api_key = _text("SERPAPI_API_KEY")

    if "theirstack" in required_sources and theirstack_api_key is None:
        raise ValueError("Configuração obrigatória ausente: THEIRSTACK_API_KEY.")
    if "serpapi" in required_sources and serpapi_api_key is None:
        raise ValueError("Configuração obrigatória ausente: SERPAPI_API_KEY.")

    return Config(
        database_url=_database_url(),
        theirstack_api_key=theirstack_api_key,
        serpapi_api_key=serpapi_api_key,
        theirstack_location_id=_integer(
            "THEIRSTACK_LOCATION_ID", 3_448_433, minimum=1, maximum=2_147_483_647
        ),
        theirstack_limit=_integer("THEIRSTACK_LIMIT", 5, minimum=1, maximum=10),
        theirstack_max_pages=_integer("THEIRSTACK_MAX_PAGES", 1, minimum=1, maximum=2),
        serpapi_query=_text("SERPAPI_QUERY", "software engineer") or "software engineer",
        serpapi_location=(
            _text("SERPAPI_LOCATION", "Sao Paulo,State of Sao Paulo,Brazil")
            or "Sao Paulo,State of Sao Paulo,Brazil"
        ),
        serpapi_max_pages=_integer("SERPAPI_MAX_PAGES", 1, minimum=1, maximum=2),
        http_timeout_seconds=_number("HTTP_TIMEOUT_SECONDS", 30, minimum=1, maximum=120),
        http_max_retries=_integer("HTTP_MAX_RETRIES", 2, minimum=0, maximum=3),
    )
