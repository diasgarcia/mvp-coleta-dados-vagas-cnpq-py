"""Redact credentials while preserving the shape of collected payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_NAMES = (
    r"authorization|proxy[-_ ]?authorization|cookie|set[-_ ]?cookie|"
    r"api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|secret"
)
_QUOTED_VALUE = re.compile(
    rf"(?i)(?P<prefix>[\"']?(?:{_SENSITIVE_NAMES})[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_HEADER_VALUE = re.compile(
    r"(?i)(\b(?:authorization|proxy[-_ ]?authorization|cookie|set[-_ ]?cookie)"
    r"\s*[:=]\s*)(?![\"'])[^\r\n,;]+"
)
_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|secret)\s*[:=]\s*)(?![\"'])[^\s,;&#}\"']+"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[^\s,;\"']+")
_URL_PASSWORD = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)")
_ENCODED_SECRET = re.compile(
    r"(?i)((?:api(?:_|%5f)?key|access(?:_|%5f)?token|refresh(?:_|%5f)?token|"
    r"client(?:_|%5f)?secret)%3d)(?:(?!%26|%23)[^&#\s])+"
)


def _secret_key(key: str) -> bool:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()
    parts = tuple(re.findall(r"[a-z0-9]+", snake))
    pairs = set(zip(parts, parts[1:], strict=False))
    return bool(
        {"authorization", "cookie", "password", "secret"}.intersection(parts)
        or {
            ("api", "key"),
            ("access", "token"),
            ("refresh", "token"),
            ("client", "secret"),
            ("proxy", "authorization"),
            ("set", "cookie"),
        }.intersection(pairs)
    )


def _quoted_replacement(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('prefix')}{quote}{REDACTED}{quote}"


def sanitize_text(value: str | None, known_secrets: Sequence[str | None] = ()) -> str | None:
    if value is None:
        return None
    clean = _URL_PASSWORD.sub(rf"\1{REDACTED}\3", value)
    clean = _QUOTED_VALUE.sub(_quoted_replacement, clean)
    clean = _HEADER_VALUE.sub(rf"\1{REDACTED}", clean)
    clean = _BEARER.sub(rf"\1{REDACTED}", clean)
    clean = _SECRET_VALUE.sub(rf"\1{REDACTED}", clean)
    clean = _ENCODED_SECRET.sub(rf"\1{REDACTED}", clean)
    for secret in known_secrets:
        if secret:
            clean = clean.replace(secret, REDACTED)
    return clean


def sanitize(value: Any, known_secrets: Sequence[str | None] = ()) -> Any:
    """Return a recursively sanitized copy without dropping unknown fields."""

    if isinstance(value, Mapping):
        return {
            sanitize_text(key, known_secrets) if isinstance(key, str) else key: (
                REDACTED
                if isinstance(key, str) and _secret_key(key)
                else sanitize(item, known_secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, known_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item, known_secrets) for item in value)
    return sanitize_text(value, known_secrets) if isinstance(value, str) else value
