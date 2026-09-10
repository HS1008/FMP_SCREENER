"""Gateway configuration. Reuses existing MI environment names; adds only used extras."""

from __future__ import annotations

import os
from typing import Mapping

TOKEN_ENV = "AI_CONTEXT_API_TOKEN"
READONLY_URL_ENV = "DATABASE_READONLY_URL"
HOST_ENV = "AI_CONTEXT_API_HOST"
PORT_ENV = "AI_CONTEXT_API_PORT"
PUBLIC_BASE_URL_ENV = "AI_GATEWAY_PUBLIC_BASE_URL"
EXPORT_MODE_ENV = "AI_GATEWAY_EXPORT_MODE"
RATE_LIMIT_ENV = "AI_GATEWAY_RATE_LIMIT_PER_MINUTE"
CORS_ORIGINS_ENV = "AI_GATEWAY_CORS_ORIGINS"
OAUTH_ENABLED_ENV = "AI_GATEWAY_OAUTH_ENABLED"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_RATE_LIMIT = 60
DEFAULT_EXPORT_MODE = "owner"
MAX_REQUEST_BYTES = 64 * 1024
MAX_HISTORY_ROWS = 500
MAX_LIST_ROWS = 500
MAX_RESPONSE_CHARS = 400_000


def _env(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(name) or default).strip()


def server_token(env: Mapping[str, str] | None = None) -> str | None:
    token = _env(TOKEN_ENV, env=env)
    return token or None


def bind_host(env: Mapping[str, str] | None = None) -> str:
    return _env(HOST_ENV, DEFAULT_HOST, env=env) or DEFAULT_HOST


def bind_port(env: Mapping[str, str] | None = None) -> int:
    raw = _env(PORT_ENV, str(DEFAULT_PORT), env=env) or str(DEFAULT_PORT)
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


def public_base_url(env: Mapping[str, str] | None = None) -> str | None:
    url = _env(PUBLIC_BASE_URL_ENV, env=env).rstrip("/")
    return url or None


def export_mode(env: Mapping[str, str] | None = None) -> str:
    mode = _env(EXPORT_MODE_ENV, DEFAULT_EXPORT_MODE, env=env).lower()
    if mode in {"owner", "external"}:
        return mode
    return DEFAULT_EXPORT_MODE


def rate_limit_per_minute(env: Mapping[str, str] | None = None) -> int:
    raw = _env(RATE_LIMIT_ENV, str(DEFAULT_RATE_LIMIT), env=env) or str(DEFAULT_RATE_LIMIT)
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RATE_LIMIT
    return max(1, min(value, 1000))


def cors_origins(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = _env(CORS_ORIGINS_ENV, env=env)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def oauth_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = _env(OAUTH_ENABLED_ENV, "1", env=env).lower()
    return raw not in {"0", "false", "no", "off"}
