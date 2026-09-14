"""Bounded IBKR option-chain collector: no live TWS, no orders, no production enablement."""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from types import SimpleNamespace

from ibkr_collector.options import collect_bounded_chain, select_bounded_specs, select_param_row
from ibkr_collector.values import BLOCKED_ECLIENT_METHODS, classify_error
from market_intelligence.ibkr_options import IBKR_OPTIONS_SOURCE_ID, normalize_ibkr_chain
from market_intelligence.openbb_provider.analytics import compute_options_metrics


class FakeClient:
    def __init__(self) -> None:
        self._n = 1000
        self.contract_details: dict[int, list] = {}
        self.contract_details_done: dict[int, threading.Event] = {}
        self.opt_params: dict[int, list] = {}
        self.opt_params_done: dict[int, threading.Event] = {}
        self.ticks: dict[int, dict] = {}
        self.market_data_types: dict[int, int] = {}
        self.errors: list[dict] = []
        self.cancelled: list[int] = []
        self.mkt_data_calls: list[int] = []
        self.mkt_data_generic: list[str] = []
        self.md_type = None

    def next_req_id(self) -> int:
        self._n += 1
        return self._n

    def wait_event(self, req_id: int, store: dict[int, threading.Event]) -> threading.Event:
        event = threading.Event()
        event.set()
        store[req_id] = event
        return event

    def reqMarketDataType(self, code: int) -> None:
        self.md_type = code

    def reqContractDetails(self, req_id: int, contract) -> None:
        self.contract_details[req_id] = [{"con_id": 756733, "symbol": "SPY"}]

    def reqSecDefOptParams(self, req_id: int, symbol: str, fut_fop: str, sec_type: str, con_id: int) -> None:
        self.opt_params[req_id] = [
            {
                "exchange": "SMART",
                "trading_class": "SPY",
                "multiplier": "100",
                "expirations": ["20260910", "20260918", "20261016"],
                "strikes": [500.0, 505.0, 510.0, 515.0, 520.0],
            }
        ]

    def reqMktData(self, req_id: int, contract, generic: str, snapshot: bool, regulatory: bool, opts) -> None:
        self.mkt_data_calls.append(req_id)
        self.mkt_data_generic.append(str(generic or ""))
        self.ticks[req_id] = {
            "bid": 1.2,
            "ask": 1.3,
            "last": 1.25,
            "volume": 10,
            "open_interest": 100,
            "implied_volatility": 0.16,
            "delta": 0.5 if getattr(contract, "right", "C") == "C" else -0.5,
            "gamma": 0.02,
            "theta": -0.01,
            "vega": 0.12,
            "delayed_ticks": True,
            "local_symbol": "SPY   260918C00510000",
        }
        self.market_data_types[req_id] = 3

    def cancelMktData(self, req_id: int) -> None:
        self.cancelled.append(req_id)


def test_option_exercise_is_blocked_and_chain_apis_are_not():
    assert "exerciseOptions" in BLOCKED_ECLIENT_METHODS
    assert "reqSecDefOptParams" not in BLOCKED_ECLIENT_METHODS
    assert "reqMktData" not in BLOCKED_ECLIENT_METHODS
    assert classify_error(101) == "line_limit"
    assert classify_error(354) == "entitlement"
    assert classify_error(2187) == "info"
    assert classify_error(300) == "info"
    assert classify_error(10091) == "entitlement"


def test_select_param_row_prefers_smart_matching_class():
    rows = [
        {"exchange": "CBOE", "trading_class": "SPY", "expirations": ["a"], "strikes": [1]},
        {"exchange": "SMART", "trading_class": "SPY", "expirations": ["a", "b"], "strikes": [1, 2, 3]},
        {"exchange": "SMART", "trading_class": "SPXW", "expirations": ["a"] * 20, "strikes": [1] * 20},
    ]
    chosen = select_param_row(rows, "SPY")
    assert chosen is not None
    assert chosen["trading_class"] == "SPY"
    assert chosen["exchange"] == "SMART"


