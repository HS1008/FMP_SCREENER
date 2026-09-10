"""Treasury daily XML parse, nulls, revisions, same-date curves."""

from datetime import date
from decimal import Decimal

from market_intelligence.source_resolve import latest_common_observation_date, resolve_observation
from market_intelligence.treasury_xml import (
    COMPLETE_NOMINAL_TENORS,
    NOMINAL_FIELDS,
    latest_complete_curve,
    months_to_fetch,
    parse_feed_xml,
)

SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry>
    <content>
      <m:properties>
        <d:NEW_DATE>2026-09-09T00:00:00</d:NEW_DATE>
        <d:BC_3MONTH>3.80</d:BC_3MONTH>
        <d:BC_6MONTH>3.70</d:BC_6MONTH>
        <d:BC_1YEAR>3.60</d:BC_1YEAR>
        <d:BC_2YEAR>3.50</d:BC_2YEAR>
        <d:BC_3YEAR>3.45</d:BC_3YEAR>
        <d:BC_5YEAR>3.55</d:BC_5YEAR>
        <d:BC_7YEAR>3.70</d:BC_7YEAR>
        <d:BC_10YEAR>4.90</d:BC_10YEAR>
        <d:BC_20YEAR>5.00</d:BC_20YEAR>
        <d:BC_30YEAR>5.10</d:BC_30YEAR>
      </m:properties>
    </content>
  </entry>
  <entry>
    <content>
      <m:properties>
        <d:NEW_DATE>2026-09-10T00:00:00</d:NEW_DATE>
        <d:BC_3MONTH>3.81</d:BC_3MONTH>
        <d:BC_10YEAR m:null="true"></d:BC_10YEAR>
        <d:BC_2YEAR>3.51</d:BC_2YEAR>
      </m:properties>
    </content>
  </entry>
</feed>
"""


def test_parse_namespaces_nulls_and_no_zero_for_missing():
    page = parse_feed_xml(SAMPLE, curve="nominal")
    by = {(p.observation_date, p.field_name): p for p in page.points}
    assert by[(date(2026, 9, 9), "BC_10YEAR")].value == Decimal("4.90")
    assert by[(date(2026, 9, 10), "BC_10YEAR")].value is None
    assert by[(date(2026, 9, 10), "BC_10YEAR")].raw_value in {None, ""}
    assert "BC_4MONTH" not in {p.field_name for p in page.points if p.observation_date == date(2026, 9, 10) and p.value == 0}


def test_same_date_curve_does_not_mix_partial_newer():
    page = parse_feed_xml(SAMPLE, curve="nominal")
    latest, legs, by_date = latest_complete_curve(page.points)
    assert latest == date(2026, 9, 9)
    assert set(legs) >= set(COMPLETE_NOMINAL_TENORS)
    assert date(2026, 9, 10) in by_date
    assert "10Y" not in {t for t, p in by_date[date(2026, 9, 10)].items() if p.value is not None}


def test_months_rollover_not_hardcoded_year():
    months = months_to_fetch(today=date(2026, 1, 3), lookback_months=2)
    assert months == ["202601", "202512"]


def test_resolve_prefers_newer_then_treasury_tie():
    fred = {"series_id": "DGS10", "source_id": "FRED", "observation_date": date(2026, 9, 9), "value": 4.91}
    ust = {"series_id": "UST_NOM_10Y", "source_id": "TREASURY", "observation_date": date(2026, 9, 9), "value": 4.90}
    picked = resolve_observation("DGS10", [fred, ust])
    assert picked.source_id == "TREASURY"
    assert picked.selection_reason.startswith("tie_preference")
    newer_fred = dict(fred, observation_date=date(2026, 9, 10), value=4.95)
    picked2 = resolve_observation("DGS10", [newer_fred, ust])
    assert picked2.source_id == "FRED"
    assert picked2.observation_date == date(2026, 9, 10)


def test_latest_common_date_rejects_mixed_tenors():
    per_tenor = {
        "2Y": {date(2026, 9, 9): 3.5, date(2026, 9, 10): 3.51},
        "10Y": {date(2026, 9, 9): 4.90},
    }
    assert latest_common_observation_date(per_tenor, ("2Y", "10Y")) == date(2026, 9, 9)
    assert latest_common_observation_date({"2Y": {date(2026, 9, 10): 3.51}, "10Y": {date(2026, 9, 9): 4.9}}, ("2Y", "10Y")) is None
