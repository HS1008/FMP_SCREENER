"""Regression coverage for live 1D return/RS, Yahoo fallback provenance, UI boundaries."""

from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from market_intelligence.equity_live import (
    attach_live_1d_to_sector_rows,
    preferred_canonical_sector_rows,
    subgroup_rows_for_parent,
)
from market_intelligence.live_session import (
    REQUIRED_SECTOR_LIVE_SYMBOLS,
    SOURCE_EQUITY_EOD,
    SOURCE_IBKR,
    SOURCE_YAHOO_EOD,
    SOURCE_YAHOO_LIVE,
    equal_dollar_live_return,
    format_live_quotes_as_of,
    in_regular_trading_hours,
    is_usable_current_quote,
    live_relative_strength,
    live_return,
    live_rs_from_prices,
    live_session_pair,
    normalize_pair_to_100,
    normalize_to_100,
    resolve_current_price,
    resolve_prior_close,
)
from market_intelligence.taxonomy import EQUAL_WEIGHT_SPX, UNIVERSE_SYMBOLS
from ibkr_collector.historical import PRIMARY_EXCHANGE

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


def test_live_return_basic():
    assert live_return(105, 100) == pytest.approx(0.05)
    assert live_return(100, 100) == pytest.approx(0.0)
    assert live_return(None, 100) is None
    assert live_return(105, 0) is None


def test_live_rs_is_ratio_not_arithmetic_excess():
    # Asset +5%, bench +2% → 1.05/1.02 - 1, not 3%.
    assert live_relative_strength(0.05, 0.02) == pytest.approx(1.05 / 1.02 - 1)
    assert live_rs_from_prices(105, 100, 102, 100) == pytest.approx(1.05 / 1.02 - 1)
    assert live_relative_strength(0.05, 0.02) != pytest.approx(0.03)


def test_session_alignment_rejects_mismatched_prior_closes():
    from market_intelligence.live_session import sessions_aligned

    assert sessions_aligned(date(2026, 9, 15), date(2026, 9, 15)) is True
    assert sessions_aligned(date(2026, 9, 15), date(2026, 9, 14)) is False
    assert sessions_aligned(None, date(2026, 9, 15)) is False


def test_live_session_pair_intraday_baseline_is_prior_session():
    now = datetime(2026, 9, 17, 15, 30, tzinfo=ET)
    pair = live_session_pair(now)
    assert pair is not None
    assert pair.current_session == date(2026, 9, 17)
    assert pair.baseline_session == date(2026, 9, 16)


def test_live_session_pair_after_close_baseline_still_prior_session():
    now = datetime(2026, 9, 17, 16, 1, tzinfo=ET)
    pair = live_session_pair(now)
    assert pair is not None
    assert pair.current_session == date(2026, 9, 17)
    assert pair.baseline_session == date(2026, 9, 16)


def test_live_session_pair_after_close_before_eod_ingest_still_prior():
    now = datetime(2026, 9, 17, 16, 30, tzinfo=ET)
    pair = live_session_pair(now)
    assert pair is not None
    assert pair.baseline_session == date(2026, 9, 16)


def test_live_session_pair_after_eod_bar_exists_baseline_still_prior():
    # Presence of today's EOD bar must not flip the live baseline to today.
    now = datetime(2026, 9, 17, 18, 0, tzinfo=ET)
    pair = live_session_pair(now)
    assert pair is not None
    assert pair.current_session == date(2026, 9, 17)
    assert pair.baseline_session == date(2026, 9, 16)
    # Resolver still uses baseline session for prior close even if D EOD exists.
    bars = [
        {"symbol": "SPY", "bar_date": date(2026, 9, 17), "adj_close_price": 500.0, "provider": "IBKR", "source_id": SOURCE_EQUITY_EOD},
        {"symbol": "SPY", "bar_date": date(2026, 9, 16), "adj_close_price": 490.0, "provider": "IBKR", "source_id": SOURCE_EQUITY_EOD},
    ]
    prior = resolve_prior_close(bars, symbol="SPY", session=pair.baseline_session)
    assert prior is not None
    assert prior.session_date == date(2026, 9, 16)
    assert prior.price == pytest.approx(490.0)


def test_live_session_pair_weekend_unavailable():
    assert live_session_pair(datetime(2026, 9, 19, 12, 0, tzinfo=ET)) is None  # Saturday
    assert live_session_pair(datetime(2026, 9, 20, 12, 0, tzinfo=ET)) is None  # Sunday


