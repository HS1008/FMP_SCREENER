"""Choose and refresh the latest regular-session open. No TWS calls."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def choose_latest_open(bars: list[tuple[date, float]]) -> tuple[date, float] | None:
    """Latest bar with a positive open. Today's incomplete RTH bar counts."""
    usable = [(day, price) for day, price in bars if day is not None and price is not None and price > 0]
    if not usable:
        return None
    return max(usable, key=lambda item: item[0])


def needs_open_refresh(
    cached: dict[str, Any] | None,
    expected: date,
    now: datetime,
    *,
    min_retry_sec: float = 900.0,
) -> bool:
    """Refresh when the cached open is not the latest opened session.

    A failed or stale attempt waits ``min_retry_sec`` so historical requests
    are not repeated on every quote tick.
    """
    if cached and str(cached.get("session_date") or "") == expected.isoformat() and cached.get("open") not in (None, ""):
        return False
    fetched = _fetched_at(cached)
    if fetched is not None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = (now - fetched).total_seconds()
        if age < min_retry_sec:
            return False
    return True


def _fetched_at(cached: dict[str, Any] | None) -> datetime | None:
    if not cached:
        return None
    raw = cached.get("fetched_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
