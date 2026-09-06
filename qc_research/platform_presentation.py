"""Investor-facing Platform Research labels and definition. No QuantConnect."""

from __future__ import annotations

import re
from typing import Any, Mapping

UNAVAILABLE = "Unavailable / Not applicable"
HELP = {
    "Sharpe": "Annualized return per unit of volatility. Higher is better; this page does not apply a PASS/FAIL cut.",
    "Sortino": "Like Sharpe, but penalizes only downside volatility.",
    "CAGR": "Compound annual growth rate over the shown sample.",
    "Max Drawdown": "Largest peak-to-trough decline in the shown sample.",
    "OOS": "Out-of-sample: evaluated after the model/spec was frozen for that window.",
    "WFO": "Walk-forward optimization: rolling train, then the next OOS year.",
    "economic_gate": "No prospective numeric PASS/FAIL threshold has been defined.",
    "promotion_gate": "Research is complete. Human approval is required only before promotion.",
    "holdout": "2025+ remains sealed until a human authorizes final holdout.",
    "mean_across_windows": "Average of the 10 yearly OOS windows. Not a stitched 2015–2024 equity curve.",
}

FAMILY_LABELS = {
    "FIXED_INCOME_TREND": "Fixed Income Trend",
    "TIME_SERIES_TREND": "Time-Series Trend",
    "BREAKOUT": "Breakout",
    "TREASURY_FUTURES_TREND": "Treasury Futures Trend",
    "PAIRS_STAT_ARB": "Pairs",
    "CROSS_SECTIONAL_PIT": "Cross-Sectional",
    "CROSS_SECTIONAL_FACTOR": "Cross-Sectional Factor",
}
ASSET_LABELS = {
    "BOND_ETF": "Bond ETF",
    "ETF": "ETF",
    "US_EQUITY": "Equity",
    "TREASURY_FUTURE": "Treasury Futures",
    "FUTURE": "Futures",
    "PAIR": "Pairs",
}
MODE_LABELS = {
    "ML_DISCOVERY": "ML Discovery",
    "MANUAL": "Manual",
    "AUTO": "Auto",
}
MODEL_LABELS = {
    "elasticnet": "ElasticNet",
    "ridge": "Ridge",
    "deterministic": "Deterministic",
    "sma": "SMA",
}
PROVENANCE_LABELS = {
    "REAL_QC": "Real QuantConnect",
    "LOCAL_LICENSED": "Local Licensed",
    "LOCAL_TEST": "Local Test",
}
STATUS_LABELS = {
    "COMPLETE": "Research Complete",
    "PLANNED": "Planned",
    "RUNNING": "Running",
    "INCOMPLETE": "Incomplete",
    "FAILED": "Failed",
    "HUMAN_REVIEW_REQUIRED": "Review Required for Promotion",
    "PROMOTION_APPROVED": "Promotion Approved",
    "NOT_DEFINED": "Not Defined",
    "LOCKED": "Holdout Locked",
    "AUTHORIZED": "Holdout Authorized",
    "ACCESSED": "Holdout Accessed",
    "DELIVERED": "Delivered",
    "PENDING": "Delivery Pending",
    "BLOCKED": "Delivery Blocked",
}


def friendly_label(value: Any, table: Mapping[str, str] | None = None) -> str:
    raw = str(value or "").strip()
    if not raw or raw == UNAVAILABLE:
        return UNAVAILABLE
    table = table or {}
    if raw in table:
        return table[raw]
    if raw in STATUS_LABELS:
        return STATUS_LABELS[raw]
    if raw in MODE_LABELS:
        return MODE_LABELS[raw]
    if raw in MODEL_LABELS:
        return MODEL_LABELS[raw]
    if raw.lower() in MODEL_LABELS:
        return MODEL_LABELS[raw.lower()]
    if raw in FAMILY_LABELS:
        return FAMILY_LABELS[raw]
    if raw in ASSET_LABELS:
        return ASSET_LABELS[raw]
    if raw in PROVENANCE_LABELS:
        return PROVENANCE_LABELS[raw]
    return raw.replace("_", " ").title()


def display_strategy_name(strategy_id: str, display_name: str | None = None) -> str:
    if display_name:
        return str(display_name)
    text = str(strategy_id or "")
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", text)
    return spaced.replace("_", " ").strip() or text


def short_id(value: Any, *, keep: int = 8) -> str:
    text = str(value or "")
    if not text or text == UNAVAILABLE:
        return UNAVAILABLE
    if len(text) <= keep + 3:
        return text
    return text[:keep] + "…"


