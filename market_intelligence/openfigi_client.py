"""OpenFIGI mapping client: event-driven, no candidate-zero default, no transient negative cache."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

OPENFIGI_SOURCE_ID = "OPENFIGI"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
ENABLE_FLAG = "MI_OPENFIGI_ENABLED"
KEY_ENV = "OPENFIGI_API_KEY"
CATALOG_VERSION = "openfigi_mapping_v1"
MAX_JOBS_WITH_KEY = 100
MAX_JOBS_WITHOUT_KEY = 10
NEGATIVE_TTL_HOURS = 24


def api_key_from_env(env: Mapping[str, str] | None = None) -> str | None:
    raw = str((env or os.environ).get(KEY_ENV, "")).strip()
    return raw or None


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    return str((env or os.environ).get(ENABLE_FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}


def request_hash(job: Mapping[str, Any]) -> str:
    payload = json.dumps(job, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def response_hash(payload: Any, secret: str | None) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str)
    if secret:
        raw = raw.replace(secret, "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_mapping(result: Mapping[str, Any] | None, error: str | None) -> str:
    if error:
        lowered = error.lower()
        if "invalid" in lowered:
            return "VALIDATION_FAILURE"
        return "TRANSIENT_FAILURE"
    data = list((result or {}).get("data") or [])
    if not data:
        warning = str((result or {}).get("warning") or "")
        if warning:
            return "NO_MATCH"
        return "NO_MATCH"
    if len(data) == 1:
        return "MATCH"
    return "AMBIGUOUS"


class OpenFIGIClient:
    def __init__(self, api_key: str | None = None, *, opener=None, timeout_s: float = 20.0, min_interval_s: float | None = None) -> None:
        self._key = api_key
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout_s
        self._last = 0.0
        self._interval = min_interval_s if min_interval_s is not None else (0.3 if api_key else 2.5)

    @property
    def max_jobs(self) -> int:
        return MAX_JOBS_WITH_KEY if self._key else MAX_JOBS_WITHOUT_KEY

    def map_jobs(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(jobs) > self.max_jobs:
            raise ValueError("OpenFIGI batch exceeds max_jobs={0}".format(self.max_jobs))
        wait = self._interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._key:
            headers["X-OPENFIGI-APIKEY"] = self._key
        request = urllib.request.Request(OPENFIGI_URL, data=json.dumps(jobs).encode("utf-8"), headers=headers, method="POST")
        self._last = time.monotonic()
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
                limit = response.headers.get("X-RateLimit-Limit") or response.headers.get("X-Rate-Limit-Limit")
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                return [{"status": "TRANSIENT_FAILURE", "error": "HTTP_{0}".format(exc.code)} for _ in jobs]
            if exc.code in {400, 401, 403}:
                return [{"status": "VALIDATION_FAILURE", "error": "HTTP_{0}".format(exc.code)} for _ in jobs]
            return [{"status": "TRANSIENT_FAILURE", "error": "HTTP_{0}".format(exc.code)} for _ in jobs]
        out = []
        for job, result in zip(jobs, body if isinstance(body, list) else [body]):
            if not isinstance(result, dict):
                out.append({"status": "TRANSIENT_FAILURE", "error": "malformed", "job": job})
                continue
            error = result.get("error")
            status = classify_mapping(result, error if isinstance(error, str) else None)
            data = list(result.get("data") or [])
            chosen = data[0] if status == "MATCH" else None
            out.append(
                {
                    "status": status,
                    "job": job,
                    "request_hash": request_hash(job),
                    "response_hash": response_hash(result, self._key),
                    "candidates": data,
                    "chosen": chosen,
                    "rate_limit": limit,
                    "expires_at": (datetime.now(timezone.utc) + timedelta(hours=NEGATIVE_TTL_HOURS)).isoformat() if status == "NO_MATCH" else None,
                }
            )
        return out
