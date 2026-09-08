"""FRED API adapter: deterministic requests, pagination, bounded retries, secret redaction.

Never logs the API key. Errors are wrapped in :class:`FredError` with redacted text.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

FRED_API_BASE = "https://api.stlouisfed.org/fred"
FRED_REALTIME_END_SENTINEL = "9999-12-31"
DEFAULT_PAGE_LIMIT = 10000
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class FredError(RuntimeError):
    """Redacted FRED failure (transport, HTTP, or payload shape)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class FredConfigurationError(FredError):
    """FRED_API_KEY is not configured."""


_KEY_PATTERN = re.compile(r"(api_key=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str, api_key: str | None = None) -> str:
    out = _KEY_PATTERN.sub(r"\1[REDACTED]", str(text))
    if api_key:
        out = out.replace(api_key, "[REDACTED]")
    return out


def api_key_from_env(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    key = (env.get("FRED_API_KEY") or "").strip()
    return key or None


@dataclass
class FredObservation:
    observation_date: date
    raw_value: str
    realtime_start: date | None
    realtime_end: date | None


def parse_fred_date(raw: Any) -> date | None:
    """Parse ``YYYY-MM-DD``; the 9999-12-31 realtime_end sentinel stays a plain ``date``.

    Never converted to a pandas timestamp (nanosecond range would overflow).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


class FredClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = FRED_API_BASE,
    ) -> None:
        if not api_key:
            raise FredConfigurationError("FRED_API_KEY is not configured")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._backoff = float(backoff_seconds)
        self._sleep = sleep
        self._base = base_url.rstrip("/")
        self.request_count = 0
        self.retry_count = 0

    # ---- transport -----------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = dict(sorted(params.items()))
        query["file_type"] = "json"
        query["api_key"] = self._api_key
        url = "{0}/{1}".format(self._base, path.lstrip("/"))
        attempt = 0
        while True:
            self.request_count += 1
            try:
                response = self._session.get(url, params=query, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt >= self._max_retries:
                    raise FredError("FRED transport failure: {0}".format(redact(exc.__class__.__name__, self._api_key)), retryable=True) from None
                attempt += 1
                self.retry_count += 1
                self._sleep(self._backoff * (2 ** (attempt - 1)))
                continue
            status = int(getattr(response, "status_code", 0) or 0)
            if status in RETRYABLE_STATUSES:
                if attempt >= self._max_retries:
                    raise FredError("FRED HTTP {0} after {1} retries".format(status, attempt), status=status, retryable=True)
                attempt += 1
                self.retry_count += 1
                retry_after = None
                try:
                    retry_after = float(response.headers.get("Retry-After", "") or 0) or None
                except (TypeError, ValueError):
                    retry_after = None
                self._sleep(retry_after if retry_after else self._backoff * (2 ** (attempt - 1)))
                continue
            if status >= 400:
                raise FredError("FRED HTTP {0} for {1}".format(status, path), status=status, retryable=False)
            try:
                payload = response.json()
            except ValueError:
                raise FredError("FRED returned non-JSON body for {0}".format(path), status=status) from None
            if not isinstance(payload, dict):
                raise FredError("FRED payload for {0} is not an object".format(path), status=status)
            return payload

    # ---- endpoints -----------------------------------------------------
    def series_metadata(self, series_id: str) -> dict[str, Any]:
        payload = self._get("series", {"series_id": series_id})
        items = payload.get("seriess")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise FredError("FRED series metadata missing for {0}".format(series_id))
        return items[0]

    def observations(
        self,
        series_id: str,
        *,
        observation_start: date | None = None,
        observation_end: date | None = None,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = 50,
    ) -> list[FredObservation]:
        params: dict[str, Any] = {
            "series_id": series_id,
            "sort_order": "asc",
            "units": "lin",
            "limit": int(page_limit),
        }
        if observation_start is not None:
            params["observation_start"] = observation_start.isoformat()
        if observation_end is not None:
            params["observation_end"] = observation_end.isoformat()
        out: list[FredObservation] = []
        offset = 0
        for _ in range(max_pages):
            page = self._get("series/observations", {**params, "offset": offset})
            rows = page.get("observations")
            if not isinstance(rows, list):
                raise FredError("FRED observations payload for {0} lacks 'observations'".format(series_id))
            for row in rows:
                if not isinstance(row, dict):
                    continue
                obs_date = parse_fred_date(row.get("date"))
                if obs_date is None:
                    raise FredError("FRED observation for {0} has unparseable date {1!r}".format(series_id, row.get("date")))
                out.append(
                    FredObservation(
                        observation_date=obs_date,
                        raw_value=str(row.get("value", "")),
                        realtime_start=parse_fred_date(row.get("realtime_start")),
                        realtime_end=parse_fred_date(row.get("realtime_end")),
                    )
                )
            count = page.get("count")
            offset += len(rows)
            if not rows or count is None or offset >= int(count):
                break
        else:
            raise FredError("FRED pagination for {0} exceeded {1} pages".format(series_id, max_pages))
        return out


__all__ = [
    "FRED_REALTIME_END_SENTINEL",
    "FredClient",
    "FredConfigurationError",
    "FredError",
    "FredObservation",
    "api_key_from_env",
    "parse_fred_date",
    "redact",
]
