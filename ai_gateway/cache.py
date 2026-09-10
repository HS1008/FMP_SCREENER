"""Short-lived in-process cache. Observation timestamps remain in every payload."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")

TTL_SECONDS = {
    "morning_context": 30,
    "market_pulse": 15,
    "rates_curve": 15,
    "macro_overview": 60,
    "macro_series": 60,
    "credit_overview": 30,
    "sector_rotation": 15,
    "sector_detail": 15,
    "industry_rotation": 30,
    "subindustry_rotation": 30,
    "order_flow": 30,
    "strategy_summary": 120,
    "strategy_oos_windows": 120,
    "strategy_experiments": 120,
    "data_health": 10,
    "market_changes": 15,
}


class TtlCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= now:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + max(0.0, float(ttl_seconds)), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


CACHE = TtlCache()


def cached(name: str, key: str, builder: Callable[[], T]) -> T:
    cache_key = "{0}:{1}".format(name, key)
    hit = CACHE.get(cache_key)
    if hit is not None:
        return hit
    value = builder()
    CACHE.set(cache_key, value, TTL_SECONDS.get(name, 15))
    return value
