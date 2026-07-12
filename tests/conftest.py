"""Global unit-test safeguards."""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make accidental TCP connections fail even if a test forgets to mock HTTP."""

    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("Acesso de rede real bloqueado durante os testes.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
