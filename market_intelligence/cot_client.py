"""Official CFTC Public Reporting Environment COT client (TFF + Disaggregated, futures-only)."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

CFTC_SOURCE_ID = "CFTC_COT"
COT_SOURCE_ID = CFTC_SOURCE_ID
CFTC_BASE = "https://publicreporting.cftc.gov/resource"
ENABLE_FLAG = "MI_COT_ENABLED"
PAGE_SIZE = 1000
COT_CATALOG_VERSION = "cftc_cot_v1"


@dataclass(frozen=True)
class COTDataset:
    dataset_id: str
    report_family: str
    futonly_or_combined: str
    description: str


# Official PRE dataset IDs (CFTC foundry). Combined reports are catalogued but not
# ingested alongside futures-only so overlapping universes are never summed.
COT_DATASETS: tuple[COTDataset, ...] = (
    COTDataset("gpe5-46if", "TFF", "FutOnly", "Traders in Financial Futures, futures only"),
    COTDataset("72hh-3qpy", "DISAGGREGATED", "FutOnly", "Disaggregated COT, futures only"),
)
COT_COMBINED_NOT_INGESTED = (
    COTDataset("yw9f-hn96", "TFF", "Combined", "TFF futures+options combined — not ingested in V1"),
    COTDataset("kh3c-gbw2", "DISAGGREGATED", "Combined", "Disaggregated combined — not ingested in V1"),
)


def enabled_from_env(env: Mapping[str, str] | None = None) -> bool:
    return str((env or os.environ).get(ENABLE_FLAG, "")).strip().lower() in {"1", "true", "yes", "on"}


class COTClient:
    def __init__(self, *, opener=None, timeout_s: float = 30.0) -> None:
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout_s

    def fetch_pages(self, dataset: COTDataset, *, since: str | None = None, contract_code: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            clauses = ["1=1"]
            if since:
                clauses.append("report_date_as_yyyy_mm_dd >= '{0}'".format(since))
            if contract_code:
                clauses.append("cftc_contract_market_code = '{0}'".format(contract_code.replace("'", "")))
            query = urllib.parse.urlencode(
                {
                    "$limit": str(PAGE_SIZE),
                    "$offset": str(offset),
                    "$order": "report_date_as_yyyy_mm_dd ASC",
                    "$where": " AND ".join(clauses),
                }
            )
            url = "{0}/{1}.json?{2}".format(CFTC_BASE, dataset.dataset_id, query)
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "FMP_SCREENER CFTC COT client"})
            with self._opener(request, timeout=self._timeout) as response:
                page = json.loads(response.read())
            if not isinstance(page, list) or not page:
                break
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += len(page)
            if offset > 200000:
                raise RuntimeError("CFTC pagination exceeded bound")
        return rows
