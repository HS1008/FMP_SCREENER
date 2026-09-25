"""Bounded Cboe entitlement probe. Never prints credentials or tokens.

Prints machine-readable capability lines for GitHub Actions logs.
Exit 0 when auth succeeds (partial data OK). Exit 2 on auth failure.
Exit 3 when credentials are missing.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from market_intelligence.cboe_analytics import expiry_window
from market_intelligence.cboe_client import (
    CboeClient,
    CboeError,
    credentials_from_env,
    prior_weekdays,
    session_date,
)


def _probe_underlying(client: CboeClient, symbol: str, day: date, as_of: date) -> None:
    label = "underlying:{0}:{1}".format(symbol, day.isoformat())
    try:
        payload = client.underlying_quotes([symbol], day, session_date=as_of)
        rows = len(payload) if isinstance(payload, list) else 1
        level = None
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            level = payload[0].get("underlying_close") or payload[0].get("underlying_last_trade_price")
        print(
            "probe={0}:SUCCEEDED:rows={1}:level_present={2}:points={3}".format(
                label, rows, level is not None, client.points_used
            )
        )
    except CboeError as exc:
        print(
            "probe={0}:{1}:http={2}:points={3}:detail={4}".format(
                label,
                exc.capability,
                exc.http_status,
                client.points_used,
                str(exc)[:100].replace("=", ":"),
            )
        )


def _probe_spx_options(client: CboeClient, day: date, as_of: date) -> None:
    label = "options:SPX:{0}".format(day.isoformat())
    try:
        lo, hi = expiry_window(day)
        payload = client.option_quotes(
            symbol="SPX",
            root="SPX",
            quote_date=day,
            session_date=as_of,
            min_expiry=lo,
            max_expiry=hi,
            min_strike=4000.0,
            max_strike=7000.0,
        )
        rows = len(payload) if isinstance(payload, list) else (1 if payload else 0)
        print("probe={0}:SUCCEEDED:rows={1}:points={2}".format(label, rows, client.points_used))
    except CboeError as exc:
        print(
            "probe={0}:{1}:http={2}:points={3}:detail={4}".format(
                label,
                exc.capability,
                exc.http_status,
                client.points_used,
                str(exc)[:100].replace("=", ":"),
            )
        )


def main() -> int:
    client_id, client_secret = credentials_from_env()
    if not client_id or not client_secret:
        print("auth=CONFIGURATION_REQUIRED")
        return 3
    as_of = session_date()
    client = CboeClient(client_id, client_secret, point_budget=40)
    print("api_root={0}".format(client.api_root))
    print("session_date={0}".format(as_of.isoformat()))
    try:
        token = client.access_token()
        print("auth=SUCCEEDED" if token else "auth=FAILED")
    except CboeError as exc:
        print(
            "auth={0}:http={1}:detail={2}".format(
                exc.capability, exc.http_status, str(exc)[:120].replace("=", ":")
            )
        )
        return 2
    # Documented example: proves historical equity underlying-quotes entitlement.
    _probe_underlying(client, "AAPL", date(2019, 1, 18), as_of)
    recent = prior_weekdays(as_of, 1)[0] if prior_weekdays(as_of, 1) else as_of - timedelta(days=1)
    _probe_underlying(client, "AAPL", recent, as_of)
    for symbol in ("VIX", "VIX9D", "SPX", "SPY"):
        _probe_underlying(client, symbol, recent, as_of)
    # One bounded SPX option snapshot for 25Δ skew entitlement.
    _probe_spx_options(client, recent, as_of)
    print("points_used={0}".format(client.points_used))
    print("requests_made={0}".format(client.requests_made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
