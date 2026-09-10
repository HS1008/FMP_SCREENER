"""Deterministic readout, comparison table, and status language."""

from __future__ import annotations

from qc_research.metric_map import METRIC_MAP, aggregation_label
from qc_research.research_library import default_run_id
from qc_research.research_readout import (
    FORBIDDEN_PHRASES,
    build_readout,
    comparison_table,
    plain_status_line,
)
import pandas as pd


def test_plain_status_does_not_treat_review_or_undefined_as_fail():
    line = plain_status_line(
        research_status="COMPLETE",
        economic_gate="NOT_DEFINED",
        promotion_gate="HUMAN_REVIEW_REQUIRED",
        holdout_status="LOCKED",
    )
    assert line == "Research complete · Economic criteria not defined · Human review pending · Holdout locked"
    assert "FAIL" not in line
    assert "approved" not in line.lower()
    assert "passed validation" not in line.lower()
    bounded = plain_status_line(
        research_status="COMPLETE",
        economic_gate="NOT_DEFINED",
        promotion_gate="HUMAN_REVIEW_REQUIRED",
        holdout_status="LOCKED",
        label_integrity="CANNOT_RULE_OUT",
    )
    assert bounded.endswith("Label integrity CANNOT_RULE_OUT")
    assert "Economic criteria not defined" in bounded
    assert "FAIL" not in bounded


def test_readout_counts_comparable_windows_and_does_not_zero_fill_missing():
    view = {
        "metric_kind": "mean_across_windows",
        "research_status": "COMPLETE",
        "economic_gate": "NOT_DEFINED",
        "holdout_status": "LOCKED",
        "cost_model": "base_5bps",
        "oos_windows": [
            {"window_id": "2019", "ml": {"sharpe_ratio": 0.4, "cagr": 0.05, "max_drawdown": -0.1, "trade_count": 8}, "baseline": {"sharpe_ratio": 0.2}},
            {"window_id": "2020", "ml": {"sharpe_ratio": 0.1, "cagr": -0.02, "max_drawdown": -0.3, "trade_count": 4}, "baseline": {"sharpe_ratio": 0.15}},
            {"window_id": "2021", "ml": {"sharpe_ratio": 0.2, "trade_count": 0}, "baseline": {}},
        ],
    }
    lines = build_readout(view)
    joined = " ".join(lines)
    assert "outperformed its baseline in 1 of 2 comparable OOS windows" in joined
    assert "zero trades" in joined.lower() or "held cash" in joined.lower()
    assert "2021" in joined
    assert any("mean across" in line.lower() and "window" in line.lower() for line in lines)
    assert "economic acceptance criteria remain undefined" in joined.lower()
    assert "holdout remains sealed" in joined.lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in joined.lower()


def test_comparison_table_uses_percentage_points_and_keeps_missing():
    rows = comparison_table(
        strategy_metrics={"cagr": 0.10, "sharpe_ratio": 0.8, "max_drawdown": -0.2},
        baseline_metrics={"cagr": 0.06, "sharpe_ratio": 0.5},
        metric_kind="mean_across_windows",
    )
    by_metric = {row["Metric"]: row for row in rows}
    cagr = next(row for key, row in by_metric.items() if key.startswith("Annualized return"))
    assert cagr["Strategy"] == 0.10
    assert cagr["Baseline"] == 0.06
    assert "pp" in str(cagr["Difference"])
    assert "not alpha" in cagr["Difference meaning"]
    sharpe = next(row for key, row in by_metric.items() if key.startswith("Sharpe"))
    assert sharpe["Difference"] == "+0.30"
    drawdown = next(row for key, row in by_metric.items() if "drawdown" in key.lower())
    assert drawdown["Baseline"] is None
    assert drawdown["Difference"] is None


def test_missing_metrics_are_not_replaced_with_zero():
    rows = comparison_table(strategy_metrics={"cagr": 0.0}, baseline_metrics={"cagr": None})
    assert rows[0]["Strategy"] == 0.0
    assert rows[0]["Baseline"] is None
    assert rows[0]["Difference"] is None
    lines = build_readout({"oos_windows": [{"window_id": "2018", "ml": {}, "baseline": {}}]})
    joined = " ".join(lines)
    assert "0 of 0" not in joined
    assert "not computed" in joined or "unavailable" in joined.lower() or "no window has both" in joined.lower()


def test_window_average_is_not_labeled_whole_period():
    assert aggregation_label("mean_across_windows") == "Mean across OOS windows"
    assert "Whole-period" in aggregation_label("stitched_full_period")
    assert "average of window CAGRs" in METRIC_MAP["annualized_return"]["limitations"]
    assert "whole-period sharpe" in METRIC_MAP["sharpe_ratio"]["limitations"].lower()


def test_default_run_is_latest_completed_not_highest_performing():
    runs = pd.DataFrame(
        [
            {"research_run_id": "old_complete", "run_status": "COMPLETE", "holdout_status": "LOCKED", "last_seen_at": "2024-01-01", "research_kind": "platform_research"},
            {"research_run_id": "new_complete", "run_status": "COMPLETE", "holdout_status": "LOCKED", "last_seen_at": "2026-01-01", "research_kind": "platform_research"},
            {"research_run_id": "holdout", "run_status": "COMPLETE", "holdout_status": "ACCESSED", "last_seen_at": "2026-06-01", "research_kind": "platform_research"},
            {"research_run_id": "failed_new", "run_status": "FAILED", "holdout_status": "LOCKED", "last_seen_at": "2026-08-01", "research_kind": "platform_research"},
        ]
    )
    assert default_run_id(runs) == "new_complete"
