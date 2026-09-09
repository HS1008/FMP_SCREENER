"""FINRA Query API client: OAuth2 client credentials, POST filters, pagination, redaction.

Official endpoints (FINRA Developer Center):
  token  POST https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials
  data   POST https://api.finra.org/data/group/{group}/name/{dataset}
  meta   GET  https://api.finra.org/metadata/group/{group}/name/{dataset}

Does not call TRAQS / TRACE file-download hosts, submission APIs, or undocumented paths.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Mapping

import requests

from market_intelligence.finra_catalog import (
    CAP_AVAILABLE,
    CAP_CONFIGURATION_REQUIRED,
    CAP_ENTITLEMENT_REQUIRED,
    CAP_TEMPORARILY_UNAVAILABLE,
    FINRA_API_BASE,
    FINRA_TOKEN_URL,
    QUERY_DATASETS,
    FinraDatasetSpec,
)

logger = logging.getLogger(__name__)

DEFAULT_PAGE_LIMIT = 5000
MAX_PAGES = 40
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
CLIENT_ID_ENVS = ("FINRA_CLIENT_ID", "FINRA_API_CLIENT_ID")
CLIENT_SECRET_ENVS = ("FINRA_CLIENT_SECRET", "FINRA_API_CLIENT_SECRET")
ENABLE_FLAGS = ("MI_FINRA_ENABLED", "MI_TRACE_ENABLED")


class FinraError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, capability: str | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.capability = capability


class FinraConfigurationError(FinraError):
    pass


def redact(text: str, *secrets: str | None) -> str:
    out = str(text)
    out = re.sub(r"(Bearer )\S+", r"\1[REDACTED]", out, flags=re.IGNORECASE)
    out = re.sub(r"(Basic )\S+", r"\1[REDACTED]", out, flags=re.IGNORECASE)
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def credentials_from_env(env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    env = os.environ if env is None else env
    client_id = next((str(env.get(name) or "").strip() for name in CLIENT_ID_ENVS if str(env.get(name) or "").strip()), None)
    client_secret = next((str(env.get(name) or "").strip() for name in CLIENT_SECRET_ENVS if str(env.get(name) or "").strip()), None)
    return client_id or None, client_secret or None


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    for name in ENABLE_FLAGS:
        if str(env.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def configured_from_env(env: Mapping[str, str] | None = None) -> bool:
    client_id, client_secret = credentials_from_env(env)
    return bool(client_id and client_secret)


@dataclass
class QueryPage:
    records: list[dict[str, Any]]
    http_status: int
    record_count: int
    offset: int
    limit: int
    record_max_limit: int | None = None


@dataclass
class DatasetProbe:
    group: str
    dataset: str
    environment: str
    http_status: int | None
    record_count: int | None
    capability_status: str
    schema_fields: list[str]
    latest_observation_date: date | None
    retrieved_at: str
    coverage_note: str
    error_redacted: str | None = None


class FinraClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 45.0,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        api_base: str = FINRA_API_BASE,
        token_url: str = FINRA_TOKEN_URL,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not client_id or not client_secret:
            raise FinraConfigurationError("FINRA client id/secret is not configured")
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._backoff = float(backoff_seconds)
        self._sleep = sleep
        self._api_base = api_base.rstrip("/")
        self._token_url = token_url
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.request_count = 0
        self.retry_count = 0

    def _redact(self, text: str) -> str:
        return redact(text, self._client_id, self._client_secret, self._token)

    def authenticate(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        raw = "{0}:{1}".format(self._client_id, self._client_secret).encode("utf-8")
        headers = {
            "Authorization": "Basic {0}".format(base64.b64encode(raw).decode("ascii")),
            "Accept": "application/json",
        }
        attempt = 0
        while True:
            self.request_count += 1
            try:
                response = self._session.post(self._token_url, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt >= self._max_retries:
                    raise FinraError("FINRA token transport failure: {0}".format(exc.__class__.__name__), retryable=True, capability=CAP_TEMPORARILY_UNAVAILABLE) from None
                attempt += 1
                self.retry_count += 1
                self._sleep(self._backoff * (2 ** (attempt - 1)))
                continue
            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                attempt += 1
                self.retry_count += 1
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else self._backoff * (2 ** (attempt - 1))
                self._sleep(min(wait, 30.0))
                continue
            if response.status_code in {401, 403}:
                raise FinraError("FINRA authentication failed (HTTP {0})".format(response.status_code), status=response.status_code, capability=CAP_CONFIGURATION_REQUIRED)
            if response.status_code >= 400:
                raise FinraError("FINRA token HTTP {0}".format(response.status_code), status=response.status_code, retryable=response.status_code in RETRYABLE_STATUSES, capability=CAP_TEMPORARILY_UNAVAILABLE)
            try:
                payload = response.json()
            except ValueError:
                raise FinraError("FINRA token response was not JSON", status=response.status_code) from None
            token = payload.get("access_token")
            if not token:
                raise FinraError("FINRA token response missing access_token", status=response.status_code, capability=CAP_CONFIGURATION_REQUIRED)
            expires = payload.get("expires_in")
            try:
                ttl = float(expires) if expires is not None else 3600.0
            except (TypeError, ValueError):
                ttl = 3600.0
            self._token = str(token)
            self._token_expires_at = now + max(60.0, ttl)
            logger.info("FINRA Query API authentication succeeded")
            return self._token

    def _auth_headers(self) -> dict[str, str]:
        token = self.authenticate()
        return {
            "Authorization": "Bearer {0}".format(token),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> requests.Response:
        url = self._api_base + path
        attempt = 0
        while True:
            self.request_count += 1
            try:
                response = self._session.request(method, url, headers=self._auth_headers(), json=json_body, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt >= self._max_retries:
                    raise FinraError("FINRA transport failure: {0}".format(exc.__class__.__name__), retryable=True, capability=CAP_TEMPORARILY_UNAVAILABLE) from None
                attempt += 1
                self.retry_count += 1
                self._sleep(self._backoff * (2 ** (attempt - 1)))
                continue
            if response.status_code == 401 and attempt == 0:
                self._token = None
                attempt += 1
                continue
            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                attempt += 1
                self.retry_count += 1
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else self._backoff * (2 ** (attempt - 1))
                self._sleep(min(wait, 30.0))
                continue
            return response

    def metadata(self, spec: FinraDatasetSpec) -> dict[str, Any]:
        path = "/metadata/group/{0}/name/{1}".format(spec.group, spec.dataset)
        response = self._request("GET", path)
        if response.status_code >= 400:
            raise FinraError("FINRA metadata HTTP {0}".format(response.status_code), status=response.status_code, capability=_capability_for_status(response.status_code))
        try:
            return response.json()
        except ValueError:
            return {}

    def query_page(self, spec: FinraDatasetSpec, *, start: date | None = None, end: date | None = None, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0, extra_filters: list[dict[str, Any]] | None = None) -> QueryPage:
        body: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}
        filters = list(extra_filters or [])
        if start is not None:
            # FINRA compareType greater/lesser are exclusive; shift one day so the window is inclusive.
            filters.append({"compareType": "greater", "fieldName": spec.date_field, "fieldValue": (start - timedelta(days=1)).isoformat()})
        if end is not None:
            filters.append({"compareType": "lesser", "fieldName": spec.date_field, "fieldValue": (end + timedelta(days=1)).isoformat()})
        if filters:
            body["compareFilters"] = filters
        path = "/data/group/{0}/name/{1}".format(spec.group, spec.dataset)
        response = self._request("POST", path, json_body=body)
        if response.status_code in {401, 403, 404}:
            raise FinraError("FINRA dataset HTTP {0}".format(response.status_code), status=response.status_code, capability=_capability_for_status(response.status_code))
        if response.status_code >= 400:
            raise FinraError("FINRA dataset HTTP {0}".format(response.status_code), status=response.status_code, retryable=response.status_code in RETRYABLE_STATUSES, capability=_capability_for_status(response.status_code))
        try:
            payload = response.json()
        except ValueError:
            raise FinraError("FINRA dataset response was not JSON", status=response.status_code) from None
        records = _as_records(payload)
        max_limit = _header_int(response.headers.get("Record-Max-Limit") or response.headers.get("record-max-limit"))
        return QueryPage(
            records=records,
            http_status=response.status_code,
            record_count=len(records),
            offset=offset,
            limit=limit,
            record_max_limit=max_limit,
        )

    def query_all(self, spec: FinraDatasetSpec, *, start: date | None = None, end: date | None = None, limit: int = DEFAULT_PAGE_LIMIT, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        last_page_full = False
        for _ in range(max_pages):
            page = self.query_page(spec, start=start, end=end, limit=limit, offset=offset)
            rows.extend(page.records)
            last_page_full = page.record_count >= limit
            if page.record_count < limit:
                return rows
            offset += page.record_count
        if last_page_full:
            raise FinraError(
                "FINRA pagination incomplete: reached max_pages={0} with a full last page ({1} accumulated rows)".format(max_pages, len(rows)),
                capability=CAP_TEMPORARILY_UNAVAILABLE,
            )
        return rows

    def probe_dataset(self, spec: FinraDatasetSpec, *, retrieved_at: str, environment: str = "production") -> DatasetProbe:
        if not spec.query_api:
            return DatasetProbe(
                group=spec.group,
                dataset=spec.dataset,
                environment=environment,
                http_status=None,
                record_count=None,
                capability_status=CAP_ENTITLEMENT_REQUIRED,
                schema_fields=[],
                latest_observation_date=None,
                retrieved_at=retrieved_at,
                coverage_note=spec.coverage_note,
                error_redacted=None,
            )
        try:
            page = self.query_page(spec, limit=spec.probe_limit, offset=0)
        except FinraError as exc:
            return DatasetProbe(
                group=spec.group,
                dataset=spec.dataset,
                environment=environment,
                http_status=exc.status,
                record_count=None,
                capability_status=exc.capability or CAP_TEMPORARILY_UNAVAILABLE,
                schema_fields=[],
                latest_observation_date=None,
                retrieved_at=retrieved_at,
                coverage_note=spec.coverage_note,
                error_redacted=self._redact(str(exc)),
            )
        fields: list[str] = []
        latest = None
        for row in page.records:
            for key in row:
                if key not in fields:
                    fields.append(key)
            raw_date = row.get(spec.date_field)
            parsed = _parse_date(raw_date)
            if parsed is not None and (latest is None or parsed > latest):
                latest = parsed
        missing = [name for name in spec.required_fields if name not in fields]
        if page.records and missing:
            return DatasetProbe(
                group=spec.group,
                dataset=spec.dataset,
                environment=environment,
                http_status=page.http_status,
                record_count=page.record_count,
                capability_status=CAP_TEMPORARILY_UNAVAILABLE,
                schema_fields=fields,
                latest_observation_date=latest,
                retrieved_at=retrieved_at,
                coverage_note=spec.coverage_note,
                error_redacted="probe rows missing required fields: {0}".format(", ".join(missing)),
            )
        return DatasetProbe(
            group=spec.group,
            dataset=spec.dataset,
            environment=environment,
            http_status=page.http_status,
            record_count=page.record_count,
            capability_status=CAP_AVAILABLE,
            schema_fields=fields,
            latest_observation_date=latest,
            retrieved_at=retrieved_at,
            coverage_note=spec.coverage_note,
        )

    def probe_catalog(self, *, retrieved_at: str, environment: str = "production") -> list[DatasetProbe]:
        return [self.probe_dataset(spec, retrieved_at=retrieved_at, environment=environment) for spec in QUERY_DATASETS]


def _capability_for_status(status: int | None) -> str:
    if status in {401}:
        return CAP_CONFIGURATION_REQUIRED
    if status in {403, 404}:
        return CAP_ENTITLEMENT_REQUIRED
    return CAP_TEMPORARILY_UNAVAILABLE


def _records_from_list(rows: list[Any]) -> list[dict[str, Any]]:
    """Accept only dict entries. Malformed members reject the page.

    An otherwise valid JSON array that contains nulls, strings, or other non-objects
    is incomplete: dropping those entries would silently lose coverage and must not
    advance a completed checkpoint.
    """
    valid: list[dict[str, Any]] = []
    rejected = 0
    for row in rows:
        if isinstance(row, dict):
            valid.append(row)
        else:
            rejected += 1
    if rejected:
        raise FinraError(
            "FINRA dataset response contained {0} malformed array entries; "
            "refusing incomplete page ({1} valid dicts kept only for diagnostics)".format(
                rejected, len(valid)
            ),
            capability=CAP_TEMPORARILY_UNAVAILABLE,
        )
    return valid


def _as_records(payload: Any) -> list[dict[str, Any]]:
    """Parse a FINRA Query API body. A JSON array (including empty) is valid.

    An object is valid only when it wraps a list under data/records/content.
    HTTP 200 plus `{}` or an unexpected object is not a successful empty dataset.
    Non-dict entries inside an otherwise valid array are not silently discarded.
    """
    if payload is None:
        raise FinraError("FINRA dataset response was null", capability=CAP_TEMPORARILY_UNAVAILABLE)
    if isinstance(payload, list):
        return _records_from_list(payload)
    if isinstance(payload, dict):
        for key in ("data", "records", "content"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return _records_from_list(inner)
        raise FinraError(
            "FINRA dataset response had unexpected JSON shape",
            capability=CAP_TEMPORARILY_UNAVAILABLE,
        )
    raise FinraError(
        "FINRA dataset response was not a JSON array or object",
        capability=CAP_TEMPORARILY_UNAVAILABLE,
    )


def _header_int(raw: str | None) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _parse_date(raw: Any) -> date | None:
    if raw is None:
        return None
    text = str(raw).strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