def test_stale_prior_session_quote_not_usable_as_current():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    current = date(2026, 9, 16)
    stale = {
        "symbol": "XLE",
        "source_id": SOURCE_IBKR,
        "last_price": 90.0,
        "quote_ts": datetime(2026, 9, 15, 15, 59, tzinfo=ET),
    }
    assert is_usable_current_quote(stale, current_session=current, now=now) is False
    fresh = {
        "symbol": "XLE",
        "source_id": SOURCE_IBKR,
        "last_price": 91.0,
        "quote_ts": datetime(2026, 9, 16, 15, 25, tzinfo=ET),
    }
    assert is_usable_current_quote(fresh, current_session=current, now=now) is True


def test_provider_priority_ibkr_over_yahoo():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    current = date(2026, 9, 16)
    candidates = [
        {
            "symbol": "XLE",
            "source_id": SOURCE_YAHOO_LIVE,
            "last_price": 99.0,
            "quote_ts": datetime(2026, 9, 16, 15, 28, tzinfo=ET),
        },
        {
            "symbol": "XLE",
            "source_id": SOURCE_IBKR,
            "last_price": 91.0,
            "quote_ts": datetime(2026, 9, 16, 15, 20, tzinfo=ET),
        },
    ]
    chosen = resolve_current_price(candidates, symbol="XLE", current_session=current, now=now)
    assert chosen is not None
    assert chosen.source_id == SOURCE_IBKR
    assert chosen.provider == "IBKR"
    assert chosen.price == pytest.approx(91.0)


def test_yahoo_fallback_when_ibkr_stale_keeps_yahoo_provenance():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    current = date(2026, 9, 16)
    candidates = [
        {
            "symbol": "XLE",
            "source_id": SOURCE_IBKR,
            "last_price": 90.0,
            "quote_ts": datetime(2026, 9, 15, 15, 59, tzinfo=ET),
        },
        {
            "symbol": "XLE",
            "source_id": SOURCE_YAHOO_LIVE,
            "last_price": 91.5,
            "quote_ts": datetime(2026, 9, 16, 15, 28, tzinfo=ET),
        },
    ]
    chosen = resolve_current_price(candidates, symbol="XLE", current_session=current, now=now)
    assert chosen is not None
    assert chosen.source_id == SOURCE_YAHOO_LIVE
    assert chosen.provider == "YAHOO"


def test_yahoo_retrieved_at_cannot_pass_freshness():
    """Retrieval time must never masquerade as a market observation timestamp."""
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    current = date(2026, 9, 16)
    stale_yahoo = {
        "symbol": "XLE",
        "source_id": SOURCE_YAHOO_LIVE,
        "last_price": 91.0,
        "quote_ts": None,
        "retrieved_at": now,  # fresh retrieval of an undated price
    }
    assert is_usable_current_quote(stale_yahoo, current_session=current, now=now) is False
    assert resolve_current_price([stale_yahoo], symbol="XLE", current_session=current, now=now) is None


def test_ingest_rejects_quote_ts_equal_retrieved_at():
    from market_intelligence.yahoo_live_quotes import ingest_yahoo_live_quotes

    class _Result:
        rowcount = 0

    class _FakeConn:
        def execute(self, *args, **kwargs):
            # ensure_yahoo_live_source + instrument upserts; reject path must not insert quotes.
            sql = str(args[0]) if args else ""
            if "mi_market_quotes" in sql:
                raise AssertionError("must not write rejected Yahoo quote rows")
            return _Result()

    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    stats = ingest_yahoo_live_quotes(
        _FakeConn(),
        [
            {
                "symbol": "XLE",
                "last_price": 91.0,
                "quote_ts": now,
                "retrieved_at": now,
                "source_id": SOURCE_YAHOO_LIVE,
                "provider": "YAHOO",
            }
        ],
    )
    assert stats["inserted"] == 0
    assert stats["rejected"] >= 1


def test_prior_close_prefers_ibkr_provider():
    session = date(2026, 9, 15)
    bars = [
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 88.0, "provider": "YAHOO", "source_id": SOURCE_EQUITY_EOD},
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 87.0, "provider": "IBKR", "source_id": SOURCE_EQUITY_EOD},
    ]
    prior = resolve_prior_close(bars, symbol="XLE", session=session)
    assert prior is not None
    assert prior.provider == "IBKR"
    assert prior.price == pytest.approx(87.0)


