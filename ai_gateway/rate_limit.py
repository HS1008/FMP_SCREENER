"""In-process sliding-window rate limiter. Identity is a token fingerprint or client IP."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from ai_gateway.config import rate_limit_per_minute
from ai_gateway.errors import RATE_LIMITED, GatewayError


class SlidingWindowLimiter:
    def __init__(self, *, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, identity: str, limit: int) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._hits[identity]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                raise GatewayError(RATE_LIMITED, "rate limit exceeded", http_status=429)
            bucket.append(now)


LIMITER = SlidingWindowLimiter()


def enforce(identity: str, *, limit: int | None = None) -> None:
    LIMITER.check(identity or "anonymous", limit if limit is not None else rate_limit_per_minute())