def test_bounded_slice_skips_past_expiry_and_caps_strikes():
    param = {
        "exchange": "SMART",
        "trading_class": "SPY",
        "multiplier": "100",
        "expirations": ["20260910", "20260918", "20261016", "20261120"],
        "strikes": [490.0, 500.0, 510.0, 520.0, 530.0],
    }
    specs = select_bounded_specs(symbol="SPY", param=param, spot=510.0, as_of=date(2026, 9, 14), max_expirations=2, atm_strikes_each_side=1)
    expiries = sorted({item.expiration for item in specs})
    strikes = sorted({item.strike for item in specs})
    rights = {item.right for item in specs}
    assert expiries == ["20260918", "20261016"]
    assert strikes == [500.0, 510.0, 520.0]
    assert rights == {"C", "P"}
    assert len(specs) == 12


def test_collect_bounded_chain_cancels_subscriptions_and_stays_delayed(monkeypatch):
    import ibkr_collector.options as options

    monkeypatch.setattr(options, "_stock_contract", lambda symbol: SimpleNamespace(symbol=symbol, conId=None, right="C"))
    monkeypatch.setattr(options, "_option_contract", lambda spec: SimpleNamespace(symbol=spec.symbol, right=spec.right, strike=spec.strike))
    client = FakeClient()
    result = collect_bounded_chain(
        client,
        "SPY",
        as_of=date(2026, 9, 14),
        spot=510.0,
        max_expirations=1,
        atm_strikes_each_side=1,
        quote_wait_sec=0.0,
        sleeper=lambda _s: None,
        clock=lambda: datetime(2026, 9, 14, 20, 10, tzinfo=timezone.utc),
    )
    assert client.md_type == 3
    assert client.mkt_data_generic[0] == ""
    assert all(item in {"", "101,106"} for item in client.mkt_data_generic)
    assert result.quality["quoted"] > 0
    assert result.quality["qualified"] > 0
    assert result.quality["generic_ticks"] == "101,106"
    assert set(client.cancelled) >= set(client.mkt_data_calls)
    assert "DELAYED" in result.market_data_types
    assert result.quality["export_scope"] == "INTERNAL_ONLY"
    assert result.quality["field_fills"]["bid"] > 0
    assert result.quality["field_fills"]["implied_volatility"] > 0
    summary_keys = result.quotes[0].keys()
    assert "bid" in summary_keys and "implied_volatility" in summary_keys and "open_interest" in summary_keys


def test_ibkr_quotes_normalize_to_canonical_chain_for_market_pulse_metrics(monkeypatch):
    import ibkr_collector.options as options

    monkeypatch.setattr(options, "_stock_contract", lambda symbol: SimpleNamespace(symbol=symbol, conId=None, right="C"))
    monkeypatch.setattr(options, "_option_contract", lambda spec: SimpleNamespace(symbol=spec.symbol, right=spec.right, strike=spec.strike))
    client = FakeClient()
    result = collect_bounded_chain(
        client,
        "SPY",
        as_of=date(2026, 9, 18),
        spot=510.0,
        max_expirations=1,
        atm_strikes_each_side=1,
        quote_wait_sec=0.0,
        sleeper=lambda _s: None,
        clock=lambda: datetime(2026, 9, 18, 20, 10, tzinfo=timezone.utc),
    )
    chain = normalize_ibkr_chain(result, clock=datetime(2026, 9, 18, 20, 10, tzinfo=timezone.utc))
    assert chain.quality["source_id"] == IBKR_OPTIONS_SOURCE_ID
    assert chain.quality["endpoint"].startswith("tws:")
    assert chain.contracts
    metrics = compute_options_metrics(chain)
    assert "iv_30d" in metrics or "gex" in metrics or "put_call" in metrics


def test_fetch_options_is_not_on_the_refresh_timer():
    from pathlib import Path

    refresh = (Path(__file__).resolve().parents[1] / "jobs" / "market_intelligence_refresh.py").read_text(encoding="utf-8")
    main = (Path(__file__).resolve().parents[1] / "ibkr_collector" / "__main__.py").read_text(encoding="utf-8")
    assert "fetch-options" in main
    assert "ibkr_collector.options" not in refresh
    assert "MI_IBKR_OPTIONS_ENABLED" not in refresh
    env = (Path(__file__).resolve().parents[1] / "deploy" / "market_intelligence" / "market_intelligence.env.example").read_text(encoding="utf-8")
    assert "MI_IBKR_OPTIONS_ENABLED=0" in env
    from ibkr_collector.options import SOURCE_ID

    assert SOURCE_ID == IBKR_OPTIONS_SOURCE_ID
