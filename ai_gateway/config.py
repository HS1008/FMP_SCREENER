"""Gateway configuration. Reuses existing MI environment names; adds only used extras.

Defaults are the restrictive ones: loopback bind, ``external`` export policy, OAuth
enabled only for the PKCE flow, CORS disabled, no remote value sources.
"""

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
REMOTE_VALUE_SOURCES_ENV = "AI_GATEWAY_REMOTE_VALUE_SOURCES"
TRUST_PROXY_ENV = "AI_GATEWAY_TRUST_PROXY"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_RATE_LIMIT = 60
# Remote clients always get the restrictive policy. ``owner`` only ever applies to a
# proven local session (see ai_gateway.context.effective_export_mode).
DEFAULT_EXPORT_MODE = "external"
MAX_REQUEST_BYTES = 64 * 1024
MAX_HISTORY_ROWS = 500
MAX_LIST_ROWS = 500
MAX_RESPONSE_CHARS = 400_000

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _env(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (source.get(name) or default).strip()


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


def configured_export_mode(env: Mapping[str, str] | None = None) -> str:
    """Operator preference. Unknown values fail closed to ``external``.

    ``owner`` here is only a *permission* for local sessions; it never applies to a remote
    request (``ai_gateway.context.effective_export_mode`` decides per request).
    """
    mode = _env(EXPORT_MODE_ENV, DEFAULT_EXPORT_MODE, env=env).lower()
    if mode in {"owner", "external"}:
        return mode
    return DEFAULT_EXPORT_MODE


def export_mode(env: Mapping[str, str] | None = None) -> str:
    """Backward-compatible alias for :func:`configured_export_mode`."""
    return configured_export_mode(env)


def remote_value_sources(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Source ids with a human-confirmed remote export right. Empty by default.

    Listing a source here is a data-rights decision recorded by an operator after
    confirming the provider's terms; it is never inferred from authentication.
    """
    raw = _env(REMOTE_VALUE_SOURCES_ENV, env=env)
    if not raw:
        return ()
    return tuple(sorted({part.strip().upper() for part in raw.split(",") if part.strip()}))


def trust_proxy(env: Mapping[str, str] | None = None) -> bool:
    """Kept for compatibility. Owner mode is never granted because of this flag.

    Proxy headers always mark a request as remote. Setting this to 0 does not make a
    reverse-proxied or public-Host request local.
    """
    raw = _env(TRUST_PROXY_ENV, "1", env=env)
    return _truthy(raw)


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
    origins = tuple(part.strip() for part in raw.split(",") if part.strip())
    # A wildcard origin is never honoured; the gateway carries bearer tokens.
    return tuple(origin for origin in origins if origin != "*")


def oauth_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = _env(OAUTH_ENABLED_ENV, "1", env=env).lower()
    return raw not in {"0", "false", "no", "off"}


def is_loopback_host(host: str | None) -> bool:
    text = str(host or "").strip().lower()
    if not text:
        return False
    if text in LOOPBACK_HOSTS:
        return True
    return text.startswith("127.")
