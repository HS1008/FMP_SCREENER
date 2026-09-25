"""Official EIA v2 client. Credential presence is not activation."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from scripts.protected_env import sanitize_url

EIA_SOURCE_ID = "EIA"
EIA_BASE = "https://api.eia.gov/v2"
EIA_CATALOG_VERSION = "eia_market_hub_v1"
EIA_MAX_ROWS = 5000
ENABLE_FLAG = "MI_EIA_ENABLED"
KEY_ENV = "EIA_API_KEY"
PLACEHOLDER_UNITS = "see series metadata"


@dataclass(frozen=True)
class EIASeriesSpec:
    series_id: str
    route: str
    facets: dict[str, list[str]]
    frequency: str
    units: str
    geographic_scope: str
    data_column: str = "value"
    status: str = "CONFIGURED"


# Verified route families from EIA API v2 documentation. Facets are conservative
# starting filters; ingest records actual metadata returned by /v2/{route}.
EIA_CATALOG: tuple[EIASeriesSpec, ...] = (
    EIASeriesSpec(
        "EIA_NG_STOR_WKLY_US",
        "natural-gas/stor/wkly",
        {"duoarea": ["NUS"]},
        "weekly",
        "billion cubic feet",
        "US",
    ),
    EIASeriesSpec(
        "EIA_PET_STOC_WSTK_US",
        "petroleum/stoc/wstk",
        {"duoarea": ["NUS"]},
        "weekly",
        "thousand barrels",
        "US",
    ),
    EIASeriesSpec(
        "EIA_ELEC_RTO_US48_DEMAND",
        "electricity/rto/region-data",
        {"respondent": ["US48"], "type": ["D"]},
        "hourly",
        "megawatthours",
        "US48",
    ),
    EIASeriesSpec(
        "EIA_ELEC_RTO_US48_GENERATION",
        "electricity/rto/region-data",
        {"respondent": ["US48"], "type": ["NG"]},
        "hourly",
        "megawatthours",
        "US48",
    ),
    EIASeriesSpec(
        "EIA_ELEC_RTO_US48_INTERCHANGE",
        "electricity/rto/region-data",
        {"respondent": ["US48"], "type": ["TI"]},
        "hourly",
        "megawatthours",
        "US48",
    ),
)

# Child production routes are not invented here. Discover via GET /v2/natural-gas
# and /v2/petroleum before adding a CONFIGURED series.
EIA_UNAVAILABLE_ITEMS: tuple[dict[str, str], ...] = (
    {
        "item": "wholesale_hub_power_and_gas_prices",
        "status": "UNAVAILABLE",
        "reason": "No exact official EIA v2 nodal/hub power-price feed is in this catalog. Legacy generated samples are excluded from production.",
    },
    {
        "item": "natural_gas_production",
        "status": "UNAVAILABLE",
        "reason": "Exact EIA v2 production child route is not verified in eia_market_hub_v1; do not guess /natural-gas/prod/*.",
    },
    {
        "item": "petroleum_production",
        "status": "UNAVAILABLE",
        "reason": "Exact EIA v2 production child route is not verified in eia_market_hub_v1; inventories use petroleum/stoc/wstk only.",
    },
)

HUB_PRICE_UNAVAILABLE = EIA_UNAVAILABLE_ITEMS[0]


def api_key_from_env(env: Mapping[str, str] | None = None) -> str | None:
    raw = str((env or os.environ).get(KEY_ENV, "")).strip()
    return raw or None


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    return str((env or os.environ).get(ENABLE_FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}


class EIAClient:
    def __init__(self, api_key: str, *, opener=None, timeout_s: float = 30.0, min_interval_s: float = 0.2) -> None:
        if not api_key or any(ch.isspace() for ch in api_key):
            raise ValueError("EIA API key missing")
        self._key = api_key
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout_s
        self._min_interval = min_interval_s
        self._last = 0.0

    def _get(self, url: str) -> dict[str, Any]:
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "FMP_SCREENER EIA v2 client"})
        self._last = time.monotonic()
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read()
                warning = response.headers.get("X-Warning") or ""
        except urllib.error.HTTPError as exc:
            raise RuntimeError("EIA HTTP {0} for {1}".format(exc.code, sanitize_url(url, self._key))) from None
        except urllib.error.URLError as exc:
            raise RuntimeError("EIA unreachable: {0}".format(exc.__class__.__name__)) from None
        payload = json.loads(body)
        if warning:
            payload.setdefault("_http_warning", warning)
        return payload

    def fetch_pages(self, spec: EIASeriesSpec, *, start: str | None = None, end: str | None = None, revision_overlap: int = 8) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        offset = 0
        total = None
        pages = 0
        while True:
            params: list[tuple[str, str]] = [
                ("api_key", self._key),
                ("frequency", spec.frequency),
                ("data[0]", spec.data_column),
                ("offset", str(offset)),
                ("length", str(EIA_MAX_ROWS)),
                ("sort[0][column]", "period"),
                ("sort[0][direction]", "asc"),
            ]
            if start:
                params.append(("start", start))
            if end:
                params.append(("end", end))
            for facet, values in spec.facets.items():
                for value in values:
                    params.append(("facets[{0}][]".format(facet), value))
            url = "{0}/{1}/data/?{2}".format(EIA_BASE, spec.route, urllib.parse.urlencode(params))
            payload = self._get(url)
            response = payload.get("response") or {}
            data = list(response.get("data") or [])
            total = response.get("total")
            warnings = payload.get("warnings") or response.get("warnings")
            pages += 1
            rows.extend(data)
            if len(data) < EIA_MAX_ROWS:
                break
            if total is not None and offset + len(data) >= int(total):
                break
            if pages > 50:
                raise RuntimeError("EIA pagination exceeded 50 pages for {0}".format(spec.series_id))
            offset += len(data)
        complete = True
        if total is not None and int(total) != len(rows):
            complete = False
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return {
            "series_id": spec.series_id,
            "route": spec.route,
            "rows": rows,
            "total": total,
            "complete": complete,
            "pages": pages,
            "warnings": warnings,
            "content_sha256": digest,
            "revision_overlap": revision_overlap,
            "request_url_redacted": sanitize_url(url, self._key),
        }
