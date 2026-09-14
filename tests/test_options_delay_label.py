"""Provider-neutral delay labels. OpenBB hashes stay on contract payload, not delay text."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from market_intelligence.ibkr_options import normalize_ibkr_chain
from market_intelligence.openbb_provider.analytics import compute_options_metrics
from market_intelligence.openbb_provider.client import RawChainFetch
from market_intelligence.openbb_provider.normalize import normalize_chain


def test_openbb_default_delay_label_is_cboe_delayed():
    raw = RawChainFetch(
        symbol="SPY",
        contracts=[
            {
                "contract_symbol": "SPY260918C00500000",
                "expiration": "2026-09-18",
                "strike": 500,
                "option_type": "call",
                "bid": 1.1,
                "ask": 1.2,
                "implied_volatility": 0.16,
                "open_interest": 10,
                "gamma": 0.01,
            }
        ],
        metadata={"current_price": 510},
        extra={},
        fetched_at=datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc),
    )
    chain = normalize_chain(raw, clock=datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc))
    assert chain.delay_label == "CBOE_DELAYED"
    assert chain.source_id == "OPENBB_CBOE_OPTIONS"
    assert compute_options_metrics(chain)["delay_label"] == "CBOE_DELAYED"


def test_ibkr_delay_label_uses_market_data_type_and_missing_fields_stay_missing():
    result = SimpleNamespace(
        symbol="SPY",
        quotes=[
            {
                "right": "C",
                "local_symbol": "SPY260918C00510000",
                "expiration": "20260918",
                "strike": 510,
                "bid": None,
                "ask": None,
                "last": None,
                "volume": None,
                "open_interest": None,
                "implied_volatility": None,
                "gamma": None,
            }
        ],
        underlying={"spot": 510, "market_data_type": "FROZEN"},
        collected_at="2026-09-14T22:10:00+00:00",
        market_data_types=["FROZEN"],
    )
    chain = normalize_ibkr_chain(result, clock=datetime(2026, 9, 14, 22, 10, tzinfo=timezone.utc))
    assert chain.source_id == "IBKR_OPTIONS"
    assert chain.delay_label == "IBKR_FROZEN"
    assert chain.contracts[0].bid is None
    assert chain.contracts[0].open_interest is None
    metrics = compute_options_metrics(chain)
    assert metrics["delay_label"] == "IBKR_FROZEN"
    assert metrics["gex_proxy"]["status"] == "UNAVAILABLE"
    assert Decimal("0") not in {chain.contracts[0].bid, chain.contracts[0].open_interest, chain.contracts[0].volume}
