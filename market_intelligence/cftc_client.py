"""Public CFTC Commitments of Traders client. No API key required."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

CFTC_RESOURCE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CFTC_ATTRIBUTION = "CFTC Public Reporting Environment, Legacy Futures-Only Commitments of Traders."
USER_AGENT = "FMP_SCREENER MarketIntelligence (public COT ingest)"

WATCHLIST = (
    "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE",
    "WTI FINANCIAL CRUDE OIL - NEW YORK MERCANTILE EXCHANGE",
    "NATURAL GAS - NEW YORK MERCANTILE EXCHANGE",
    "GOLD - COMMODITY EXCHANGE INC.",
    "COPPER- #1 - COMMODITY EXCHANGE INC.",
)


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class CftcClient:
    def __init__(self, *, opener=None, timeout_s: float = 30.0) -> None:
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout_s

    def latest_rows(self, *, limit: int = 400) -> list[dict[str, Any]]:
        quoted = ",".join("'{0}'".format(name.replace("'", "''")) for name in WATCHLIST)
        query = urllib.parse.urlencode(
            {
                "$select": "market_and_exchange_names,report_date_as_yyyy_mm_dd,open_interest_all,"
                "noncomm_positions_long_all,noncomm_positions_short_all,comm_positions_long_all,"
                "comm_positions_short_all,nonrept_positions_long_all,nonrept_positions_short_all,"
                "change_in_noncomm_long_all,change_in_noncomm_short_all,commodity_name,futonly_or_combined",
                "$where": "market_and_exchange_names in ({0})".format(quoted),
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": str(int(limit)),
            }
        )
        req = urllib.request.Request(
            "{0}?{1}".format(CFTC_RESOURCE, query),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with self._opener(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("CFTC payload is not a list")
        rows = []
        for raw in payload:
            report = str(raw.get("report_date_as_yyyy_mm_dd") or "")[:10]
            if not report:
                continue
            long_nc = _int(raw.get("noncomm_positions_long_all"))
            short_nc = _int(raw.get("noncomm_positions_short_all"))
            rows.append(
                {
                    "market": raw.get("market_and_exchange_names"),
                    "report_date": report,
                    "open_interest": _int(raw.get("open_interest_all")),
                    "noncomm_long": long_nc,
                    "noncomm_short": short_nc,
                    "noncomm_net": None if long_nc is None or short_nc is None else long_nc - short_nc,
                    "comm_long": _int(raw.get("comm_positions_long_all")),
                    "comm_short": _int(raw.get("comm_positions_short_all")),
                    "nonrept_long": _int(raw.get("nonrept_positions_long_all")),
                    "nonrept_short": _int(raw.get("nonrept_positions_short_all")),
                    "change_noncomm_long": _int(raw.get("change_in_noncomm_long_all")),
                    "change_noncomm_short": _int(raw.get("change_in_noncomm_short_all")),
                    "commodity_name": raw.get("commodity_name"),
                    "report_type": raw.get("futonly_or_combined") or "FutOnly",
                }
            )
        return rows
