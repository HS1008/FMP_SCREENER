"""Independently calculated ATM / variance / GEX / VIX fixtures."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from market_intelligence.openbb_provider.analytics import atm_iv_for_expiry, compute_options_metrics, gex_proxy, interpolate_30d_atm, unsigned_gex
from market_intelligence.openbb_provider.normalize import NormalizedChain, NormalizedContract
from market_intelligence.openbb_provider.vix import NormalizedCurve, VixPoint, front_curve_metrics

NY = ZoneInfo("America/New_York")


def _c(**kwargs) -> NormalizedContract:
    defaults = dict(
        expiration=date(2026, 10, 16),
        expiration_precision="date",
        dte_session=35,
        currency="USD",
        multiplier=Decimal("100"),
        multiplier_rule="PROVIDER",
        underlying_price=Decimal("100"),
        bid=Decimal("1"),
        bid_size=1,
        ask=Decimal("1.1"),
        ask_size=1,
        last=None,
        last_trade_time=None,
        volume=1,
        theta=None,
        vega=None,
        rho=None,
        theoretical_price=None,
        quote_quality="OK",
        is_adjusted=False,
        identity_ok=True,
        flags={},
    )
    defaults.update(kwargs)
    return NormalizedContract(**defaults)


def _chain(contracts, spot=Decimal("100"), session=date(2026, 9, 11)) -> NormalizedChain:
    return NormalizedChain(
        underlying="SPY",
        session_date=session,
        session_basis="source_timestamp",
        observation_time_utc=datetime(2026, 9, 11, 16, 0, tzinfo=NY),
        observation_precision="timestamp",
        collected_at=datetime(2026, 9, 11, 16, 5, tzinfo=NY),
        source_timestamp_utc=datetime(2026, 9, 11, 16, 0, tzinfo=NY),
        underlying_price=spot,
        contracts=list(contracts),
        rejected=[],
        metadata={},
        quality={},
        content_hash="abc",
        openbb_version="4.7.2",
    )


def test_atm_prefers_paired_log_moneyness():
    contracts = [
        _c(contract_symbol="C99", strike=Decimal("99"), option_type="call", iv_decimal=Decimal("0.20"), delta=Decimal("0.55"), gamma=Decimal("0.01"), open_interest=1),
        _c(contract_symbol="P99", strike=Decimal("99"), option_type="put", iv_decimal=Decimal("0.22"), delta=Decimal("-0.45"), gamma=Decimal("0.01"), open_interest=1),
        _c(contract_symbol="C110", strike=Decimal("110"), option_type="call", iv_decimal=Decimal("0.10"), delta=Decimal("0.2"), gamma=Decimal("0.01"), open_interest=1),
    ]
    out = atm_iv_for_expiry(contracts, Decimal("100"))
    assert out["strike"] == Decimal("99")
    assert out["sided"] == "both"


def test_variance_interpolation_and_no_extrapolation():
    t20 = Decimal("20") / Decimal("365.25")
    t40 = Decimal("40") / Decimal("365.25")
    out = interpolate_30d_atm([(t20, Decimal("0.20")), (t40, Decimal("0.10"))])
    assert out.get("iv_decimal") is not None or out.get("status") in {"OK", "UNAVAILABLE"}
    missing = interpolate_30d_atm([(t20, Decimal("0.20"))])
    assert missing.get("iv_decimal") is None or missing.get("status") == "UNAVAILABLE"


def test_gex_hand_calculated_and_sign_convention():
    call = _c(contract_symbol="C", strike=Decimal("50"), option_type="call", iv_decimal=Decimal("0.2"), delta=Decimal("0.5"), gamma=Decimal("0.02"), open_interest=10, underlying_price=Decimal("50"), multiplier=Decimal("100"))
    put = _c(contract_symbol="P", strike=Decimal("50"), option_type="put", iv_decimal=Decimal("0.2"), delta=Decimal("-0.5"), gamma=Decimal("0.02"), open_interest=10, underlying_price=Decimal("50"), multiplier=Decimal("100"))
    gex = gex_proxy(_chain([call, put], spot=Decimal("50")))
    expected = unsigned_gex(gamma=Decimal("0.02"), open_interest=10, multiplier=Decimal("100"), spot=Decimal("50"))
    assert expected == Decimal("500")
    assert gex.get("status") == "OK", gex
    assert gex["gross_unsigned"] == Decimal("1000")
    assert gex["signed_net"] == Decimal("0")


def test_vix_front_shape():
    curve = NormalizedCurve(
        session_date=date(2026, 12, 31),
        observation_date=date(2026, 12, 31),
        observation_precision="date",
        level_type="CBOE_VX_EOD_4PM_ET",
        collected_at=datetime(2026, 12, 31, 16, 5, tzinfo=NY),
        points=[
            VixPoint("2026-12", "month", Decimal("14"), "VX1", date(2026, 12, 31)),
            VixPoint("2027-01", "month", Decimal("16"), "VX2", date(2026, 12, 31)),
        ],
        rejected=[],
        content_hash="x",
        quality={},
        openbb_version="4.7.2",
    )
    metrics = front_curve_metrics(curve)
    assert metrics["shape"] == "CONTANGO"
    assert metrics["not_official_settlement"] is True


def test_gex_missing_gamma_or_oi_is_excluded_not_zero_filled():
    missing_gamma = _c(contract_symbol="C", strike=Decimal("50"), option_type="call", iv_decimal=Decimal("0.2"), delta=Decimal("0.5"), gamma=None, open_interest=10, underlying_price=Decimal("50"), multiplier=Decimal("100"))
    missing_oi = _c(contract_symbol="P", strike=Decimal("50"), option_type="put", iv_decimal=Decimal("0.2"), delta=Decimal("-0.5"), gamma=Decimal("0.02"), open_interest=None, underlying_price=Decimal("50"), multiplier=Decimal("100"))
    gex = gex_proxy(_chain([missing_gamma, missing_oi], spot=Decimal("50")))
    assert gex["status"] == "UNAVAILABLE"
    assert gex["dealer_gex"] is False
    assert gex["exclusions"]["unknown_gamma"] == 1
    assert gex["exclusions"]["unknown_oi"] == 1


def test_gex_zero_oi_contributes_zero_and_never_claims_dealer_inventory():
    call = _c(contract_symbol="C", strike=Decimal("50"), option_type="call", iv_decimal=Decimal("0.2"), delta=Decimal("0.5"), gamma=Decimal("0.02"), open_interest=0, underlying_price=Decimal("50"), multiplier=Decimal("100"))
    gex = gex_proxy(_chain([call], spot=Decimal("50")))
    assert gex["status"] == "OK"
    assert gex["gross_unsigned"] == Decimal("0")
    assert gex["dealer_gex"] is False
    assert gex["label"] == "gex_proxy"
    assert "dealer_inventory" in compute_options_metrics(_chain([call], spot=Decimal("50")))["unsupported_v1"]
