"""Small shared fixtures and a hard network guard."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from job_collector.config import Config


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Acesso de rede real bloqueado durante os testes.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def fixture_json() -> Callable[[str], Any]:
    directory = Path(__file__).parent / "fixtures"
    return lambda name: json.loads((directory / name).read_text(encoding="utf-8"))


@pytest.fixture
def config() -> Config:
    return Config(
        "postgresql://postgres:postgres@localhost:5433/job_market-py",
        "their-secret",
        "serp-secret",
        3_448_433,
        5,
        1,
        "software engineer",
        "Sao Paulo,State of Sao Paulo,Brazil",
        1,
        30,
        0,
    )
