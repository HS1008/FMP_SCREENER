"""Internal metric mapping for Strategy Monitor. Documentation used by tests.

Families do not share the same metrics. Missing metrics stay missing.
"""

from __future__ import annotations

from typing import Any

METRIC_MAP: dict[str, dict[str, Any]] = {
    "annualized_return": {
        "aliases": ("cagr",),
        "units": "decimal fraction",
        "sources": {
            "qc_native": "backtests.cagr",
            "reconstructed": "oos_aggregate.ml.cagr from monthly/daily net returns",
            "per_window": "oos_aggregate.windows[].ml.cagr",
            "mean_across_windows": "average of per-window CAGRs — not whole-period CAGR",
            "whole_period": "stitched return/equity series only when metric_kind=stitched_full_period",
        },
        "limitations": "Do not present the average of window CAGRs as whole-period CAGR.",
    },
    "max_drawdown": {
        "aliases": ("max_drawdown",),
        "units": "decimal fraction (typically negative)",
        "sources": {
            "qc_native": "backtests.max_drawdown",
            "reconstructed": "min(wealth/peak-1) on stored return series",
            "per_window": "windows[].ml.max_drawdown",
            "mean_across_windows": "average of window drawdowns — not whole-period max DD",
            "whole_period": "stitched equity only",
        },
        "limitations": "Monthly-sampled reconstruction understates intra-month drawdown.",
    },
    "sharpe_ratio": {
        "aliases": ("sharpe", "sharpe_ratio"),
        "units": "ratio",
        "sources": {
            "qc_native": "backtests.sharpe_ratio",
            "reconstructed": "mean(r)/stdev(r)*sqrt(periods_per_year)",
            "per_window": "windows[].ml.sharpe_ratio",
            "mean_across_windows": "average of window Sharpes — not whole-period Sharpe",
            "whole_period": "stitched returns only",
        },
        "limitations": "Do not average Sharpe ratios and label the result whole-period Sharpe.",
    },
    "benchmark_return": {
        "aliases": ("baseline_cagr",),
        "units": "decimal fraction",
        "sources": {
            "qc_native": "implicit QuantConnect benchmark (alpha/beta only; not a stored SPY series)",
            "reconstructed": "oos_aggregate.baseline.cagr",
            "per_window": "windows[].baseline.cagr",
            "mean_across_windows": "average of baseline window CAGRs",
            "whole_period": "stitched baseline series when present",
        },
        "limitations": "Platform research compares to a frozen baseline, not necessarily SPY.",
    },
    "excess_return": {
        "aliases": ("cagr_diff", "ml_minus_baseline.cagr"),
        "units": "percentage points when both sides are percent returns",
        "sources": {
            "qc_native": "not labeled alpha; backtests.alpha is a QC ratio vs its implicit benchmark",
            "reconstructed": "strategy CAGR minus baseline CAGR",
            "per_window": "windows[].ml_minus_baseline.cagr",
            "mean_across_windows": "mean of per-window differences",
            "whole_period": "difference of whole-period CAGRs only",
        },
        "limitations": "CAGR difference is not alpha. No significance threshold is invented.",
    },
    "exposure_turnover": {
        "aliases": ("annual_turnover", "trade_count"),
        "units": "fraction or count as stored",
        "sources": {
            "qc_native": "backtests.trade_count / Stage 2 annual_turnover",
            "reconstructed": "optional",
            "per_window": "windows[].ml.trade_count",
            "mean_across_windows": "average trade count",
            "whole_period": "unavailable unless a full trade blotter exists",
        },
        "limitations": "Zero trades is a real cash window; missing trades are not zero.",
    },
}


def aggregation_label(metric_kind: str | None) -> str:
    kind = str(metric_kind or "mean_across_windows")
    if kind == "stitched_full_period":
        return "Whole-period (stitched canonical returns)"
    if kind == "qc_native":
        return "Whole-period (native QuantConnect)"
    return "Mean across OOS windows"