def test_yahoo_eod_fills_missing_ibkr_exact_session():
    session = date(2026, 9, 16)
    bars = [
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 88.5, "provider": "YAHOO", "source_id": SOURCE_YAHOO_EOD},
    ]
    prior = resolve_prior_close(bars, symbol="XLE", session=session)
    assert prior is not None
    assert prior.source_id == SOURCE_YAHOO_EOD
    assert prior.provider == "YAHOO"
    assert prior.price == pytest.approx(88.5)


def test_yahoo_eod_d2_does_not_fill_missing_d1():
    baseline = date(2026, 9, 16)
    bars = [
        {"symbol": "XLE", "bar_date": date(2026, 9, 15), "adj_close_price": 87.0, "provider": "YAHOO", "source_id": SOURCE_YAHOO_EOD},
    ]
    assert resolve_prior_close(bars, symbol="XLE", session=baseline) is None


def test_later_ibkr_eod_supersedes_yahoo_eod():
    session = date(2026, 9, 16)
    bars = [
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 88.5, "provider": "YAHOO", "source_id": SOURCE_YAHOO_EOD},
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 87.0, "provider": "IBKR", "source_id": SOURCE_EQUITY_EOD},
    ]
    prior = resolve_prior_close(bars, symbol="XLE", session=session)
    assert prior is not None
    assert prior.source_id == SOURCE_EQUITY_EOD
    assert prior.provider == "IBKR"


def test_equal_dollar_basket_requires_all_members():
    members = ("A", "B", "C")
    ok, used, missing = equal_dollar_live_return({"A": 0.01, "B": 0.02, "C": 0.03}, members)
    assert ok == pytest.approx(0.02)
    assert used == members
    assert missing == ()
    bad, used2, missing2 = equal_dollar_live_return({"A": 0.01, "B": 0.02, "C": None}, members)
    assert bad is None
    assert "C" in missing2
    assert used2 == ("A", "B")


def test_normalize_spy_rsp_to_100():
    series = [(date(2026, 1, 2), 50.0), (date(2026, 1, 3), 55.0)]
    out = normalize_to_100(series)
    assert out[0][1] == pytest.approx(100.0)
    assert out[1][1] == pytest.approx(110.0)


def test_normalize_pair_common_start_when_spy_starts_earlier():
    spy = [
        (date(2026, 1, 2), 400.0),
        (date(2026, 1, 3), 410.0),
        (date(2026, 1, 6), 420.0),
    ]
    rsp = [
        (date(2026, 1, 3), 150.0),
        (date(2026, 1, 6), 165.0),
    ]
    out = normalize_pair_to_100(spy, rsp)
    assert out[0][0] == date(2026, 1, 3)
    assert out[0][1] == pytest.approx(100.0)  # SPY
    assert out[0][2] == pytest.approx(100.0)  # RSP
    assert out[1][1] == pytest.approx(100.0 * 420.0 / 410.0)
    assert out[1][2] == pytest.approx(100.0 * 165.0 / 150.0)


def test_rsp_in_universe_and_primary_exchange():
    assert EQUAL_WEIGHT_SPX == "RSP"
    assert "RSP" in UNIVERSE_SYMBOLS
    assert PRIMARY_EXCHANGE["RSP"] == "ARCA"


def test_eod_task_xml_has_bounded_restart_on_failure():
    from ibkr_collector.service_windows import _eod_task_xml

    xml = _eod_task_xml(Path("python.exe"), Path("C:/repo"), "dipka")
    assert "RestartOnFailure" in xml
    assert "PT12M" in xml
    assert "<Count>3</Count>" in xml
    assert "fetch-eod --client-id 72" in xml
    assert "StartWhenAvailable>true" in xml


def test_preferred_sector_rows_exclude_themes():
    rows = [
        {
            "sector_key": "Technology",
            "canonical_sector": "Technology",
            "entity_kind": "SECTOR",
            "instrument_id": "XLK",
            "as_of": "2026-09-15",
            "source_id": "EQUITY_EOD",
            "metrics": {},
        },
        {
            "sector_key": "THEME:AI",
            "canonical_sector": None,
            "entity_kind": "THEME",
            "instrument_id": "AIQ",
            "as_of": "2026-09-15",
            "source_id": "FMP_LEGACY",
            "metrics": {},
        },
        {
            "sector_key": "Energy",
            "canonical_sector": "Energy",
            "entity_kind": "SECTOR",
            "instrument_id": "XLE",
            "as_of": "2026-09-14",
            "source_id": "FMP_LEGACY",
            "metrics": {},
        },
        {
            "sector_key": "Energy",
            "canonical_sector": "Energy",
            "entity_kind": "SECTOR",
            "instrument_id": "XLE",
            "as_of": "2026-09-15",
            "source_id": "EQUITY_EOD",
            "metrics": {},
        },
    ]
    chosen = preferred_canonical_sector_rows(rows)
    keys = {r["canonical_sector"] for r in chosen}
    assert "THEME:AI" not in keys
    assert keys == {"Technology", "Energy"}
    energy = next(r for r in chosen if r["canonical_sector"] == "Energy")
    assert energy["source_id"] == "EQUITY_EOD"


