"""Deterministic Strategy Monitor readout from stored research results.

No LLM. Every statement traces to canonical fields. Missing data is stated as
missing. Forbidden promotional language is never emitted.
"""

from __future__ import annotations

from typing import Any, Mapping

from qc_research.platform_presentation import UNAVAILABLE, friendly_label

FORBIDDEN_PHRASES = (
    "ready to trade",
    "proven edge",
    "low risk",
    "passed validation",
    "economically acceptable",
    "approved for live",
)


def _num(value: Any) -> float | None:
    if value is None or value == UNAVAILABLE:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _windows(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = view.get("oos_windows")
    if raw == UNAVAILABLE or raw is None:
        return []
    return [row for row in raw if isinstance(row, dict)]


def _ml(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("ml")
    return value if isinstance(value, dict) else {}


def _baseline(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("baseline")
    return value if isinstance(value, dict) else {}


def _window_label(row: Mapping[str, Any]) -> str:
    return str(row.get("window_id") or row.get("oos_end") or row.get("end") or "an unlabeled window")


def _metric_kind_label(kind: Any) -> str:
    text = str(kind or "mean_across_windows")
    if text == "stitched_full_period":
        return "whole-period statistics from a stitched canonical return series"
    if text == "qc_native":
        return "native QuantConnect whole-period statistics"
    return "mean across OOS windows — not a whole-period CAGR or Sharpe"


def plain_status_line(
    *,
    research_status: Any = None,
    economic_gate: Any = None,
    promotion_gate: Any = None,
    holdout_status: Any = None,
    delivery_status: Any = None,
    run_complete: bool | None = None,
    label_integrity: Any = None,
) -> str:
    """Separate execution, economic acceptance, and human review."""
    status = str(research_status or "").upper()
    economic = str(economic_gate or "").upper()
    promotion = str(promotion_gate or "").upper()
    holdout = str(holdout_status or "").upper()
    parts: list[str] = []
    if status in {"COMPLETE", "RESEARCH_COMPLETE", "NON_HOLDOUT_COMPLETE"}:
        parts.append("Research complete")
    elif status in {"FAILED", "ERROR"}:
        parts.append("Research failed")
    elif status in {"INCOMPLETE", "PARTIAL"}:
        parts.append("Research incomplete")
    elif status in {"RUNNING", "IN_PROGRESS"}:
        parts.append("Research running")
    elif status:
        parts.append(friendly_label(status))
    else:
        parts.append("Research status unavailable")

    if economic in {"", "NOT_DEFINED"}:
        parts.append("Economic criteria not defined")
    elif economic == "PASS":
        parts.append("Economic gate PASS")
    elif economic == "FAIL":
        parts.append("Economic gate FAIL")
    elif economic:
        parts.append("Economic gate {0}".format(economic))

    if promotion in {"HUMAN_REVIEW_REQUIRED", ""}:
        parts.append("Human review pending")
    elif promotion == "PROMOTION_APPROVED":
        parts.append("Human promotion approved")
    elif promotion:
        parts.append(friendly_label(promotion))

    if holdout == "LOCKED":
        parts.append("Holdout locked")
    elif holdout == "ACCESSED":
        parts.append("Holdout accessed")
    integrity = str(label_integrity or "").strip()
    if integrity:
        parts.append("Label integrity {0}".format(integrity))
    if run_complete is False:
        parts.append("Latest run is not complete")
    return " · ".join(parts)


def build_readout(view: Mapping[str, Any] | None) -> list[str]:
    """Short factual statements. Empty list if nothing can be said."""
    view = dict(view or {})
    statements: list[str] = []
    windows = _windows(view)
    comparable = []
    positive = []
    cash = []
    missing_baseline = []
    drawdowns: list[tuple[float, str]] = []
    for row in windows:
        ml = _ml(row)
        base = _baseline(row)
        sharpe = _num(ml.get("sharpe_ratio"))
        ret = _num(ml.get("cagr") if ml.get("cagr") is not None else ml.get("net_profit"))
        trades = _num(ml.get("trade_count"))
        dd = _num(ml.get("max_drawdown"))
        label = _window_label(row)
        if sharpe is not None and _num(base.get("sharpe_ratio")) is not None:
            comparable.append((sharpe, float(base["sharpe_ratio"]), label))
        elif sharpe is not None:
            missing_baseline.append(label)
        if ret is not None and ret > 0:
            positive.append(label)
        elif ret is None and sharpe is not None and sharpe > 0:
            positive.append(label)
        if trades is not None and trades == 0:
            cash.append(label)
        if dd is not None:
            drawdowns.append((dd, label))

    if windows:
        beat = sum(1 for sharpe, base, _ in comparable if sharpe > base)
        if comparable:
            statements.append(
                "The strategy outperformed its baseline in {0} of {1} comparable OOS windows "
                "(aligned window-by-window; missing baselines are not counted as zero).".format(
                    beat, len(comparable)
                )
            )
        else:
            statements.append(
                "{0} completed OOS window(s) are stored; no window has both strategy and baseline Sharpe, "
                "so outperformance is not computed.".format(len(windows))
            )
        if positive:
            statements.append(
                "Positive-return (or positive-Sharpe when return is unavailable) windows: {0} of {1}.".format(
                    len(positive), len(windows)
                )
            )
        if cash:
            statements.append(
                "The strategy held cash / recorded zero trades throughout: {0}.".format(", ".join(cash))
            )
        if missing_baseline:
            statements.append(
                "Baseline metrics are missing for {0} window(s); those windows are identified, not zero-filled.".format(
                    len(missing_baseline)
                )
            )
        if drawdowns:
            worst = min(drawdowns, key=lambda item: item[0])
            statements.append(
                "The largest stored window drawdown was {0:.2%} in {1}.".format(worst[0], worst[1])
                if abs(worst[0]) <= 5
                else "The largest stored window drawdown was {0} in {1}.".format(worst[0], worst[1])
            )

    kind = view.get("metric_kind") or "mean_across_windows"
    statements.append("Headline performance uses {0}.".format(_metric_kind_label(kind)))

    cost = view.get("cost_model")
    if cost and cost != UNAVAILABLE:
        statements.append("Results include the documented cost model ({0}); undocumented stress-cost results are not assumed.".format(cost))
    else:
        statements.append("A documented cost model is unavailable on this record.")

    status = str(view.get("research_status") or view.get("research_state") or "")
    economic = str(view.get("economic_gate") or "")
    if status.upper() in {"COMPLETE", "RESEARCH_COMPLETE"}:
        if economic.upper() in {"", "NOT_DEFINED", UNAVAILABLE.upper()}:
            statements.append("Research is complete; economic acceptance criteria remain undefined.")
        else:
            statements.append("Research is complete; economic gate is {0}.".format(economic))
    elif status:
        statements.append("Research status is {0}; this is not an economic PASS/FAIL.".format(status))

    holdout = str(view.get("holdout_status") or "")
    if holdout.upper() == "LOCKED":
        statements.append("The protected holdout remains sealed and is not opened from this page.")

    cleaned = []
    for line in statements:
        lower = line.lower()
        if any(phrase in lower for phrase in FORBIDDEN_PHRASES):
            continue
        cleaned.append(line)
    return cleaned


def comparison_table(
    *,
    strategy_metrics: Mapping[str, Any] | None = None,
    baseline_metrics: Mapping[str, Any] | None = None,
    deltas: Mapping[str, Any] | None = None,
    metric_kind: str | None = None,
    baseline_label: str = "Baseline",
) -> list[dict[str, Any]]:
    """Strategy / baseline / difference rows. Difference only where meaningful.

    CAGR differences are labeled in percentage points, never as alpha.
    Missing values stay missing; they are not replaced with zero.
    """
    strategy_metrics = dict(strategy_metrics or {})
    baseline_metrics = dict(baseline_metrics or {})
    deltas = dict(deltas or {})
    specs = (
        ("cagr", "Annualized return", "fraction", "pp", False),
        ("max_drawdown", "Maximum drawdown", "fraction", "pp", False),
        ("sharpe_ratio", "Sharpe ratio", "ratio", "ratio", False),
        ("sortino_ratio", "Sortino ratio", "ratio", "ratio", False),
        ("net_profit", "Net return", "fraction", "pp", False),
        ("trade_count", "Trades", "count", "count", False),
        ("cost_drag", "Cost drag", "unknown", None, True),
        ("annual_turnover", "Turnover", "fraction", None, True),
    )
    rows = []
    for key, label, units, diff_kind, cost_like in specs:
        left = strategy_metrics.get(key)
        right = baseline_metrics.get(key)
        if left is None and right is None:
            continue
        delta = deltas.get(key)
        if delta is None and left is not None and right is not None and diff_kind:
            try:
                delta = float(left) - float(right)
            except (TypeError, ValueError):
                delta = None
        if key == "cagr" and delta is not None:
            # Percentage-point difference between annualized returns. Not alpha.
            diff_label = "{0:+.2f} pp".format(float(delta) * 100.0) if abs(float(delta)) <= 5 else "{0:+.4f} (pp)".format(float(delta))
        elif key == "max_drawdown" and delta is not None:
            diff_label = "{0:+.2f} pp".format(float(delta) * 100.0) if abs(float(delta)) <= 5 else str(delta)
        elif diff_kind == "ratio" and delta is not None:
            diff_label = "{0:+.2f}".format(float(delta))
        elif diff_kind == "count" and delta is not None:
            diff_label = "{0:+.0f}".format(float(delta))
        else:
            diff_label = None
        rows.append(
            {
                "Metric": label + (" (mean across OOS windows)" if metric_kind == "mean_across_windows" else ""),
                "Strategy": left,
                baseline_label: right,
                "Difference": diff_label,
                "Units": units,
                "Difference meaning": (
                    "percentage points; not alpha"
                    if key == "cagr" and diff_label
                    else ("same units" if diff_label else "not computed")
                ),
                "Cost note": "Explicit cost/fee field; not described as percent return drag unless the source unit is a return." if cost_like else None,
            }
        )
    return rows
