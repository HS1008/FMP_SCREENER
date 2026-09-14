"""Normalization: units, OCC identity, Friday-as-Friday, null vs zero."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from market_intelligence.openbb_provider.client import RawChainFetch, RawCurveFetch
from market_intelligence.openbb_provider.normalize import normalize_chain
from market_intelligence.openbb_provider.vix import normalize_curve

NY = ZoneInfo("America/New_York")


def _raw(rows, *, fetched_at, meta=None, symbol="SPY"):
    return RawChainFetch(
        symbol=symbol,
        contracts=rows,
        metadata=meta or {"symbol": symbol, "current_price": 500.0, "last_trade_timestamp": "2026-09-11 16:00:00"},
        fetched_at=fetched_at if isinstance(fetched_at, datetime) else datetime.fromisoformat(str(fetched_at)),
        openbb_version="4.7.2",
    )


def _row(**kwargs):
    base = {
        "contract_symbol": "SPY260918C00500000",
        "expiration": "2026-09-18",
        "strike": 500,
        "option_type": "call",
        "implied_volatility": 0.16,
        "delta": 0.5,
        "gamma": 0.01,
        "open_interest": 10,
        "volume": 3,
        "bid": 1.2,
        "ask": 1.3,
        "contract_size": 100,
    }
    base.update(kwargs)
    return base


def test_friday_chain_fetched_monday_keeps_friday_session():
    monday = datetime(2026, 9, 14, 10, 0, tzinfo=NY)
    snap = normalize_chain(_raw([_row()], fetched_at=monday), clock=monday)
    assert snap.session_date.isoformat() == "2026-09-11"
    assert snap.observation_precision == "timestamp"


def test_zero_oi_preserved_missing_gamma_null():
    snap = normalize_chain(
        _raw(
            [_row(open_interest=0, volume=0, gamma=None, contract_symbol="SPY260918P00500000", option_type="put")],
            fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        )
    )
    contract = snap.contracts[0]
    assert contract.open_interest == 0
    assert contract.volume == 0
    assert contract.gamma is None


def test_negative_counts_and_crossed_quotes_flagged():
    snap = normalize_chain(
        _raw([_row(open_interest=-4, bid=2.5, ask=2.0)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY))
    )
    contract = snap.contracts[0]
    assert contract.open_interest is None
    assert contract.quote_quality == "CROSSED"


def test_adjusted_occ_excluded():
    snap = normalize_chain(
        _raw([_row(contract_symbol="SPY2609181C00500000", strike=500)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY))
    )
    assert snap.contracts[0].is_adjusted is True


def test_iv_stays_decimal():
    snap = normalize_chain(_raw([_row(implied_volatility=0.16)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    assert snap.contracts[0].iv_decimal == Decimal("0.16")


def test_same_content_hash_ignores_fetch_time():
    rows = [_row()]
    a = normalize_chain(_raw(rows, fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY)))
    b = normalize_chain(_raw(rows, fetched_at=datetime(2026, 9, 14, 9, 15, tzinfo=NY)))
    assert a.content_hash == b.content_hash


def test_unknown_as_of_when_metadata_lacks_timestamp():
    snap = normalize_chain(
        _raw([_row()], fetched_at=datetime(2026, 9, 14, 9, 15, tzinfo=NY), meta={"symbol": "SPY", "current_price": 500})
    )
    assert snap.observation_precision == "unknown"
    assert snap.observation_time_utc is None


def test_vix_month_precision_and_nearest_mismatch():
    raw = RawCurveFetch(
        symbol="VX_EOD",
        points=[
            {"expiration": "2026-09", "price": 15.2, "date": "2026-09-11"},
            {"expiration": "2026-10", "price": 16.1, "date": "2026-09-11"},
        ],
        metadata={"requested_date": "2026-09-10"},
        fetched_at=datetime(2026, 9, 14, 10, 0, tzinfo=NY),
    )
    snap = normalize_curve(raw)
    assert snap.points[0].expiration_precision == "month"
    assert snap.quality.get("not_official_settlement") is True or snap.level_type == "CBOE_VX_EOD_4PM_ET"


def test_missing_greeks_and_oi_stay_null():
    snap = normalize_chain(
        _raw(
            [_row(gamma=None, delta=None, theta=None, vega=None, rho=None, open_interest=None, last_trade_time=None)],
            fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        )
    )
    contract = snap.contracts[0]
    assert contract.gamma is None
    assert contract.delta is None
    assert contract.open_interest is None
    assert contract.last_trade_time is None


def test_partial_provider_response_keeps_valid_rows():
    snap = normalize_chain(
        _raw(
            [_row(), "not-a-row", _row(contract_symbol="SPY260918P00500000", option_type="put", delta=-0.5, gamma=0.01)],
            fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        )
    )
    assert len(snap.contracts) == 2
    assert any(row.get("reason") == "non_object_row" for row in snap.rejected)


def test_pandas_nat_last_trade_time_is_null():
    pandas = pytest.importorskip("pandas")
    snap = normalize_chain(
        _raw([_row(last_trade_time=pandas.NaT)], fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY))
    )
    assert snap.contracts[0].last_trade_time is None


def test_near_equal_occ_strike_is_kept_and_dropped_mismatches_do_not_hide_chain():
    snap = normalize_chain(
        _raw(
            [
                _row(contract_symbol="SPY260918C00450500", strike=450.5000000002),
                _row(contract_symbol="SPY260918P00450500", option_type="put", strike=999),
            ],
            fetched_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        )
    )
    assert any(c.contract_symbol == "SPY260918C00450500" for c in snap.contracts)
    assert snap.quality["identity_conflicts"] >= 1
    assert snap.complete is True