def test_subgroup_ui_collects_without_dataset_selector():
    industries = {
        "datasets": {
            "INDUSTRY_RS_VS_SECTOR_ETF": {
                "Financials": [
                    {
                        "industry_key": "KRE regional banks comparison",
                        "instrument_id": "KRE_ETF",
                        "as_of": "2026-09-15",
                        "source_id": "EQUITY_EOD",
                        "metrics": {"rs_chg_1w": 0.01},
                        "coverage": {"membership": ["KRE"]},
                    }
                ]
            },
            "THEME_RS": {
                "Financials": [
                    {
                        "industry_key": "Equal-Weight Financial Breadth",
                        "instrument_id": "FIN_BASKET",
                        "as_of": "2026-09-15",
                        "source_id": "EQUITY_EOD",
                        "metrics": {"rs_chg_1w": 0.02},
                        "coverage": {"membership": ["JPM", "BAC"]},
                    }
                ]
            },
        }
    }
    items, unavailable = subgroup_rows_for_parent(industries, "Financials")
    assert unavailable == []
    labels = {r["industry_key"] for r in items}
    assert "KRE regional banks comparison" in labels
    assert "Equal-Weight Financial Breadth" in labels


def test_attach_live_overlays_sector_metrics():
    live = {
        "prior_session": "2026-09-15",
        "by_symbol": {
            "XLE": {
                "live_return": 0.05,
                "prior_close": {"session_date": "2026-09-15"},
                "current": {"provider": "IBKR"},
            },
            "SPY": {
                "live_return": 0.02,
                "prior_close": {"session_date": "2026-09-15"},
                "current": {"provider": "IBKR"},
            },
        },
    }
    rows = [{"sector_key": "Energy", "instrument_id": "XLE", "metrics": {"rs_chg_1w": 0.01}}]
    out = attach_live_1d_to_sector_rows(rows, live)
    assert out[0]["metrics"]["live_ret_1d"] == pytest.approx(0.05)
    assert out[0]["metrics"]["live_rs_chg_1d"] == pytest.approx(1.05 / 1.02 - 1)


def test_format_live_quotes_label_full_partial_none():
    assert format_live_quotes_as_of(None, available=False) == "Live quotes unavailable"
    assert len(REQUIRED_SECTOR_LIVE_SYMBOLS) == 12
    full = format_live_quotes_as_of(
        datetime(2026, 9, 16, 15, 28, 14, tzinfo=ET),
        available=True,
        fresh_count=12,
        required_count=12,
    )
    assert full.startswith("Live quotes as of")
    assert "12/12" in full
    partial = format_live_quotes_as_of(
        datetime(2026, 9, 16, 15, 28, 14, tzinfo=ET),
        available=True,
        fresh_count=8,
        required_count=12,
    )
    assert partial.startswith("Live quotes partial")
    assert "8/12" in partial
    assert "latest" in partial


def test_yahoo_watchdog_skips_when_ibkr_fresh():
    from market_intelligence.yahoo_live_quotes import refresh_yahoo_live_quotes, symbols_needing_yahoo_fallback

    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    rows = [
        {
            "symbol": "XLE",
            "source_id": SOURCE_IBKR,
            "last_price": 91.0,
            "quote_ts": datetime(2026, 9, 16, 15, 25, tzinfo=ET),
        }
    ]
    assert symbols_needing_yahoo_fallback(rows, wanted=["XLE"], now=now) == []
    with patch("market_intelligence.yahoo_live_quotes.fetch_yahoo_last_prices") as fetch:
        result = refresh_yahoo_live_quotes(
            None,
            env={"MI_YAHOO_LIVE_FALLBACK": "1"},
            now=now,
            quote_rows=rows,
            symbols=["XLE"],
        )
        fetch.assert_not_called()
    assert result["status"] == "SKIPPED"
    assert result["outcome"] == "SKIPPED_ALREADY_CURRENT"


