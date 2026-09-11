"""Per-request transport context that decides the effective export mode.

The export policy has two modes (``market_intelligence.export_policy``): ``external``
(restrictive; the only mode a remote client can receive) and ``owner`` (local private
session). The service layer never trusts a configuration value alone: it combines the
operator preference with *where the request came from*.

A request is LOCAL only when every one of these holds:

* the transport is stdio (a local MCP client spawned the process), or the HTTP client
  address is loopback;
* no reverse-proxy headers (``X-Forwarded-For``, ``X-Real-IP``, ``Forwarded``) are present
  while ``AI_GATEWAY_TRUST_PROXY`` is on (the default) — nginx on the same host forwards
  remote clients from 127.0.0.1;
* the gateway itself is bound to a loopback address (a non-loopback bind means the process
  is intentionally reachable from elsewhere, so it refuses owner mode entirely).

Anything else is REMOTE and gets ``external``.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Mapping

from market_intelligence.export_policy import EXPORT_MODE_EXTERNAL, EXPORT_MODE_OWNER, normalize_export_mode

from ai_gateway.config import (
    bind_host,
    configured_export_mode,
    is_loopback_host,
    remote_value_sources,
    trust_proxy,
)

TRANSPORT_HTTP = "http"
TRANSPORT_STDIO = "stdio"
TRANSPORT_UNKNOWN = "unknown"

PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")


@dataclass(frozen=True)
class RequestContext:
    transport: str = TRANSPORT_UNKNOWN
    client_host: str | None = None
    proxied: bool = False
    bind: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def is_local(self) -> bool:
        if self.transport == TRANSPORT_STDIO:
            return True
        if self.transport != TRANSPORT_HTTP:
            return False
        if self.proxied:
            return False
        if not is_loopback_host(self.client_host):
            return False
        return is_loopback_host(self.bind or bind_host())


# Default: unknown transport -> remote -> external. Services that run outside a request
# (tests, scripts) therefore see the restrictive policy unless they set the context.
_CURRENT: ContextVar[RequestContext] = ContextVar("ai_gateway_request_context", default=RequestContext())


def current_context() -> RequestContext:
    return _CURRENT.get()


def set_context(ctx: RequestContext):
    """Install ``ctx`` for the current task; returns a token for :func:`reset_context`."""
    return _CURRENT.set(ctx)


def reset_context(token) -> None:
    _CURRENT.reset(token)


def http_context(*, client_host: str | None, headers: Mapping[str, str] | None, bind: str | None = None) -> RequestContext:
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    has_proxy_header = any(name in lowered and str(lowered[name]).strip() for name in PROXY_HEADERS)
    proxied = bool(has_proxy_header and trust_proxy())
    return RequestContext(
        transport=TRANSPORT_HTTP,
        client_host=client_host,
        proxied=proxied,
        bind=bind or bind_host(),
        details={"proxy_headers_present": has_proxy_header},
    )


def stdio_context() -> RequestContext:
    return RequestContext(transport=TRANSPORT_STDIO, client_host="stdio", proxied=False, bind="stdio")


def effective_export_mode(ctx: RequestContext | None = None) -> str:
    """``owner`` only when the operator allowed it AND the session is provably local."""
    context = ctx if ctx is not None else current_context()
    preferred = normalize_export_mode(configured_export_mode())
    if preferred == EXPORT_MODE_OWNER and context.is_local():
        return EXPORT_MODE_OWNER
    return EXPORT_MODE_EXTERNAL


def effective_remote_value_sources(ctx: RequestContext | None = None) -> tuple[str, ...]:
    """Source-specific remote rights apply to remote requests; owner sessions need none."""
    return remote_value_sources()


def export_policy_for_request(ctx: RequestContext | None = None) -> dict[str, Any]:
    context = ctx if ctx is not None else current_context()
    mode = effective_export_mode(context)
    return {
        "export_mode": mode,
        "remote_value_sources": list(effective_remote_value_sources(context)),
        "session": "local_owner" if mode == EXPORT_MODE_OWNER else "remote_or_unproven",
        "transport": context.transport,
    }


__all__ = [
    "PROXY_HEADERS",
    "RequestContext",
    "TRANSPORT_HTTP",
    "TRANSPORT_STDIO",
    "TRANSPORT_UNKNOWN",
    "current_context",
    "effective_export_mode",
    "effective_remote_value_sources",
    "export_policy_for_request",
    "http_context",
    "reset_context",
    "set_context",
    "stdio_context",
]
