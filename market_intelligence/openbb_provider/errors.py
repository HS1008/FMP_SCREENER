"""Sanitized OpenBB/Cboe error categories. Never attach credentials or raw payloads."""

from __future__ import annotations


class OpenBBAcquisitionError(RuntimeError):
    """Provider acquisition failed. ``category`` drives retry vs fail-closed."""

    def __init__(self, message: str, *, category: str, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status = status


MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
TIMEOUT = "TIMEOUT"
RATE_LIMIT = "RATE_LIMIT"
ACCESS_DENIED = "ACCESS_DENIED"
SCHEMA = "SCHEMA"
EMPTY = "EMPTY"
PARTIAL = "PARTIAL"
NETWORK = "NETWORK"
DEADLINE = "DEADLINE"
DISABLED = "DISABLED"
ENTITLEMENT = "ENTITLEMENT"
UNKNOWN = "UNKNOWN"

NON_RETRYABLE = frozenset({MISSING_DEPENDENCY, ACCESS_DENIED, SCHEMA, DISABLED, ENTITLEMENT})


def classify_exception(exc: BaseException) -> tuple[str, bool]:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    if isinstance(exc, OpenBBAcquisitionError):
        return exc.category, exc.retryable
    if "timeout" in name or "timeout" in text:
        return TIMEOUT, True
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return RATE_LIMIT, True
    if any(token in text for token in ("401", "403", "forbidden", "unauthorized", "denied", "entitlement")):
        return ACCESS_DENIED, False
    if any(token in text for token in ("schema", "validation", "pydantic", "keyerror")):
        return SCHEMA, False
    if any(token in name for token in ("connect", "network", "http")) or "connection" in text:
        return NETWORK, True
    return UNKNOWN, True
