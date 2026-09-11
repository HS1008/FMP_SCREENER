"""Smallest live Treasury XML read (one month, one request). Skipped when the network is blocked.

This is the bounded production-acceptance probe for the official Daily Treasury Par Yield
Curve XML feed. It performs exactly one HTTP request for the current month (a second one
for the previous month only when the current month has no complete curve yet, e.g. on the
first business day of a month). It never spams the provider and never writes a database.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from market_intelligence.treasury_xml import (
    COMPLETE_NOMINAL_TENORS,
    NOMINAL_DATA,
    TreasuryXmlClient,
    latest_complete_curve,
    months_to_fetch,
)

# The Treasury endpoint regularly answers in 15-25 seconds; a shorter timeout skips the
# probe on hosts where it would otherwise have passed.
LIVE_TIMEOUT_SECONDS = 60


def _fetch_points(client: TreasuryXmlClient, months: list[str]):
    points = []
    for yyyymm in months:
        page = client.fetch_month(NOMINAL_DATA, yyyymm)
        points.extend(page.points)
        latest, _legs, _by_date = latest_complete_curve(points)
        if latest is not None:
            break
    return points


def test_live_treasury_month_has_dated_par_yields():
    client = TreasuryXmlClient(timeout=LIVE_TIMEOUT_SECONDS)
    today = datetime.now(timezone.utc).date()
    months = months_to_fetch(today=today, lookback_months=2)
    try:
        points = _fetch_points(client, months)
    except Exception as exc:  # noqa: BLE001 - network is optional here
        pytest.skip("live Treasury XML unavailable: {0}".format(exc.__class__.__name__))
    dated = [p for p in points if p.value is not None and p.field_name == "BC_10YEAR"]
    if not dated:
        pytest.skip("Treasury XML returned no 10Y values for {0}".format(months))

    # No observation may sit in the future relative to the UTC calendar day.
    assert max(p.observation_date for p in points) <= today
    assert all(p.series_id == "UST_NOM_10Y" for p in dated)

    # A complete same-date nominal curve must exist and carry every required tenor.
    latest, legs, by_date = latest_complete_curve(points)
    assert latest is not None
    assert latest <= today
    assert set(COMPLETE_NOMINAL_TENORS) <= set(legs)
    assert all(legs[t].observation_date == latest for t in COMPLETE_NOMINAL_TENORS)
    assert all(legs[t].value is not None and 0 < float(legs[t].value) < 25 for t in COMPLETE_NOMINAL_TENORS)

    # Weekends never carry an observation.
    assert all(d.weekday() < 5 for d in by_date)

    # The Treasury does not publish a curve on Labor Day (first Monday of September).
    labor_day = date(latest.year, 9, 1)
    while labor_day.weekday() != 0:
        labor_day = labor_day.replace(day=labor_day.day + 1)
    if labor_day.month == 9 and any(d.month == 9 and d.year == latest.year for d in by_date):
        assert labor_day not in by_date
