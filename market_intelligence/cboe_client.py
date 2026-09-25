"""Cboe LiveVol All Access client.

Official contract (https://api.livevol.com/v1/docs/Home/Authentication and the All Access help pages):

- Token: POST https://id.livevol.com/connect/token
  Authorization: Basic base64(client_id:client_secret)
  Body: grant_type=client_credentials
- Data: https://api.livevol.com/v1/live/allaccess/...
  Authorization: Bearer <access_token>

Documented point costs used for the budget guard (per request, not per row):

- GET /allaccess/market/underlying-quotes: live 4, delayed 8, historical 3
- GET /allaccess/market/option-and-underlying-quotes: live 4, delayed 8, historical 3
- GET /allaccess/reference/expirations: 1

Historical means ``date`` is before the New York session date, on the live or delayed host.
Official token examples send ``grant_type=client_credentials`` only; ``scope=api.allaccess``
is tried as a fallback for IdentityServer clients that require it.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

TOKEN_URL = "https://id.livevol.com/connect/token"
# Trial / All Access accounts commonly start on the delayed host. Live requires SIP entitlements.
API_ROOT_LIVE = "https://api.livevol.com/v1/live/allaccess"
API_ROOT_DELAYED = "https://api.livevol.com/v1/delayed/allaccess"
API_ROOT = API_ROOT_DELAYED
SOURCE_ID = "CBOE_ALL_ACCESS"
ENABLE_FLAG = "MI_CBOE_ENABLED"
CLIENT_ID_ENV = "CBOE_CLIENT_ID"
CLIENT_SECRET_ENV = "CBOE_CLIENT_SECRET"
API_MODE_ENV = "CBOE_API_MODE"
NY = ZoneInfo("America/New_York")

STATUS_READY = "READY"
STATUS_AUTH_FAILED = "AUTH_FAILED"
STATUS_ENTITLEMENT_REQUIRED = "ENTITLEMENT_REQUIRED"
STATUS_TRIAL_LIMIT = "TRIAL_LIMIT"
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
STATUS_DISABLED = "DISABLED"
STATUS_CONFIGURED = "CONFIGURED"

UNDERLYING_QUOTES = "/market/underlying-quotes"
OPTION_QUOTES = "/market/option-and-underlying-quotes"
EXPIRATIONS = "/reference/expirations"

POINT_COST_LIVE = {
    UNDERLYING_QUOTES: 4,
    OPTION_QUOTES: 4,
    EXPIRATIONS: 1,
}
POINT_COST_DELAYED = {
    UNDERLYING_QUOTES: 8,
    OPTION_QUOTES: 8,
    EXPIRATIONS: 1,
}
POINT_COST_HISTORICAL = {
    UNDERLYING_QUOTES: 3,
    OPTION_QUOTES: 3,
    EXPIRATIONS: 1,
}
# Back-compat alias used by older tests / docs.
POINT_COST = {
    (UNDERLYING_QUOTES, False): 4,
    (UNDERLYING_QUOTES, True): 3,
    (OPTION_QUOTES, False): 4,
    (OPTION_QUOTES, True): 3,
    (EXPIRATIONS, False): 1,
    (EXPIRATIONS, True): 1,
}

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_POINT_BUDGET = 80
# Cloudflare on id.livevol.com rejects the stock Python-urllib User-Agent (error 1010).
USER_AGENT = "FMP-SCREENER-MI/1.0 (+writer; Cboe LiveVol All Access)"


class CboeError(RuntimeError):
    def __init__(self, capability: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.capability = capability
        self.http_status = http_status


class CboeAuthError(CboeError):
    pass


class CboeEntitlementError(CboeError):
    pass


class CboeTrialLimitError(CboeError):
    pass


class CboeRateLimitError(CboeError):
    pass


class CboeUnavailableError(CboeError):
    pass


class CboeMalformedPayload(CboeError):
    pass


class CboeBudgetError(CboeError):
    pass


@dataclass
class _Token:
    access_token: str
    expires_at: float


@dataclass
class CboeClient:
    client_id: str
    client_secret: str
    opener: Callable[..., Any] | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    point_budget: int = DEFAULT_POINT_BUDGET
    api_root: str = API_ROOT
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _token: _Token | None = field(default=None, init=False)
    _cache: dict[str, Any] = field(default_factory=dict, init=False)
    points_used: int = field(default=0, init=False)
    requests_made: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise CboeError(STATUS_CONFIGURATION_REQUIRED, "Cboe client credentials are missing")
        if any(ch.isspace() for ch in self.client_id + self.client_secret):
            raise CboeError(STATUS_CONFIGURATION_REQUIRED, "Cboe client credentials contain whitespace")
        mode = (os.environ.get(API_MODE_ENV) or "").strip().lower()
        if mode in {"live", "delayed"} and self.api_root in {API_ROOT, API_ROOT_LIVE, API_ROOT_DELAYED}:
            self.api_root = API_ROOT_LIVE if mode == "live" else API_ROOT_DELAYED
        self._opener = self.opener or urllib.request.urlopen

    def invalidate_token(self) -> None:
        self._token = None

    def access_token(self) -> str:
        now = self.clock()
        if self._token is not None and now < self._token.expires_at:
            return self._token.access_token
        payload = self._token_request()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise CboeMalformedPayload(STATUS_AUTH_FAILED, "token response missing access_token")
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        # Refresh one minute before the documented expiry, and never cache a dead token.
        skew = 60 if expires_in > 120 else max(0, expires_in // 5)
        self._token = _Token(token, now + max(1, expires_in - skew))
        return token

    def underlying_quotes(self, symbols: list[str], quote_date: date, *, session_date: date) -> Any:
        return self._get(
            UNDERLYING_QUOTES,
            {"symbols": ",".join(symbols), "date": quote_date.isoformat(), "seq_no": "0"},
            historical=quote_date < session_date,
        )

    def expirations(self, symbol: str) -> Any:
        return self._get(EXPIRATIONS, {"symbols": symbol}, historical=False)

    def option_quotes(self, *, symbol: str, root: str, quote_date: date, session_date: date, min_expiry: date, max_expiry: date, min_strike: float | None = None, max_strike: float | None = None) -> Any:
        params = {
            "symbol": symbol,
            "root": root,
            "date": quote_date.isoformat(),
            "min_expiry": min_expiry.isoformat(),
            "max_expiry": max_expiry.isoformat(),
            "seq_no": "0",
        }
        if min_strike is not None:
            params["min_strike"] = _strike(min_strike)
        if max_strike is not None:
            params["max_strike"] = _strike(max_strike)
        return self._get(OPTION_QUOTES, params, historical=quote_date < session_date)

    def _point_cost(self, path: str, *, historical: bool) -> int:
        if historical:
            return POINT_COST_HISTORICAL.get(path, 3)
        if "delayed" in (self.api_root or ""):
            return POINT_COST_DELAYED.get(path, 8)
        return POINT_COST_LIVE.get(path, 4)

    def _get(self, path: str, params: dict[str, str], *, historical: bool) -> Any:
        key = path + "?" + urllib.parse.urlencode(params)
        if key in self._cache:
            return self._cache[key]
        cost = self._point_cost(path, historical=historical)
        if self.points_used + cost > self.point_budget:
            raise CboeBudgetError(STATUS_TRIAL_LIMIT, "run point budget would be exceeded")
        token = self.access_token()
        url = self.api_root + path + "?" + urllib.parse.urlencode(params)
        payload = self._request_json(
            url,
            method="GET",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            data=None,
        )
        self.points_used += cost
        self.requests_made += 1
        self._cache[key] = payload
        return payload

    def _token_request(self) -> dict[str, Any]:
        raw = (self.client_id + ":" + self.client_secret).encode("utf-8")
        basic = base64.b64encode(raw).decode("ascii")
        # Official examples send grant_type only (no scope). Some All Access
        # IdentityServer clients additionally require scope=api.allaccess.
        forms = (
            {"grant_type": "client_credentials"},
            {"grant_type": "client_credentials", "scope": "api.allaccess"},
        )
        last_auth: CboeAuthError | None = None
        for form in forms:
            body = urllib.parse.urlencode(form).encode("utf-8")
            try:
                payload = self._request_json(
                    TOKEN_URL,
                    method="POST",
                    headers={
                        "Authorization": "Basic " + basic,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                    data=body,
                )
            except CboeAuthError as exc:
                last_auth = exc
                continue
            if not isinstance(payload, dict):
                raise CboeMalformedPayload(STATUS_AUTH_FAILED, "token response was not an object")
            return payload
        # Body-credential fallback (IdentityServer clients that reject Basic).
        form_with_client = {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret}
        try:
            payload = self._request_json(
                TOKEN_URL,
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data=urllib.parse.urlencode(form_with_client).encode("utf-8"),
            )
        except CboeAuthError as exc:
            raise last_auth or exc
        if not isinstance(payload, dict):
            raise CboeMalformedPayload(STATUS_AUTH_FAILED, "token response was not an object")
        return payload

    def _request_json(self, url: str, *, method: str, headers: dict[str, str], data: bytes | None) -> Any:
        delay = 0.4
        last: CboeError | None = None
        merged = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(url, data=data, headers=merged, method=method)
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    raw = response.read()
                    http_status = int(getattr(response, "status", None) or response.getcode() or 0) or None
                    headers_map = getattr(response, "headers", None)
                    content_type = ""
                    if headers_map is not None:
                        content_type = str(headers_map.get("Content-Type") or headers_map.get("content-type") or "")
            except urllib.error.HTTPError as exc:
                body = _read_error_body(exc)
                err = error_for_status(int(exc.code), body)
                last = err
                if attempt >= self.max_attempts or not _retryable(err):
                    raise err
                self.sleep(delay)
                delay *= 2
                continue
            except urllib.error.URLError:
                last = CboeUnavailableError(STATUS_UNAVAILABLE, "Cboe request timed out or was unreachable", http_status=None)
                if attempt >= self.max_attempts:
                    raise last
                self.sleep(delay)
                delay *= 2
                continue
            except TimeoutError:
                last = CboeUnavailableError(STATUS_UNAVAILABLE, "Cboe request timed out", http_status=None)
                if attempt >= self.max_attempts:
                    raise last
                self.sleep(delay)
                delay *= 2
                continue
            if not raw or not raw.strip():
                # LiveVol returns HTTP 204 when the request is accepted but no rows
                # match the symbol/date (common on trial when a product/date is empty).
                raise CboeUnavailableError(
                    STATUS_UNAVAILABLE,
                    "Cboe returned empty body (status={0}, content_type={1}, bytes=0)".format(
                        http_status or "unknown",
                        (content_type or "unknown")[:80],
                    ),
                    http_status=http_status,
                )
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                preview = raw[:80].decode("utf-8", "replace").replace("\n", " ")
                if "cloudflare" in preview.lower() or "cf-ray" in preview.lower() or preview.lstrip().startswith("<!"):
                    raise CboeUnavailableError(
                        STATUS_UNAVAILABLE,
                        "Cboe edge returned a non-API page (possible IP/WAF block; status={0}, content_type={1})".format(
                            http_status or "unknown",
                            (content_type or "unknown")[:40],
                        ),
                        http_status=http_status,
                    ) from exc
                raise CboeMalformedPayload(
                    STATUS_UNAVAILABLE,
                    "Cboe response was not JSON (status={0}, content_type={1}, preview={2})".format(
                        http_status or "unknown",
                        (content_type or "unknown")[:40],
                        preview[:60],
                    ),
                ) from exc
            return parsed
        raise last or CboeUnavailableError(STATUS_UNAVAILABLE, "Cboe request failed")


def session_date(now: datetime | None = None) -> date:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(NY).date()


def credentials_from_env(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    source = env if env is not None else os.environ
    client_id = str(source.get(CLIENT_ID_ENV, "")).strip().strip("\ufeff").strip('"').strip("'")
    client_secret = str(source.get(CLIENT_SECRET_ENV, "")).strip().strip("\ufeff").strip('"').strip("'")
    return client_id, client_secret


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return str(source.get(ENABLE_FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}


def probe_status(env: Mapping[str, str] | None = None) -> tuple[str, str, bool]:
    client_id, client_secret = credentials_from_env(env)
    if not client_id or not client_secret:
        return STATUS_CONFIGURATION_REQUIRED, "CBOE_CLIENT_ID and CBOE_CLIENT_SECRET are required on the writer", False
    if not enabled_from_env(env):
        return STATUS_DISABLED, "MI_CBOE_ENABLED is not set; credential presence is not activation", False
    return STATUS_CONFIGURED, "credentials present and adapter enabled; READY is recorded after a token succeeds", True


def error_for_status(status: int, body: str) -> CboeError:
    text = (body or "").lower()
    safe_detail = ""
    if body:
        # Keep a short non-secret diagnostic; strip anything that looks like a token/secret.
        safe_detail = " ".join(body.replace("\n", " ").split())[:120]
        for marker in ("access_token", "client_secret", "refresh_token", "authorization"):
            if marker in safe_detail.lower():
                safe_detail = "redacted_error_body"
                break
    if status in {401, 403}:
        if any(word in text for word in ("entitlement", "subscription", "not subscribed", "permission", "forbidden product", "scope")):
            return CboeEntitlementError(
                STATUS_ENTITLEMENT_REQUIRED,
                "Cboe entitlement rejected the request ({0})".format(safe_detail or status),
                http_status=status,
            )
        return CboeAuthError(
            STATUS_AUTH_FAILED,
            "Cboe rejected the credentials ({0})".format(safe_detail or status),
            http_status=status,
        )
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return CboeRateLimitError(STATUS_RATE_LIMITED, "Cboe rate limit", http_status=status)
    if any(word in text for word in ("trial", "point limit", "points exceeded", "quota", "daily limit")):
        return CboeTrialLimitError(STATUS_TRIAL_LIMIT, "Cboe trial or point limit", http_status=status)
    if status >= 500:
        return CboeUnavailableError(STATUS_UNAVAILABLE, "Cboe service unavailable", http_status=status)
    return CboeUnavailableError(
        STATUS_UNAVAILABLE,
        "Cboe request failed ({0})".format(safe_detail or status),
        http_status=status,
    )


def _retryable(err: CboeError) -> bool:
    return isinstance(err, (CboeRateLimitError, CboeUnavailableError)) and not isinstance(err, CboeTrialLimitError)


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:  # noqa: BLE001
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    # Never keep a credential-shaped blob from an error page.
    if "client_secret" in text.lower() or "access_token" in text.lower():
        return ""
    return text[:240]


def _strike(value: float) -> str:
    return "{0:.4f}".format(float(value)).rstrip("0").rstrip(".")


def prior_weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end - timedelta(days=1)
    while len(days) < count and cursor.year > 2000:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return days