def test_yahoo_watchdog_requests_only_stale_ibkr_symbols():
    from market_intelligence.yahoo_live_quotes import symbols_needing_yahoo_fallback

    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    rows = [
        {
            "symbol": "XLE",
            "source_id": SOURCE_IBKR,
            "last_price": 91.0,
            "quote_ts": datetime(2026, 9, 16, 15, 25, tzinfo=ET),
        },
        {
            "symbol": "XLK",
            "source_id": SOURCE_IBKR,
            "last_price": 200.0,
            "quote_ts": datetime(2026, 9, 16, 14, 0, tzinfo=ET),  # stale (>15m)
        },
    ]
    need = symbols_needing_yahoo_fallback(rows, wanted=["XLE", "XLK"], now=now)
    assert need == ["XLK"]


def test_yahoo_watchdog_disabled_flag_no_requests():
    from market_intelligence.due_state import yahoo_live_due
    from market_intelligence.yahoo_live_quotes import refresh_yahoo_live_quotes

    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    decision = yahoo_live_due(enabled=False, now=now)
    assert decision.due is False
    with patch("market_intelligence.yahoo_live_quotes.fetch_yahoo_last_prices") as fetch:
        result = refresh_yahoo_live_quotes(None, env={"MI_YAHOO_LIVE_FALLBACK": "0"}, now=now, symbols=["XLE"])
        fetch.assert_not_called()
    assert result["status"] == "SKIPPED"


def test_yahoo_watchdog_outside_rth_and_weekend_no_poll():
    from market_intelligence.due_state import yahoo_live_due
    from market_intelligence.yahoo_live_quotes import refresh_yahoo_live_quotes

    after_close = datetime(2026, 9, 16, 16, 30, tzinfo=ET)
    assert in_regular_trading_hours(after_close) is False
    assert yahoo_live_due(enabled=True, now=after_close).due is False
    weekend = datetime(2026, 9, 19, 12, 0, tzinfo=ET)
    assert yahoo_live_due(enabled=True, now=weekend).due is False
    with patch("market_intelligence.yahoo_live_quotes.fetch_yahoo_last_prices") as fetch:
        result = refresh_yahoo_live_quotes(
            None,
            env={"MI_YAHOO_LIVE_FALLBACK": "1"},
            now=after_close,
            symbols=["XLE"],
            quote_rows=[],
        )
        fetch.assert_not_called()
    assert result["reason"] == "outside_rth"


def test_streamlit_pages_do_not_import_providers():
    banned = ("yfinance", "ib_insync", "EClient", "fetch_yahoo", "YahooAdapter", "connect_historical")
    for path in [
        ROOT / "market_intelligence" / "pages_ui.py",
        ROOT / "market_intelligence" / "ui.py",
        ROOT / "market_intelligence" / "read_models.py",
        ROOT / "market_intelligence" / "equity_live.py",
        ROOT / "market_intelligence" / "live_session.py",
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                rendered = ast.dump(node)
                for token in banned:
                    assert token not in rendered, "{0} imports {1}".format(path.name, token)
        for token in ("import yfinance", "from yfinance"):
            assert token not in text, "{0} must not import yfinance".format(path.name)


def test_ui_has_no_dataset_selector_and_uses_live_labels():
    text = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert "Industry dataset" not in text
    assert "INDUSTRY_RS_VS_SECTOR_ETF" not in text or "subgroup_rows_for_parent" in text
    assert "Live 1D RS" in text
    assert "Live 1D Return" in text
    assert "Industry & Subgroup Leadership" in text
    assert "SPY vs RSP" in text
    assert 'selectbox("Industry dataset"' not in text


def test_default_watchlist_includes_rsp_and_sector_etfs():
    from ibkr_collector.config import DEFAULT_WATCHLIST

    symbols = {row["symbol"] for row in DEFAULT_WATCHLIST}
    assert "RSP" in symbols
    assert "XLE" in symbols and "XLK" in symbols and "SPY" in symbols


def test_heatmap_missing_is_em_dash_not_none_string():
    ui_text = (ROOT / "market_intelligence" / "ui.py").read_text(encoding="utf-8")
    pages = (ROOT / "market_intelligence" / "pages_ui.py").read_text(encoding="utf-8")
    assert 'return "—"' in ui_text
    # Subgroup main table no longer dumps Coverage = None into the grid.
    assert '"Coverage": (row.get("coverage")' not in pages
    assert "Industry & Subgroup Leadership" in pages
