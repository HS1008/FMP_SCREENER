"""Short-lived cache for information_schema view checks.

A dashboard page can ask whether the same view exists many times on one
connection. The answer is stored on that connection for ``VIEW_EXISTS_TTL_SECONDS``.
A new connection, or the same connection after the TTL, checks again, so a
migration is visible without a process restart.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text

VIEW_EXISTS_TTL_SECONDS = 60.0
_CACHE_KEY = "mi_view_exists"


def clear_view_exists_cache(conn: Any) -> None:
    info = getattr(conn, "info", None)
    if isinstance(info, dict):
        info.pop(_CACHE_KEY, None)


def view_exists(conn: Any, name: str) -> bool:
    info = getattr(conn, "info", None)
    cache: dict[str, tuple[float, bool]] | None = None
    if isinstance(info, dict):
        current = info.get(_CACHE_KEY)
        if not isinstance(current, dict):
            current = {}
            info[_CACHE_KEY] = current
        cache = current
    now = time.monotonic()
    if cache is not None:
        hit = cache.get(name)
        if hit is not None and now - hit[0] < VIEW_EXISTS_TTL_SECONDS:
            return hit[1]
    found = bool(
        conn.execute(
            text("SELECT 1 FROM information_schema.views WHERE table_name = :n"),
            {"n": name},
        ).first()
    )
    if cache is not None:
        cache[name] = (now, found)
    return found
