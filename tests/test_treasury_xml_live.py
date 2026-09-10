"""Smallest live Treasury XML read. Skipped when the network is blocked."""

from datetime import date

import pytest

from market_intelligence.treasury_xml import NOMINAL_DATA, TreasuryXmlClient, months_to_fetch


def test_live_treasury_month_has_dated_par_yields():
    client = TreasuryXmlClient(timeout=15)
    yyyymm = months_to_fetch(today=date.today(), lookback_months=1)[0]
    try:
        page = client.fetch_month(NOMINAL_DATA, yyyymm)
    except Exception as exc:
        pytest.skip("live Treasury XML unavailable: {0}".format(exc.__class__.__name__))
    dated = [p for p in page.points if p.value is not None and p.field_name == "BC_10YEAR"]
    if not dated:
        pytest.skip("Treasury XML returned no 10Y values for {0}".format(yyyymm))
    latest = max(p.observation_date for p in dated)
    assert latest <= date.today()
    assert dated[-1].series_id == "UST_NOM_10Y"
