"""Small defensive sanitizer for persisted payloads and diagnostic text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_QUOTED_HEADER_PATTERN = re.compile(
    r"(?ix)"
    r"(?P<prefix>[\"']?\b(?:(?:proxy[-_ ]?)?authorization|set[-_ ]?cookie|cookie)"
    r"[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\r\n]*?)(?P=quote)"
)
_UNQUOTED_HEADER_PATTERN = re.compile(
    r"(?i)(\b(?:(?:proxy[-_ ]?)?authorization|set[-_ ]?cookie|cookie)\s*[:=]\s*)"
    r"(?![\"'])[^\r\n,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[^\s,;\"']+")
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)")
_QUOTED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?ix)"
    r"(?P<prefix>\b(?:"
    r"api[_-]?key|apikey|apiKey|"
    r"access[_-]?token|accessToken|"
    r"refresh[_-]?token|refreshToken|"
    r"client[_-]?secret|clientSecret|"
    r"password|secret"
    r")\b[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\r\n]*?)(?P=quote)"
)
_UNQUOTED_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?ix)"
    r"(\b(?:"
    r"api[_-]?key|apikey|apiKey|"
    r"access[_-]?token|accessToken|"
    r"refresh[_-]?token|refreshToken|"
    r"client[_-]?secret|clientSecret|"
    r"password|secret"
    r")\b[\"']?\s*[:=]\s*)"
    r"(?![\"'])[^\s,;&#}\"']+"
)
_ENCODED_SECRET_PATTERN = re.compile(
    r"(?ix)"
    r"((?:api(?:_|%5f)?key|access(?:_|%5f)?token|refresh(?:_|%5f)?token|"
    r"client(?:_|%5f)?secret)%3d)"
    r"(?:(?!%26|%23)[^&#\s])+"
)


def _is_secret_key(key: str) -> bool:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", snake_case.lower()) if part)
    compact = "".join(parts)

    if {"authorization", "cookie", "password", "secret"}.intersection(parts):
        return True

    sensitive_pairs = {
        ("api", "key"),
        ("access", "token"),
        ("refresh", "token"),
        ("client", "secret"),
        ("proxy", "authorization"),
        ("set", "cookie"),
    }
    adjacent_pairs = set(zip(parts, parts[1:], strict=False))
    if sensitive_pairs.intersection(adjacent_pairs):
        return True

    return compact in {
        "apikey",
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "password",
        "secret",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
    }


def sanitize_text(value: str | None) -> str | None:
    """Redact credentials embedded in text while retaining useful context."""

    if value is None:
        return None

    sanitized = _URL_CREDENTIAL_PATTERN.sub(rf"\1{REDACTED}\3", value)
    sanitized = _QUOTED_HEADER_PATTERN.sub(_redact_quoted_value, sanitized)
    sanitized = _UNQUOTED_HEADER_PATTERN.sub(rf"\1{REDACTED}", sanitized)
    sanitized = _BEARER_PATTERN.sub(rf"\1{REDACTED}", sanitized)
    sanitized = _QUOTED_SECRET_ASSIGNMENT_PATTERN.sub(_redact_quoted_value, sanitized)
    sanitized = _UNQUOTED_SECRET_ASSIGNMENT_PATTERN.sub(rf"\1{REDACTED}", sanitized)
    return _ENCODED_SECRET_PATTERN.sub(rf"\1{REDACTED}", sanitized)


def _redact_quoted_value(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('prefix')}{quote}{REDACTED}{quote}"


def sanitize(value: Any) -> Any:
    """Return a recursively sanitized copy of a JSON-like value."""

    if isinstance(value, Mapping):
        return {
            key: REDACTED if isinstance(key, str) and _is_secret_key(key) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    if isinstance(value, str):
        return sanitize_text(value)
    return value