def parse_trial_id(trial_id: str | None) -> dict[str, Any]:
    raw = str(trial_id or "")
    if not raw:
        return {}
    family, _, rest = raw.partition("::")
    parsed: dict[str, Any] = {"trial_id": raw, "model_family": family or None}
    lookback = re.search(r"lb(\d+)", rest)
    if lookback:
        parsed["lookback"] = int(lookback.group(1))
    alpha = re.search(r"a(\d+)p(\d+)", rest)
    if alpha:
        parsed["alpha"] = float("{0}.{1}".format(alpha.group(1), alpha.group(2)))
    else:
        alpha_int = re.search(r"a(\d+)\b", rest)
        if alpha_int:
            parsed["alpha"] = float(alpha_int.group(1))
    l1 = re.search(r"l1(\d+)p(\d+)", rest)
    if l1:
        parsed["l1_ratio"] = float("{0}.{1}".format(l1.group(1), l1.group(2)))
    sma = re.search(r"sma(\d+)", rest)
    if sma:
        parsed["lookback"] = int(sma.group(1))
        parsed["label"] = "{0}-day SMA long/cash".format(sma.group(1))
    return parsed


def strategy_definition_from_payloads(
    *,
    summary: Mapping[str, Any] | None = None,
    spec: Mapping[str, Any] | None = None,
    trials: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    summary = dict(summary or {})
    inner_spec = dict(spec or {})
    if isinstance(inner_spec.get("payload"), dict):
        inner_spec = inner_spec["payload"]
    identity = dict(inner_spec.get("identity") or {})
    data = dict(inner_spec.get("data") or {})
    signal = dict(inner_spec.get("signal") or {})
    features = dict(inner_spec.get("features") or {})
    target = dict(inner_spec.get("target") or {})
    model = dict(inner_spec.get("model") or {})
    portfolio = dict(inner_spec.get("portfolio") or {})
    costs = dict(inner_spec.get("costs") or {})
    validation = dict(inner_spec.get("validation") or {})
    wfo = dict(validation.get("outer_wfo") or {})
    inner = dict(validation.get("inner_cv") or {})
    declared = dict(summary.get("strategy_definition") or {})
    symbol = (
        declared.get("instrument")
        or summary.get("symbol")
        or data.get("primary_symbol")
        or (list(data.get("symbols") or [None])[0])
    )
    selected = declared.get("winner") or parse_trial_id(
        summary.get("selected_candidate") or summary.get("selected_trial_id")
    )
    if isinstance(selected, str):
        selected = parse_trial_id(selected)
    baseline = declared.get("baseline") or parse_trial_id(summary.get("baseline_trial_id"))
    if isinstance(baseline, str):
        baseline = parse_trial_id(baseline)
    feature_list = declared.get("features") or list(features.get("ordered_feature_schema") or [])
    lookback = declared.get("lookback") or signal.get("lookback") or selected.get("lookback")
    mode = summary.get("research_mode") or identity.get("research_mode")
    if mode == "MANUAL":
        behavior = declared.get("portfolio_behavior") or (
            "Trade the specified rule on {0}. Hold cash when the rule is off.".format(symbol or "the instrument")
        )
    else:
        behavior = declared.get("portfolio_behavior") or (
            "Hold {0} when the frozen model's signal is positive. Otherwise hold cash.".format(symbol or "the instrument")
        )
    horizon = declared.get("target")
    if not horizon and target.get("label_horizon"):
        horizon = "{0}-session forward return".format(target.get("label_horizon"))
    return {
        "display_name": declared.get("display_name") or display_strategy_name(
            str(summary.get("strategy_id") or identity.get("strategy_id") or ""),
            summary.get("display_name"),
        ),
        "thesis": declared.get("thesis") or summary.get("thesis") or inner_spec.get("intent", {}).get("original_user_thesis"),
        "instrument": symbol,
        "universe": declared.get("universe") or data.get("symbols") or ([symbol] if symbol else []),
        "portfolio_behavior": behavior,
        "features": feature_list,
        "lookback": lookback,
        "target": horizon,
        "models_searched": declared.get("models_searched")
        or list(model.get("approved_model_families") or [])
        or ((trials or {}).get("payload") or trials or {}).get("candidates"),
        "winner": selected,
        "baseline": baseline,
        "validation": declared.get("validation")
        or {
            "train_years": wfo.get("train_years"),
            "oos_years": wfo.get("oos_years"),
            "first_oos_year": wfo.get("first_oos_year"),
            "last_oos_year": wfo.get("last_oos_year"),
            "inner_folds": inner.get("n_folds"),
            "embargo_sessions": inner.get("embargo_trading_days"),
            "purge": validation.get("purge"),
        },
        "execution": declared.get("execution")
        or {
            "signal_timing": summary.get("signal_timing") or "decision_close_next_session",
            "fill_assumptions": summary.get("fill_assumptions") or "next_open_or_moc",
        },
        "cost_model_id": declared.get("cost_model_id") or costs.get("cost_model_id") or summary.get("cost_model_id"),
        "metric_kind": declared.get("metric_kind") or "mean_across_windows",
        "signal_rule": signal.get("signal_definition"),
        "entry": signal.get("entry") or signal.get("entry_z"),
        "exit": signal.get("exit") or signal.get("exit_z"),
        "sizing": portfolio.get("construction_method"),
        "leverage": portfolio.get("leverage"),
        "rebalance": portfolio.get("rebalance_frequency") or signal.get("rebalance_frequency"),
    }


def robustness_summary(windows: list[Mapping[str, Any]] | None, stability: Mapping[str, Any] | None = None) -> dict[str, Any]:
    rows = [row for row in (windows or []) if isinstance(row, dict)]
    count = len(rows)
    selected = [str(row.get("selected_trial_id") or "") for row in rows]
    unique = {item for item in selected if item}
    positive = 0
    beat = 0
    for row in rows:
        ml = row.get("ml") if isinstance(row.get("ml"), dict) else {}
        base = row.get("baseline") if isinstance(row.get("baseline"), dict) else {}
        sharpe = ml.get("sharpe_ratio")
        if sharpe is not None and float(sharpe) > 0:
            positive += 1
        if sharpe is not None and base.get("sharpe_ratio") is not None and float(sharpe) > float(base["sharpe_ratio"]):
            beat += 1
    trades = [((row.get("ml") or {}).get("trade_count") if isinstance(row.get("ml"), dict) else None) for row in rows]
    trade_vals = [float(item) for item in trades if item is not None]
    if not trade_vals:
        activity = "unknown"
    elif sum(trade_vals) / len(trade_vals) < 2:
        activity = "mostly cash / sparse"
    else:
        activity = "active"
    if count and len(unique) == 1:
        stability_label = "High"
    elif count and len(unique) <= 2:
        stability_label = "Medium"
    elif count:
        stability_label = "Low"
    else:
        stability_label = UNAVAILABLE
    winner = next(iter(unique), None)
    return {
        "selected_model": winner,
        "selected_in": "{0} / {1}".format(selected.count(winner) if winner else 0, count) if count else UNAVAILABLE,
        "positive_ml_sharpe": "{0} / {1}".format(positive, count) if count else UNAVAILABLE,
        "ml_beats_baseline": "{0} / {1}".format(beat, count) if count else UNAVAILABLE,
        "parameter_stability": stability_label,
        "signal_activity": activity,
        "unique_models": sorted(unique),
        "stability_raw": dict(stability or {}),
    }


def tidy_number(value: Any, *, digits: int = 4) -> Any:
    if value is None or value == UNAVAILABLE:
        return UNAVAILABLE
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return round(number, digits)


def format_model_choice(choice: Any) -> str:
    parsed = choice
    if isinstance(choice, str):
        parsed = parse_trial_id(choice)
    if not isinstance(parsed, dict) or not parsed:
        return UNAVAILABLE
    if parsed.get("label"):
        return str(parsed["label"])
    family = parsed.get("model_family")
    bits = [friendly_label(family) if family else None]
    if parsed.get("alpha") is not None:
        bits.append("alpha {0}".format(parsed["alpha"]))
    if parsed.get("l1_ratio") is not None:
        bits.append("l1_ratio {0}".format(parsed["l1_ratio"]))
    if parsed.get("lookback") is not None and parsed.get("alpha") is None:
        bits.append("lookback {0}".format(parsed["lookback"]))
    return " · ".join(item for item in bits if item) or str(parsed.get("trial_id") or UNAVAILABLE)


def format_validation(validation: Any) -> str:
    if isinstance(validation, str) and validation:
        return validation
    if not isinstance(validation, dict) or not validation:
        return UNAVAILABLE
    parts = []
    train = validation.get("train_years")
    oos = validation.get("oos_years")
    if train and oos:
        parts.append("rolling {0}-year train, {1}-year OOS".format(train, oos))
    first = validation.get("first_oos_year")
    last = validation.get("last_oos_year")
    if first and last:
        parts.append("{0}–{1}".format(first, last))
    return ", ".join(str(item) for item in parts) or UNAVAILABLE


def format_inner_cv(validation: Any) -> str:
    if not isinstance(validation, dict) or not validation:
        return UNAVAILABLE
    bits = []
    folds = validation.get("inner_folds") or validation.get("n_folds")
    if folds:
        bits.append("{0} chronological folds".format(folds))
    if validation.get("purge"):
        bits.append("purge")
    embargo = validation.get("embargo_sessions") or validation.get("embargo_trading_days")
    if embargo:
        bits.append("{0}-session embargo".format(embargo))
    return " + ".join(bits) or UNAVAILABLE


def format_execution(execution: Any) -> str:
    if isinstance(execution, str) and execution:
        return execution
    if not isinstance(execution, dict) or not execution:
        return UNAVAILABLE
    timing = execution.get("signal_timing") or "decision close → next valid session"
    fill = execution.get("fill_assumptions")
    if timing == "decision_close_next_session":
        timing = "decision close → next valid session"
    if fill:
        return "{0}; fill {1}".format(timing, fill)
    return str(timing)


def picker_label(row: Mapping[str, Any]) -> str:
    strategy_id = str(row.get("strategy_id") or row.get("name") or "")
    name = display_strategy_name(strategy_id, row.get("display_name") or (None if row.get("name") == strategy_id else row.get("name")))
    environment = str(row.get("environment") or "research").title()
    mode = friendly_label(row.get("research_mode") or row.get("status"), MODE_LABELS)
    if str(row.get("research_kind") or "") == "platform_research" or environment.lower() == "research":
        return "{0} · Research · {1}".format(name, mode if mode != UNAVAILABLE else "Research")
    return "{0} · {1}".format(name, environment)
