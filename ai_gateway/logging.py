"""Structured audit logs. Never write tokens, passwords, URLs with credentials, or secrets."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from market_intelligence.nulls import strict_dumps

logger = logging.getLogger("ai_gateway")

_REDACT_KEYS = {
    "authorization",
    "token",
    "password",
    "secret",
    "api_key",
    "database_url",
    "access_token",
    "refresh_token",
    "code_verifier",
    "client_secret",
}


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in _REDACT_KEYS else _safe(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe(v) for v in obj]
    return obj


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    try:
        logger.info(strict_dumps(_safe(payload)))
    except Exception:  # noqa: BLE001 - logging must never raise into request handling
        logger.info(json.dumps({"event": event, "status": payload.get("status")}, default=str))


@contextmanager
def timed_call(event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    try:
        yield extra
    except Exception as exc:
        log_event(
            event,
            status="error",
            error_type=exc.__class__.__name__,
            latency_ms=int((time.perf_counter() - started) * 1000),
            **fields,
            **extra,
        )
        raise
    log_event(
        event,
        status=extra.get("status", "success"),
        latency_ms=int((time.perf_counter() - started) * 1000),
        **fields,
        **{k: v for k, v in extra.items() if k != "status"},
    )
