"""Strategy Monitor library and investment-question layout (no QuantConnect)."""

from __future__ import annotations

from pathlib import Path

from qc_research.research_library import filter_library
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
MONITOR = (ROOT / "pages" / "strategy_monitor.py").read_text(encoding="utf-8")
PLATFORM = (ROOT / "qc_research" / "ml_monitor_ui.py").read_text(encoding="utf-8")


def test_monitor_has_library_filters_and_stable_selection():
    assert "Research library" in MONITOR
    assert 'key="strategy_monitor_selected_strategy"' in MONITOR
    assert 'key="strategy_monitor_asset_class"' in MONITOR
    assert 'key="strategy_monitor_research_status"' in MONITOR
    assert 'key="strategy_monitor_include_smoke"' in MONITOR
    assert "latest completed eligible non-holdout" in MONITOR
    assert "highest-performing" in MONITOR
    assert "strategy_monitor_last_strategy" in MONITOR
    assert "This page does not launch backtests" in MONITOR
    assert "arrow_safe_frame" in MONITOR
    assert "UNAVAILABLE" in MONITOR


def test_monitor_does_not_open_or_launch_holdout():
    assert "authorize final holdout" not in MONITOR.lower()
    assert "Open holdout" not in MONITOR
    assert "launch holdout" not in MONITOR.lower()
    assert "create_backtest" not in MONITOR


def test_platform_view_uses_investment_tabs_and_readout():
    assert '["Summary", "Performance", "Robustness / OOS", "Research Details"]' in PLATFORM
    assert "build_readout" in PLATFORM
    assert "comparison_table" in PLATFORM
    assert "Completed research is not labeled approved" in PLATFORM
    assert "percentage points, not alpha" in PLATFORM


def test_filter_library_keeps_failed_and_hides_smoke_by_default():
    frame = pd.DataFrame(
        [
            {"strategy_id": "A", "asset_class": "Equity", "run_status": "COMPLETE", "failed": False, "is_smoke": False},
            {"strategy_id": "B", "asset_class": "Equity", "run_status": "FAILED", "failed": True, "is_smoke": False},
            {"strategy_id": "C", "asset_class": "Bond ETF", "run_status": "COMPLETE", "failed": False, "is_smoke": True},
        ]
    )
    default = filter_library(frame)
    assert set(default["strategy_id"]) == {"A", "B"}
    failed = filter_library(frame, research_status="Failed")
    assert list(failed["strategy_id"]) == ["B"]
    smoke = filter_library(frame, include_smoke=True)
    assert set(smoke["strategy_id"]) == {"A", "B", "C"}
