"""Platform source contracts: EIA, COT, OpenFIGI, SEC metrics, GEX, no synthetic EIA."""

from __future__ import annotations

import json
from decimal import Decimal
from io import BytesIO

from data_sources.eia_wholesale import load_cached_or_fetch_eia_data
from market_intelligence.cot_client import COT_COMBINED_NOT_INGESTED, COT_DATASETS
from market_intelligence.derived_gex import exposure_magnitude, signed_proxy
from market_intelligence.eia_client import EIA_CATALOG, EIAClient, EIASeriesSpec, HUB_PRICE_UNAVAILABLE
from market_intelligence.ingest_cot import explode_row, net_and_pct
from market_intelligence.openfigi_client import OpenFIGIClient, classify_mapping, request_hash
from market_intelligence.sec_metrics import discrete_quarter_from_ytd, fcf, pe, refuse_summed_ytd_mix, ttm_from_four_quarters
from market_intelligence.source_policy import POLICIES


class _FakeResponse:
    def __init__(self, payload, headers=None):
        self._payload = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_production_eia_loader_never_returns_generated_samples(monkeypatch, tmp_path):
    import data_sources.eia_wholesale as eia

    monkeypatch.setattr(eia, "EIA_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(eia, "EIA_LOCAL_DIR", tmp_path / "local")
    power, gas, sample = load_cached_or_fetch_eia_data()
    assert sample is False
    assert power.empty and gas.empty


def test_eia_pagination_does_not_advance_when_incomplete():
    spec = EIA_CATALOG[0]
    pages = [
        {"response": {"total": 3, "data": [{"period": "2026-09-04", "value": "1"}, {"period": "2026-09-11", "value": "2"}]}},
    ]

    def opener(request, timeout=0):
        return _FakeResponse(pages[0])

    client = EIAClient("fake-eia-canary-key", opener=opener, min_interval_s=0)
    extract = client.fetch_pages(spec)
    assert extract["complete"] is False
    assert "fake-eia-canary-key" not in extract["request_url_redacted"]
    assert HUB_PRICE_UNAVAILABLE["status"] == "UNAVAILABLE"


def test_eia_complete_extract_and_numeric_strings():
    spec = EIASeriesSpec("X", "natural-gas/stor/wkly", {"duoarea": ["NUS"]}, "weekly", "bcf", "US")

    def opener(request, timeout=0):
        return _FakeResponse({"response": {"total": 1, "data": [{"period": "2026-09-11", "value": "312.5"}]}})

    extract = EIAClient("fake-eia-canary-key", opener=opener, min_interval_s=0).fetch_pages(spec)
    assert extract["complete"] is True
    assert extract["rows"][0]["value"] == "312.5"


def test_openfigi_does_not_pick_candidate_zero_when_ambiguous():
    payload = [{"data": [{"figi": "BBG1", "ticker": "A"}, {"figi": "BBG2", "ticker": "B"}]}]

    def opener(request, timeout=0):
        assert request.get_header("X-openfigi-apikey") == "fake-figi-canary"
        return _FakeResponse(payload)

    rows = OpenFIGIClient("fake-figi-canary", opener=opener, min_interval_s=0).map_jobs(
        [{"idType": "TICKER", "idValue": "AAA", "exchCode": "US"}]
    )
    assert rows[0]["status"] == "AMBIGUOUS"
    assert rows[0]["chosen"] is None
    assert len(rows[0]["candidates"]) == 2


def test_openfigi_transient_http_is_not_no_match():
    class Boom:
        def __init__(self):
            self.code = 503
            self.headers = {}

        def read(self):
            return b"{}"

    def opener(request, timeout=0):
        import urllib.error

        raise urllib.error.HTTPError(request.full_url, 503, "no", {}, BytesIO(b""))

    rows = OpenFIGIClient(None, opener=opener, min_interval_s=0).map_jobs([{"idType": "TICKER", "idValue": "ZZZZZZZ"}])
    assert rows[0]["status"] == "TRANSIENT_FAILURE"
    assert classify_mapping({}, None) == "NO_MATCH"
    job = {"idType": "TICKER", "idValue": "SPY", "exchCode": "US"}
    assert request_hash(job) == request_hash(dict(job))


def test_cot_families_are_distinct_and_combined_not_ingested():
    assert {item.futonly_or_combined for item in COT_DATASETS} == {"FutOnly"}
    assert all(item.futonly_or_combined == "Combined" for item in COT_COMBINED_NOT_INGESTED)
    dataset = COT_DATASETS[0]
    rows = explode_row(
        dataset,
        {
            "cftc_contract_market_code": "13874A",
            "report_date_as_yyyy_mm_dd": "2026-09-08",
            "open_interest_all": "1000",
            "dealer_positions_long_all": "10",
            "dealer_positions_short_all": "4",
            "dealer_positions_spread_all": "1",
            "asset_mgr_positions_long": "2",
            "asset_mgr_positions_short": "3",
            "asset_mgr_positions_spread": "0",
            "lev_money_positions_long": "5",
            "lev_money_positions_short": "6",
            "lev_money_positions_spread": "0",
            "other_rept_positions_long": "1",
            "other_rept_positions_short": "1",
            "other_rept_positions_spread": "0",
            "nonrept_positions_long": "2",
            "nonrept_positions_short": "2",
        },
    )
    assert {row["trader_category"] for row in rows} >= {"Dealer", "LevMoney"}
    assert all(row["report_family"] == "TFF" for row in rows)
    missing = net_and_pct(Decimal(1), Decimal(2), None)
    assert missing["status"] == "UNAVAILABLE" and missing["reason"] == "missing_oi"
    zero = net_and_pct(Decimal(1), Decimal(2), Decimal(0))
    assert zero["reason"] == "zero_oi"


def test_gex_zero_oi_is_not_missing_and_missing_oi_is_incomplete():
    ok = exposure_magnitude(gamma="0.01", open_interest="0", multiplier="100", spot="500")
    assert ok["status"] == "OK" and ok["value"] == Decimal("0")
    assert ok["dealer_gex"] is False
    missing = exposure_magnitude(gamma="0.01", open_interest=None, multiplier="100", spot="500")
    assert missing["status"] == "INCOMPLETE"
    proxy = signed_proxy(call_magnitude="10", put_magnitude="4")
    assert proxy["value"] == Decimal("6")
    assert "dealer" not in proxy["label"]


def test_sec_metrics_refuse_mixed_ytd_and_invalid_pe():
    assert refuse_summed_ytd_mix([1, 2, 3])["status"] == "AMBIGUOUS_SCOPE"
    q2 = discrete_quarter_from_ytd("30", "10")
    assert q2["value"] == Decimal("20")
    assert ttm_from_four_quarters(["1", "2", "3", "4"])["value"] == Decimal("10")
    bad = pe("10", "-1")
    assert bad["status"] == "INVALID_DENOMINATOR"
    cash = fcf("100", "40")
    assert cash["value"] == Decimal("60")


def test_source_policies_are_off_by_default():
    assert all(item.enabled is False for item in POLICIES)
    assert any(item.source_id == "FMP_LEGACY" for item in POLICIES)
    from market_intelligence.source_policy import persist_policies

    dry = persist_policies(None, dry_run=True)
    assert dry["dry_run"] is True and dry["enabled_any"] is False


def test_eia_unavailable_items_are_explicit():
    from market_intelligence.eia_client import EIA_CATALOG, EIA_UNAVAILABLE_ITEMS

    assert {item.series_id for item in EIA_CATALOG} >= {"EIA_NG_STOR_WKLY_US", "EIA_PET_STOC_WSTK_US", "EIA_ELEC_RTO_US48_DEMAND", "EIA_ELEC_RTO_US48_GENERATION", "EIA_ELEC_RTO_US48_INTERCHANGE"}
    assert {item["item"] for item in EIA_UNAVAILABLE_ITEMS} >= {"wholesale_hub_power_and_gas_prices", "natural_gas_production", "petroleum_production"}


def test_derived_curves_and_fx_conventions():
    from datetime import date

    from market_intelligence.derived_curves import calendar_spread, fx_mid, select_front_deferred

    picked = select_front_deferred(
        [{"con_id": 1, "local_symbol": "ESU6", "expiry": "2026-09-18"}, {"con_id": 2, "local_symbol": "ESZ6", "expiry": "2026-12-18"}],
        as_of=date(2026, 9, 13),
    )
    assert picked["front"]["local_symbol"] == "ESU6" and picked["deferred"]["local_symbol"] == "ESZ6"
    spread = calendar_spread(front_price="100", deferred_price="101", front_expiry=date(2026, 9, 18), deferred_expiry=date(2026, 12, 18), as_of=date(2026, 9, 13))
    assert spread["absolute_spread"] == Decimal("1")
    bad = calendar_spread(front_price="0", deferred_price="-1", front_expiry=None, deferred_expiry=None, as_of=None)
    assert bad["status"] == "UNAVAILABLE" and bad["ratio"] is None
    fx = fx_mid(pair="EURUSD", bid="1.1", ask="1.2", last="1.15", base="EUR", quote="USD")
    assert fx["mid"] == Decimal("1.15") and fx["last_used"] is False


def test_cot_availability_and_insufficient_history():
    from datetime import date, datetime, timezone

    from market_intelligence.cot_analytics import availability_status, crowding

    status = availability_status(position_date=date(2026, 9, 8), retrieved_at=datetime(2026, 9, 11, 20, tzinfo=timezone.utc))
    assert status["delayed_relative_to_normal_friday"] is False
    delayed = availability_status(position_date=date(2026, 9, 8), retrieved_at=datetime(2026, 9, 15, 20, tzinfo=timezone.utc))
    assert delayed["delayed_relative_to_normal_friday"] is True
    assert crowding(net_oi=Decimal("1"), history=[Decimal("1")])["reason"] == "insufficient_history"


def test_sec_events_are_rule_based_not_llm():
    from market_intelligence.sec_events import event_from_form
    from market_intelligence.sec_metrics import METRIC_CATALOG, metric_status_for_entity

    assert event_from_form("10-Q")["event_type"] == "earnings"
    assert event_from_form("8-K", item_codes=("2.02",))["event_type"] == "earnings"
    assert event_from_form("ZZZ")["status"] == "UNSUPPORTED"
    assert event_from_form("10-K")["guidance_or_surprise"] is None
    assert metric_status_for_entity("ROIC", "bank")["status"] == "UNSUPPORTED_ENTITY"
    assert "PE" in METRIC_CATALOG


def test_sec_company_facts_extract_is_bounded_and_does_not_invent_frames():
    from market_intelligence.ingest_sec import extract_company_facts

    payload = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {"accn": "0001-01", "fy": 2024, "fp": "FY", "form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "val": 100, "frame": "CY2024"},
                            {"accn": "0001-02", "fy": 2025, "fp": "FY", "form": "10-K/A", "start": "2025-01-01", "end": "2025-12-31", "val": 110},
                        ]
                    }
                },
                "CustomMadeUpConcept": {"units": {"USD": [{"val": 1, "end": "2025-12-31"}]}},
            }
        }
    }
    rows = extract_company_facts(payload, cik="0000320193")
    assert len(rows) == 1
    assert rows[0]["concept"] == "Revenues" and rows[0]["value"] == 110
    assert rows[0]["original_or_amended"] == "AMENDED"
    assert rows[0]["frame"] is None
    assert "CustomMadeUpConcept" not in {row["concept"] for row in rows}


def test_shared_tws_budget_respects_process_share(monkeypatch, tmp_path):
    monkeypatch.setenv("IBKR_BUDGET_PATH", str(tmp_path / "budget.json"))
    from ibkr_collector.budget import SHARES, acquire, release

    first = acquire("diagnostic", SHARES["diagnostic"])
    assert first.allowed is True
    blocked = acquire("diagnostic", 1)
    assert blocked.allowed is False and blocked.reason == "process_share"
    release("diagnostic", SHARES["diagnostic"])
    again = acquire("diagnostic", 1)
    assert again.allowed is True
    release("diagnostic", 1)
