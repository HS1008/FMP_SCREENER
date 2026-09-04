"""Fail-closed economic gates.

Mirrored from quant-strategies/research/platform/economic_gate.py.
Keep the two copies in semantic lockstep.

Operational completeness is independent of economic PASS/WATCH/FAIL.

Rules:
- No configured thresholds -> economic_gate=NOT_DEFINED (not PASS)
- PASS only if every configured gate was evaluated and passed
- Unknown configured key -> must not PASS (FAIL closed)
- Configured key with missing metric -> must not PASS (FAIL closed)
- Reserved/unimplemented configured key -> must not PASS (FAIL closed)
"""

from __future__ import annotations

from typing import Any, Mapping


PASS = "PASS"
WATCH = "WATCH"
FAIL = "FAIL"
COMPLETE = "COMPLETE"
ECONOMIC_GATE_APPLIED = "APPLIED"
ECONOMIC_GATE_NOT_DEFINED = "NOT_DEFINED"

# Stage 2 V1 schema plus prospective platform keys.
SUPPORTED_THRESHOLD_KEYS = (
    "min_median_oos_rank_ic",
    "min_positive_ic_fraction",
    "min_windows_ml_ic_gt_baseline_fraction",
    "min_windows_ml_net_gt_baseline_fraction",
    "min_ml_minus_baseline_risk_adjusted",
    "cost_stress_robustness",
    "min_parameter_or_feature_stability",
    "min_signal_quality",
    "min_positive_window_fraction",
    "min_baseline_outperformance_fraction",
    "min_net_risk_adjusted_delta",
    "min_parameter_stability",
    "min_feature_stability",
    "pbo_dsr_conditions",
)

# Numeric comparison: (metric_key, severity, metric aliases...)
# severity FAIL → economic FAIL if metric < threshold
# severity WATCH → economic WATCH if metric < threshold
EVALUATED_THRESHOLD_SPECS = {
    "min_median_oos_rank_ic": ("fail", ("median_rank_ic",)),
    "min_positive_ic_fraction": ("watch", ("positive_ic_fraction",)),
    "min_windows_ml_ic_gt_baseline_fraction": (
        "watch",
        ("windows_ml_ic_gt_baseline_fraction",),
    ),
    "min_windows_ml_net_gt_baseline_fraction": (
        "watch",
        ("windows_ml_net_gt_baseline_fraction",),
    ),
    "min_ml_minus_baseline_risk_adjusted": (
        "fail",
        ("ml_minus_baseline_risk_adjusted", "sharpe_ratio_diff"),
    ),
    "min_signal_quality": ("fail", ("signal_quality", "median_rank_ic")),
    "min_positive_window_fraction": (
        "watch",
        ("positive_window_fraction", "positive_ic_fraction"),
    ),
    "min_baseline_outperformance_fraction": (
        "watch",
        ("baseline_outperformance_fraction", "windows_ml_net_gt_baseline_fraction"),
    ),
    "min_net_risk_adjusted_delta": (
        "fail",
        ("net_risk_adjusted_delta", "ml_minus_baseline_risk_adjusted", "sharpe_ratio_diff"),
    ),
    "min_parameter_stability": ("watch", ("parameter_selection_stability",)),
    "min_feature_stability": ("watch", ("feature_stability_score", "mean_sign_agreement_frequency")),
}

RESERVED_THRESHOLD_KEYS = tuple(
    key for key in SUPPORTED_THRESHOLD_KEYS if key not in EVALUATED_THRESHOLD_SPECS
)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric(metrics: Mapping[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        parsed = _as_float(metrics.get(name))
        if parsed is not None:
            return parsed
    return None


def apply_economic_gates(
    thresholds: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | None,
    *,
    supported_keys: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Apply configured economic gates. Never infers missing metrics as 0."""
    keys = tuple(supported_keys or SUPPORTED_THRESHOLD_KEYS)
    gates = {key: value for key, value in dict(thresholds or {}).items() if value is not None}
    clean_metrics = {
        key: value
        for key, value in dict(metrics or {}).items()
        if "holdout" not in str(key).lower()
    }
    result = {
        "thresholds": dict(gates),
        "oos_metrics": dict(clean_metrics),
        "supported_threshold_keys": list(keys),
        "unevaluated_threshold_keys": [],
        "unknown_threshold_keys": [],
        "economic_gate": None,
        "economic_status": None,
        "status": None,
        "reasons": [],
    }
    if not gates:
        result["economic_gate"] = ECONOMIC_GATE_NOT_DEFINED
        result["reasons"] = ["thresholds_not_defined"]
        return result

    result["economic_gate"] = ECONOMIC_GATE_APPLIED
    unknown = [key for key in gates if key not in keys]
    unevaluated: list[str] = []
    failures: list[str] = []
    watches: list[str] = []

    for key, raw in gates.items():
        if key in unknown:
            continue
        spec = EVALUATED_THRESHOLD_SPECS.get(key)
        if spec is None:
            unevaluated.append(key)
            continue
        severity, metric_names = spec
        metric = _metric(clean_metrics, metric_names)
        threshold = _as_float(raw)
        if metric is None or threshold is None:
            unevaluated.append(key)
            continue
        if metric < threshold:
            if severity == "fail":
                failures.append(key)
            else:
                watches.append(key)

    result["unknown_threshold_keys"] = unknown
    result["unevaluated_threshold_keys"] = unevaluated + list(unknown)

    if failures:
        result["status"] = FAIL
        result["economic_status"] = FAIL
        result["reasons"] = failures
        return result
    if unknown or unevaluated:
        # Fail closed: configured but not fully evaluated. Operational status
        # stays COMPLETE; economic assessment must not PASS.
        result["status"] = COMPLETE
        result["economic_status"] = FAIL
        reasons = []
        if unknown:
            reasons.extend("unknown_threshold:{0}".format(key) for key in unknown)
        if unevaluated:
            reasons.extend("unevaluated_threshold:{0}".format(key) for key in unevaluated)
        result["reasons"] = reasons
        return result
    if watches:
        result["status"] = WATCH
        result["economic_status"] = WATCH
        result["reasons"] = watches
        return result
    result["status"] = PASS
    result["economic_status"] = PASS
    return result
