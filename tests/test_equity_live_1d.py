"""Regression coverage for live 1D return/RS, Yahoo fallback provenance, UI boundaries."""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from market_intelligence.equity_live import (
    attach_live_1d_to_sector_rows,
    preferred_canonical_sector_rows,
    subgroup_rows_for_parent,
)
from market_intelligence.live_session import (
    SOURCE_IBKR,
    SOURCE_YAHOO_LIVE,
    equal_dollar_live_return,
    format_live_quotes_as_of,
    is_usable_current_quote,
    live_relative_strength,
    live_return,
    live_rs_from_prices,
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


def test_stale_prior_session_quote_not_usable_as_current():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    prior_session = date(2026, 9, 15)
    stale = {
        "symbol": "XLE",
        "source_id": SOURCE_IBKR,
        "last_price": 90.0,
        "quote_ts": datetime(2026, 9, 15, 15, 59, tzinfo=ET),
    }
    assert is_usable_current_quote(stale, expected_session=prior_session, now=now) is False
    fresh = {
        "symbol": "XLE",
        "source_id": SOURCE_IBKR,
        "last_price": 91.0,
        "quote_ts": datetime(2026, 9, 16, 15, 25, tzinfo=ET),
    }
    assert is_usable_current_quote(fresh, expected_session=prior_session, now=now) is True


def test_provider_priority_ibkr_over_yahoo():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    session = date(2026, 9, 15)
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
    chosen = resolve_current_price(candidates, symbol="XLE", expected_session=session, now=now)
    assert chosen is not None
    assert chosen.source_id == SOURCE_IBKR
    assert chosen.provider == "IBKR"
    assert chosen.price == pytest.approx(91.0)


def test_yahoo_fallback_when_ibkr_stale_keeps_yahoo_provenance():
    now = datetime(2026, 9, 16, 15, 30, tzinfo=ET)
    session = date(2026, 9, 15)
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
    chosen = resolve_current_price(candidates, symbol="XLE", expected_session=session, now=now)
    assert chosen is not None
    assert chosen.source_id == SOURCE_YAHOO_LIVE
    assert chosen.provider == "YAHOO"


def test_prior_close_prefers_ibkr_provider():
    session = date(2026, 9, 15)
    bars = [
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 88.0, "provider": "YAHOO", "source_id": "EQUITY_EOD"},
        {"symbol": "XLE", "bar_date": session, "adj_close_price": 87.0, "provider": "IBKR", "source_id": "EQUITY_EOD"},
    ]
    prior = resolve_prior_close(bars, symbol="XLE", session=session)
    assert prior is not None
    assert prior.provider == "IBKR"
    assert prior.price == pytest.approx(87.0)


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


def test_format_live_quotes_label():
    assert format_live_quotes_as_of(None, available=False) == "Live quotes unavailable"
    label = format_live_quotes_as_of(datetime(2026, 9, 16, 15, 27, 14, tzinfo=ET), available=True)
    assert label.startswith("Live quotes as of")
    assert label.endswith("ET")


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

