"""Stage 2 Strategy Monitor.

Reads PostgreSQL only. Does not train models. Does not call QuantConnect.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import text

from qc_research.ml_aggregation import (
    COMPLETE,
    STAGE2_THRESHOLD_KEYS,
    assess_stage2,
    nonholdout_research_experiment_count,
    stage2_backtests,
    stage2_holdout_rows,
    stage2_research_rows,
)


def _read_sql(engine, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    if engine is None:
        return pd.DataFrame()
    try:
        return pd.read_sql(text(sql), engine, params=params or {})
    except Exception:
        return pd.DataFrame()


def _as_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


UNAVAILABLE = "Unavailable / Not applicable"


def classify_monitor_provenance(value: Any) -> str:
    text = str(value or "").strip()
    if text == "REAL_QC":
        return "REAL_QC"
    if text in {"LOCAL_TEST", "REAL_HISTORICAL_PRE_2025"}:
        return "LOCAL_TEST"
    return "UNAVAILABLE"


def format_monitor_value(
    value: Any,
    *,
    available: bool = True,
    reconstructed: bool = False,
    reconstructed_note: str | None = None,
    provenance: str | None = None,
) -> Any:
    if provenance in {"SYNTHETIC_TEST_ONLY", "UNAVAILABLE"}:
        return UNAVAILABLE
    if not available or value is None:
        return UNAVAILABLE
    if reconstructed:
        return {
            "value": value,
            "source_label": reconstructed_note
            or "monthly-sampled drawdown; not QuantConnect Max Drawdown",
        }
    return value


def infer_research_labels(
    *,
    strategy_id: str,
    run_summary: dict[str, Any] | None = None,
    assessment: dict[str, Any] | None = None,
) -> dict[str, str]:
    summary = dict(run_summary or {})
    assessment = dict(assessment or {})
    mode = summary.get("research_mode") or assessment.get("research_mode")
    asset = summary.get("asset_class") or assessment.get("asset_class")
    family = summary.get("strategy_family_id") or assessment.get("strategy_family")
    if not mode:
        if strategy_id == "SPYTrend":
            mode = "MANUAL"
        elif strategy_id == "CrossSectionalFactorML" or summary.get("research_kind") == "stage2_ml":
            mode = "ML_DISCOVERY"
        else:
            mode = "UNKNOWN"
    if not asset:
        if strategy_id == "SPYTrend":
            asset = "ETF"
        elif strategy_id == "CrossSectionalFactorML":
            asset = "US_EQUITY"
        else:
            asset = "UNKNOWN"
    if not family:
        if strategy_id == "SPYTrend":
            family = "TIME_SERIES_TREND"
        elif strategy_id == "CrossSectionalFactorML":
            family = "CROSS_SECTIONAL_FACTOR"
        else:
            family = "UNKNOWN"
    mode_label = {"MANUAL": "Manual", "ML_DISCOVERY": "ML Discovery"}.get(str(mode), str(mode))
    asset_label = {
        "US_EQUITY": "Equity",
        "ETF": "ETF",
        "PAIR": "Pair",
        "BOND_ETF": "Bond ETF",
        "FUTURE": "Futures",
        "TREASURY_FUTURE": "Treasury Futures",
        "RATE_FUTURE": "Rates Futures",
        "MULTI_ASSET": "Multi Asset",
        "FIXED_INCOME_PROXY": "Fixed Income Proxy",
    }.get(str(asset), str(asset))
    provenance = str(summary.get("provenance") or assessment.get("provenance") or "")
    state = str(summary.get("research_state") or assessment.get("research_state") or "")
    return {
        "research_mode": str(mode),
        "research_mode_label": mode_label,
        "asset_class": str(asset),
        "asset_class_label": asset_label,
        "strategy_family": str(family),
        "research_state": state or UNAVAILABLE,
        "artifact_provenance": provenance or UNAVAILABLE,
        "economic_gate": str(assessment.get("economic_gate") or summary.get("economic_gate") or UNAVAILABLE),
        "cost_model_id": str(summary.get("cost_model_id") or assessment.get("cost_model_id") or UNAVAILABLE),
        "spec_hash": str(summary.get("config_fingerprint") or summary.get("strategy_spec_hash") or UNAVAILABLE),
        "git_sha": str(summary.get("git_sha") or UNAVAILABLE),
        "lineage": str(summary.get("research_lineage_id") or UNAVAILABLE),
        "trial_count": summary.get("trial_count"),
        "search_space_hash": str(summary.get("search_space_hash") or UNAVAILABLE),
        "model_family": str(summary.get("model_family") or UNAVAILABLE),
        "selected_candidate": str(summary.get("selected_candidate") or summary.get("selected_trial_id") or UNAVAILABLE),
        "baseline_trial_id": str(summary.get("baseline_trial_id") or UNAVAILABLE),
        "provenance_kind": classify_monitor_provenance(provenance),
    }


def load_platform_run_ids(engine, strategy_id: str) -> list[str]:
    rows = _read_sql(
        engine,
        """
        SELECT DISTINCT research_run_id
        FROM research_artifacts
        WHERE research_run_id LIKE :prefix
           OR research_run_id LIKE 'PLATFORM_%'
        ORDER BY 1
        """,
        {"prefix": "%{0}%".format(strategy_id)},
    )
    if rows is None or rows.empty:
        return []
    return [str(value) for value in rows["research_run_id"].dropna().astype(str).tolist() if value]


def build_platform_monitor_view(
    *,
    strategy_id: str,
    selected_run: str | None,
    run_summary: dict[str, Any] | None = None,
    assessment: dict[str, Any] | None = None,
    oos: dict[str, Any] | None = None,
    spec: dict[str, Any] | None = None,
    search_space: dict[str, Any] | None = None,
    trials: dict[str, Any] | None = None,
    pair: dict[str, Any] | None = None,
    fixed_income: dict[str, Any] | None = None,
    roll: dict[str, Any] | None = None,
    model_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not selected_run:
        return None
    summary = dict(run_summary or {})
    inner_summary = summary.get("payload") if isinstance(summary.get("payload"), dict) else summary
    inner_assess = (assessment or {}).get("payload") if isinstance((assessment or {}).get("payload"), dict) else (assessment or {})
    inner_oos = (oos or {}).get("payload") if isinstance((oos or {}).get("payload"), dict) else (oos or {})
    inner_spec = (spec or {}).get("payload") if isinstance((spec or {}).get("payload"), dict) else (spec or {})
    identity = dict(inner_spec.get("identity") or {})
    merged_summary = {
        **inner_summary,
        "research_mode": inner_summary.get("research_mode") or identity.get("research_mode"),
        "asset_class": inner_summary.get("asset_class") or identity.get("asset_class"),
        "strategy_family_id": inner_summary.get("strategy_family_id") or identity.get("strategy_family_id"),
        "research_state": inner_summary.get("research_state") or inner_assess.get("research_state"),
        "provenance": summary.get("provenance") or inner_summary.get("provenance"),
        "config_fingerprint": identity.get("config_fingerprint") or summary.get("config_fingerprint"),
        "research_lineage_id": identity.get("research_lineage_id"),
        "cost_model_id": (inner_spec.get("costs") or {}).get("cost_model_id") or inner_summary.get("cost_model_id"),
        "trial_count": (trials or {}).get("payload", trials or {}).get("trial_count")
        if isinstance(trials, dict)
        else None,
        "git_sha": identity.get("git_sha"),
        "search_space_hash": inner_summary.get("search_space_hash"),
        "model_family": inner_summary.get("model_family"),
        "selected_candidate": inner_summary.get("selected_candidate") or inner_summary.get("selected_trial_id"),
        "baseline_trial_id": inner_summary.get("baseline_trial_id"),
        "history_provider": inner_summary.get("history_provider"),
        "feature_schema_hash": inner_summary.get("feature_schema_hash"),
        "winner_backtest_id": inner_summary.get("winner_backtest_id"),
        "baseline_backtest_id": inner_summary.get("baseline_backtest_id"),
        "economic_gate": inner_summary.get("economic_gate") or inner_assess.get("economic_gate"),
    }
    inner_model = (model_metadata or {}).get("payload") if isinstance((model_metadata or {}).get("payload"), dict) else (model_metadata or {})
    intercept_only = inner_model.get("intercept_only")
    if intercept_only is None:
        intercept_only = (inner_model.get("fitted_model") or {}).get("intercept_only")
    if intercept_only is None:
        intercept_only = inner_summary.get("intercept_only")
    if isinstance(search_space, dict):
        space_payload = search_space.get("payload") if isinstance(search_space.get("payload"), dict) else search_space
        merged_summary["search_space_hash"] = merged_summary.get("search_space_hash") or space_payload.get("search_space_hash")
    if isinstance(trials, dict):
        payload = trials.get("payload") if isinstance(trials.get("payload"), dict) else trials
        merged_summary["trial_count"] = payload.get("trial_count")
        merged_summary["selected_candidate"] = merged_summary.get("selected_candidate") or payload.get("selected_trial_id")
    labels = infer_research_labels(
        strategy_id=strategy_id,
        run_summary=merged_summary,
        assessment=inner_assess,
    )
    sharpe = inner_oos.get("sharpe_ratio")
    windows = inner_oos.get("windows")
    provenance_kind = labels.get("provenance_kind") or classify_monitor_provenance(merged_summary.get("provenance"))
    return {
        "strategy_id": strategy_id,
        "research_run_id": selected_run,
        "labels": labels,
        "research_mode_label": labels["research_mode_label"],
        "asset_class_label": labels["asset_class_label"],
        "strategy_family": labels["strategy_family"],
        "research_state": format_monitor_value(labels.get("research_state"), available=bool(labels.get("research_state") and labels.get("research_state") != UNAVAILABLE)),
        "provenance": provenance_kind,
        "provenance_kind": provenance_kind,
        "economic_gate": format_monitor_value(labels.get("economic_gate"), available=labels.get("economic_gate") not in {None, "", UNAVAILABLE}),
        "spec_hash": format_monitor_value(labels.get("spec_hash"), available=labels.get("spec_hash") not in {None, "", UNAVAILABLE}),
        "git_sha": format_monitor_value(labels.get("git_sha"), available=labels.get("git_sha") not in {None, "", UNAVAILABLE}),
        "lineage": format_monitor_value(labels.get("lineage"), available=labels.get("lineage") not in {None, "", UNAVAILABLE}),
        "cost_model": format_monitor_value(labels.get("cost_model_id"), available=labels.get("cost_model_id") not in {None, "", UNAVAILABLE}),
        "trial_count": format_monitor_value(merged_summary.get("trial_count"), available=merged_summary.get("trial_count") is not None),
        "search_space_hash": format_monitor_value(labels.get("search_space_hash"), available=labels.get("search_space_hash") not in {None, "", UNAVAILABLE}),
        "model_family": format_monitor_value(labels.get("model_family"), available=labels.get("model_family") not in {None, "", UNAVAILABLE}),
        "selected_candidate": format_monitor_value(labels.get("selected_candidate"), available=labels.get("selected_candidate") not in {None, "", UNAVAILABLE}),
        "baseline": format_monitor_value(labels.get("baseline_trial_id"), available=labels.get("baseline_trial_id") not in {None, "", UNAVAILABLE}),
        "sharpe": format_monitor_value(sharpe, available=sharpe is not None, provenance=str(inner_oos.get("provenance") or merged_summary.get("provenance") or "")),
        "oos_windows": windows if windows else UNAVAILABLE,
        "search_space": search_space,
        "pair": pair,
        "fixed_income": fixed_income,
        "roll": roll,
        "trials": trials,
        "history_provider": format_monitor_value(
            merged_summary.get("history_provider"),
            available=merged_summary.get("history_provider") not in {None, ""},
        ),
        "feature_schema_hash": format_monitor_value(
            merged_summary.get("feature_schema_hash"),
            available=merged_summary.get("feature_schema_hash") not in {None, ""},
        ),
        "winner_backtest_id": format_monitor_value(
            merged_summary.get("winner_backtest_id"),
            available=merged_summary.get("winner_backtest_id") not in {None, ""},
        ),
        "baseline_backtest_id": format_monitor_value(
            merged_summary.get("baseline_backtest_id"),
            available=merged_summary.get("baseline_backtest_id") not in {None, ""},
        ),
        "intercept_only": format_monitor_value(intercept_only, available=intercept_only is not None),
        "intercept_only_flag": intercept_only if isinstance(intercept_only, bool) else None,
        "economic_pass": False if str(labels.get("economic_gate") or "") == "NOT_DEFINED" else None,
    }


def render_platform_section(strategy_id: str, *, engine=None) -> None:
    """PostgreSQL-only view of MANUAL / ML_DISCOVERY platform runs."""
    run_ids = load_platform_run_ids(engine, strategy_id)
    if not run_ids:
        return
    st.header("PLATFORM RESEARCH")
    st.caption(
        "Read-only PostgreSQL view of generic MANUAL / ML_DISCOVERY runs. "
        "This page does not call QuantConnect. Unavailable values are never shown as 0."
    )
    selected_run = st.selectbox(
        "Platform research run",
        run_ids,
        key="strategy_monitor_platform_research_run",
    )
    if not selected_run:
        return
    run_summary = load_stage2_artifact_payload(engine, selected_run, "run_summary")
    assessment = load_stage2_artifact_payload(engine, selected_run, "assessment")
    oos = load_stage2_artifact_payload(engine, selected_run, "oos_aggregate")
    spec = load_stage2_artifact_payload(engine, selected_run, "strategy_spec")
    search_space = load_stage2_artifact_payload(engine, selected_run, "search_space")
    trials = load_stage2_artifact_payload(engine, selected_run, "trials")
    model_metadata = load_stage2_artifact_payload(engine, selected_run, "model_metadata")
    pair = load_stage2_artifact_payload(engine, selected_run, "pair_diagnostics")
    fixed_income = load_stage2_artifact_payload(engine, selected_run, "fixed_income_diagnostics") or load_stage2_artifact_payload(
        engine, selected_run, "fixed_income_risk"
    )
    roll = load_stage2_artifact_payload(engine, selected_run, "roll_diagnostics") or load_stage2_artifact_payload(
        engine, selected_run, "futures_roll_diagnostics"
    )
    view = build_platform_monitor_view(
        strategy_id=strategy_id,
        selected_run=selected_run,
        run_summary=run_summary,
        assessment=assessment,
        oos=oos,
        spec=spec,
        search_space=search_space,
        trials=trials,
        pair=pair,
        fixed_income=fixed_income,
        roll=roll,
        model_metadata=model_metadata,
    )
    if view is None:
        return
    st.caption(
        "Mode: {0} · Asset: {1} · Family: {2} · State: {3} · Provenance: {4}".format(
            view["research_mode_label"],
            view["asset_class_label"],
            view["strategy_family"],
            view["research_state"],
            view["provenance"],
        )
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Economic gate", view["economic_gate"])
    c2.metric("Spec hash", view["spec_hash"])
    c3.metric("Trial count", view["trial_count"])
    c4.metric("Cost model", view["cost_model"])
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Lineage", view["lineage"])
    d2.metric("Git SHA", view["git_sha"])
    d3.metric("OOS Sharpe", view["sharpe"])
    d4.metric("Provenance", view.get("provenance_kind") or view["provenance"])
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("Search space", view.get("search_space_hash"))
    e2.metric("Model family", view.get("model_family"))
    e3.metric("Selected candidate", view.get("selected_candidate"))
    e4.metric("Baseline", view.get("baseline"))
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("History provider", view.get("history_provider"))
    f2.metric("Feature schema", view.get("feature_schema_hash"))
    f3.metric("Winner QC id", view.get("winner_backtest_id"))
    f4.metric("Baseline QC id", view.get("baseline_backtest_id"))
    if view.get("intercept_only_flag") is True:
        st.warning(
            "Winner is intercept-only. This is infrastructure evidence; economic_gate stays NOT_DEFINED."
        )
    if view.get("oos_windows") not in {None, UNAVAILABLE}:
        st.subheader("OOS windows")
        st.write(view["oos_windows"])
    if view.get("search_space"):
        st.subheader("Search space")
        st.write(view["search_space"])
    if view.get("trials"):
        st.subheader("Trial ledger")
        st.write(view["trials"])
        payload = view["trials"].get("payload") if isinstance(view["trials"], dict) else None
        if isinstance(payload, dict) and payload.get("provenance") == "SYNTHETIC_TEST_ONLY":
            st.error("Synthetic trial ledger cannot be research evidence.")
    if view.get("pair"):
        st.subheader("Pair diagnostics")
        st.write(view["pair"])
        inner = view["pair"].get("payload") if isinstance(view["pair"], dict) else view["pair"]
        if isinstance(inner, dict) and inner.get("selection_used_oos"):
            st.error("Pair selection used OOS — invalid research.")
    if view.get("fixed_income"):
        st.subheader("Fixed-income / DV01 diagnostics")
        st.write(view["fixed_income"])
        st.caption("Unsupported cash-bond metrics are Unavailable / Not applicable, never zero-filled.")
    if view.get("roll"):
        st.subheader("Roll diagnostics")
        st.write(view["roll"])


def load_stage2_trials(engine, research_run_id: str) -> pd.DataFrame:
    return _read_sql(
        engine,
        """
        SELECT *
        FROM ml_trials
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id, trial_id
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_models(engine, research_run_id: str) -> pd.DataFrame:
    return _read_sql(
        engine,
        """
        SELECT *
        FROM ml_models
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_feature_diagnostics(engine, research_run_id: str) -> pd.DataFrame:
    return _read_sql(
        engine,
        """
        SELECT *
        FROM ml_feature_diagnostics
        WHERE research_run_id = :research_run_id
        ORDER BY outer_window_id, coefficient_rank NULLS LAST
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_signal_points(engine, research_run_id: str) -> pd.DataFrame:
    return _read_sql(
        engine,
        """
        SELECT *
        FROM ml_signal_points
        WHERE research_run_id = :research_run_id
        ORDER BY timestamp
        """,
        {"research_run_id": research_run_id},
    )


def load_stage2_run_ids(engine, strategy_id: str) -> list[str]:
    if engine is None:
        return []
    rows = _read_sql(
        engine,
        """
        SELECT DISTINCT research_run_id
        FROM research_runs
        WHERE research_kind = 'stage2_ml'
          AND strategy_id = :strategy_id
        UNION
        SELECT DISTINCT research_run_id
        FROM research_artifacts
        WHERE research_run_id LIKE :prefix
        ORDER BY 1
        """,
        {
            "strategy_id": strategy_id,
            "prefix": "STAGE2_{0}_%".format(strategy_id),
        },
    )
    if rows is None or rows.empty:
        return []
    return [str(value) for value in rows["research_run_id"].dropna().astype(str).tolist() if value]


def load_stage2_artifact_payload(engine, research_run_id: str, artifact_type: str) -> dict[str, Any] | None:
    rows = _read_sql(
        engine,
        """
        SELECT payload_json
        FROM research_artifacts
        WHERE research_run_id = :research_run_id
          AND artifact_type = :artifact_type
        ORDER BY synced_at DESC NULLS LAST
        LIMIT 1
        """,
        {"research_run_id": research_run_id, "artifact_type": artifact_type},
    )
    if rows is None or rows.empty:
        return None
    return _as_payload(rows.iloc[0].get("payload_json"))


def window_comparison_frame(aggregate: dict[str, Any] | None) -> pd.DataFrame:
    rows = []
    for item in (aggregate or {}).get("windows") or []:
        ml = item.get("ml") or {}
        baseline = item.get("baseline") or {}
        delta = item.get("ml_minus_baseline") or {}
        rows.append(
            {
                "window_id": item.get("window_id"),
                "selected_alpha": item.get("selected_alpha"),
                "robustness_label": item.get("robustness_label"),
                "ml_median_rank_ic": ml.get("median_rank_ic"),
                "baseline_median_rank_ic": baseline.get("median_rank_ic"),
                "median_rank_ic_diff": delta.get("median_rank_ic"),
                "ml_compounded_net": ml.get("compounded_net_return"),
                "baseline_compounded_net": baseline.get("compounded_net_return"),
                "net_diff": delta.get("compounded_net_return"),
                "ml_sharpe": ml.get("sharpe_ratio"),
                "baseline_sharpe": baseline.get("sharpe_ratio"),
                "sharpe_diff": delta.get("sharpe_ratio"),
                "ml_sortino": ml.get("sortino_ratio"),
                "baseline_sortino": baseline.get("sortino_ratio"),
                "sortino_diff": delta.get("sortino_ratio"),
                "ml_cagr": ml.get("cagr"),
                "baseline_cagr": baseline.get("cagr"),
                "cagr_diff": delta.get("cagr"),
                "ml_max_drawdown": ml.get("max_drawdown"),
                "baseline_max_drawdown": baseline.get("max_drawdown"),
                "max_drawdown_diff": delta.get("max_drawdown"),
                "ml_turnover": ml.get("annualized_turnover"),
                "baseline_turnover": baseline.get("annualized_turnover"),
            }
        )
    return pd.DataFrame(rows)


def feature_stability_frame(aggregate: dict[str, Any] | None) -> pd.DataFrame:
    payload = (aggregate or {}).get("feature_stability") or {}
    rows = []
    for item in payload.get("features") or []:
        rows.append(
            {
                "feature_name": item.get("feature_name"),
                "majority_sign": item.get("majority_sign"),
                "sign_agreement_frequency": item.get("sign_agreement_frequency"),
                "median_ridge_coefficient": item.get("median_ridge_coefficient"),
                "median_normalized_magnitude": item.get("median_normalized_magnitude"),
                "stdev_normalized_magnitude": item.get("stdev_normalized_magnitude"),
                "median_coefficient_rank": item.get("median_coefficient_rank"),
                "stdev_coefficient_rank": item.get("stdev_coefficient_rank"),
                "median_normalized_magnitude_rank": item.get("median_normalized_magnitude_rank"),
                "stdev_normalized_magnitude_rank": item.get("stdev_normalized_magnitude_rank"),
                "mean_univariate_rank_ic": item.get("mean_univariate_rank_ic"),
                "median_univariate_rank_ic": item.get("median_univariate_rank_ic"),
                "positive_ic_fraction": item.get("positive_ic_fraction"),
            }
        )
    frame = pd.DataFrame(rows)
    order = payload.get("feature_order") or []
    if not frame.empty and order:
        frame["_order"] = frame["feature_name"].map({name: index for index, name in enumerate(order)})
        frame = frame.sort_values("_order", kind="stable").drop(columns=["_order"])
    return frame


def robustness_frame(aggregate: dict[str, Any] | None) -> pd.DataFrame:
    stability = (aggregate or {}).get("stability") or {}
    counts = dict(stability.get("robustness_label_counts") or {})
    frequency = dict(stability.get("robustness_label_frequency") or {})
    rows = []
    for label in ("STABLE_PLATEAU", "ISOLATED_PEAK", "WEAK_SIGNAL"):
        rows.append(
            {
                "robustness_label": label,
                "count": counts.get(label, 0),
                "frequency": frequency.get(label),
            }
        )
    for label, count in counts.items():
        if label in {"STABLE_PLATEAU", "ISOLATED_PEAK", "WEAK_SIGNAL"}:
            continue
        rows.append({"robustness_label": label, "count": count, "frequency": frequency.get(label)})
    return pd.DataFrame(rows)


def build_stage2_monitor_view(
    *,
    strategy_id: str,
    selected_run: str | None,
    assessment: dict[str, Any] | None = None,
    aggregate: dict[str, Any] | None = None,
    run_summary: dict[str, Any] | None = None,
    research_rows: pd.DataFrame | None = None,
    holdout_rows: pd.DataFrame | None = None,
) -> dict[str, Any] | None:
    """Pure monitor model. No Streamlit, QC, or Object Store."""
    if not selected_run:
        return None
    summary = dict(run_summary or {})
    aggregate = dict(aggregate or {})
    completed = summary.get("completed_qc_experiments")
    if completed is None and research_rows is not None and not research_rows.empty:
        status = research_rows.get("status")
        if status is not None:
            completed = int(status.astype(str).str.lower().eq("completed").sum())
    expected = int(summary.get("expected_qc_experiments") or 31)
    resolved = dict(assessment or {})
    if not resolved:
        oos_metrics = None
        ml = aggregate.get("ml") if aggregate else None
        if isinstance(ml, dict) and ml:
            oos_metrics = {
                "median_rank_ic": ml.get("median_rank_ic"),
                "positive_ic_fraction": ml.get("positive_ic_fraction"),
            }
        resolved = assess_stage2(
            research_rows,
            expected=expected,
            completed=completed,
            failed=int(summary.get("failed_qc_experiments") or 0),
            skipped=int(summary.get("skipped_qc_experiments") or 0),
            oos_metrics=oos_metrics,
            holdout_rows=holdout_rows,
            run_summary=summary,
            research_experiment_count=nonholdout_research_experiment_count(summary, research_rows),
        )
    accounting = (
        aggregate.get("create_accounting")
        or resolved.get("create_accounting")
        or {
            "created_backtests": summary.get("created_backtests"),
            "created_backtests_this_process": summary.get("created_backtests_this_process"),
            "original_suite_qc_creates": summary.get("original_suite_qc_creates"),
            "salvage_qc_creates": summary.get("salvage_qc_creates"),
            "salvage": summary.get("salvage"),
        }
    )
    source = dict(aggregate.get("metric_source") or {})
    qc_runtime = dict(source.get("qc_runtime_statistics") or {})
    reconstructed = qc_runtime.get("available") is False
    labels = infer_research_labels(
        strategy_id=strategy_id,
        run_summary=summary,
        assessment=resolved,
    )
    return {
        "strategy_id": strategy_id,
        "research_run_id": selected_run,
        "progress": resolved.get("progress"),
        "status": resolved.get("status"),
        "economic_gate": resolved.get("economic_gate"),
        "economic_status": resolved.get("economic_status"),
        "label_uses_holdout": False,
        "holdout_excluded": True,
        "reasons": list(resolved.get("reasons") or []),
        "create_accounting": dict(accounting or {}),
        "research_experiment_count": resolved.get("research_experiment_count"),
        "supported_threshold_keys": list(resolved.get("supported_threshold_keys") or STAGE2_THRESHOLD_KEYS),
        "ml": dict(aggregate.get("ml") or {}),
        "baseline": dict(aggregate.get("baseline") or {}),
        "comparison": dict(aggregate.get("comparison") or {}),
        "stability": dict(aggregate.get("stability") or {}),
        "feature_stability": dict(aggregate.get("feature_stability") or {}),
        "cost": dict(aggregate.get("cost") or {}),
        "metric_source": source,
        "windows": window_comparison_frame(aggregate),
        "feature_stability_table": feature_stability_frame(aggregate),
        "robustness_table": robustness_frame(aggregate),
        "show_section": True,
        "research_mode": labels["research_mode"],
        "research_mode_label": labels["research_mode_label"],
        "asset_class": labels["asset_class"],
        "asset_class_label": labels["asset_class_label"],
        "strategy_family": labels["strategy_family"],
        "max_drawdown_display": format_monitor_value(
            (aggregate.get("ml") or {}).get("max_drawdown"),
            available=(aggregate.get("ml") or {}).get("max_drawdown") is not None,
            reconstructed=reconstructed,
        ),
        "qc_max_drawdown_available": bool(qc_runtime.get("available")),
    }


def render_stage2_section(
    strategy_id: str,
    backtests: pd.DataFrame,
    *,
    engine=None,
) -> None:
    """Render a Stage 2 summary from PostgreSQL rows and ingested JSON."""
    stage = stage2_backtests(backtests)
    research = stage2_research_rows(stage) if stage is not None else pd.DataFrame()
    holdout = stage2_holdout_rows(stage) if stage is not None else pd.DataFrame()
    run_ids = []
    if stage is not None and not stage.empty and "research_run_id" in stage.columns:
        run_ids.extend(
            [
                value
                for value in stage["research_run_id"].dropna().astype(str).unique().tolist()
                if value
            ]
        )
    for run_id in load_stage2_run_ids(engine, strategy_id):
        if run_id not in run_ids:
            run_ids.append(run_id)
    if not run_ids:
        return

    st.header("STAGE 2 ML RESEARCH")
    st.caption(
        "Read-only PostgreSQL view of Stage 2 research. "
        "This page does not train models, launch QuantConnect jobs, "
        "or access the 2025+ holdout."
    )

    selected_run = st.selectbox(
        "Stage 2 research run",
        run_ids,
        key="strategy_monitor_stage2_research_run",
    )
    run_research = research
    if selected_run and research is not None and not research.empty and "research_run_id" in research.columns:
        run_research = research[research["research_run_id"].astype(str) == selected_run]
    aggregate = load_stage2_artifact_payload(engine, selected_run, "oos_aggregate") if selected_run else None
    assessment = (
        load_stage2_artifact_payload(engine, selected_run, "nonholdout_assessment") if selected_run else None
    )
    run_summary = load_stage2_artifact_payload(engine, selected_run, "run_summary") if selected_run else None
    view = build_stage2_monitor_view(
        strategy_id=strategy_id,
        selected_run=selected_run,
        assessment=assessment,
        aggregate=aggregate,
        run_summary=run_summary,
        research_rows=run_research,
        holdout_rows=holdout,
    )
    if view is None:
        return
    st.write("Operational status: **{0}**".format(view["status"]))
    st.caption(
        "Research mode: {0} · Asset class: {1} · Family: {2} · Provenance: {3}".format(
            view.get("research_mode_label") or "Unavailable / Not applicable",
            view.get("asset_class_label") or "Unavailable / Not applicable",
            view.get("strategy_family") or "Unavailable / Not applicable",
            (run_summary or {}).get("provenance") or "Unavailable / Not applicable",
        )
    )
    economic = view.get("economic_status") or view.get("economic_gate") or "NOT_DEFINED"
    st.write("Economic gate: **{0}**".format(economic))
    st.caption(
        "Holdout rows are displayed separately and never change PASS/WATCH/FAIL. "
        "Economic PASS/WATCH/FAIL is applied only when Stage 2 thresholds are defined."
    )
    accounting = view.get("create_accounting") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Original suite QC creates", accounting.get("original_suite_qc_creates") if accounting.get("original_suite_qc_creates") is not None else "—")
    c2.metric(
        "Creates this process",
        accounting.get("created_backtests_this_process")
        if accounting.get("created_backtests_this_process") is not None
        else accounting.get("created_backtests")
        if accounting.get("created_backtests") is not None
        else "—",
    )
    c3.metric("Salvage", "yes" if accounting.get("salvage") else "no")
    c4.metric(
        "Non-holdout experiments",
        view.get("research_experiment_count") if view.get("research_experiment_count") is not None else "—",
    )
    if view.get("ml"):
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("ML median Rank IC", view["ml"].get("median_rank_ic"))
        m2.metric("Baseline median Rank IC", view["baseline"].get("median_rank_ic"))
        m3.metric(
            "Windows ML IC > baseline",
            "{0}/{1}".format(
                (view.get("comparison") or {}).get("windows_ml_ic_gt_baseline"),
                (view.get("comparison") or {}).get("windows_compared_ic"),
            ),
        )
        stability = (view.get("stability") or {}).get("parameter_selection_stability")
        m4.metric("Alpha stability", stability)
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("ML Sharpe (stitched net)", view["ml"].get("sharpe_ratio"))
        r2.metric("Baseline Sharpe", view["baseline"].get("sharpe_ratio"))
        r3.metric("ML CAGR", view["ml"].get("cagr"))
        r4.metric("ML max drawdown", view["ml"].get("max_drawdown"))
        if isinstance(view.get("max_drawdown_display"), dict):
            st.caption(view["max_drawdown_display"].get("source_label"))
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("ML Sortino", view["ml"].get("sortino_ratio"))
        s2.metric("Baseline Sortino", view["baseline"].get("sortino_ratio"))
        s3.metric("Sharpe ML-baseline", (view.get("comparison") or {}).get("sharpe_ratio_diff"))
        s4.metric("CAGR ML-baseline", (view.get("comparison") or {}).get("cagr_diff"))
        source = ((view.get("metric_source") or {}).get("qc_runtime_statistics") or {})
        if source.get("available") is False:
            st.caption(source.get("reason") or "")
            st.caption(
                "Max drawdown is monthly-sampled from reconstructed net returns; "
                "it is not QuantConnect Max Drawdown."
            )
    windows = view.get("windows")
    if windows is not None and not windows.empty:
        st.subheader("Outer OOS windows (ML vs baseline)")
        st.dataframe(windows, use_container_width=True, hide_index=True)
    robustness = view.get("robustness_table")
    if robustness is not None and not robustness.empty:
        st.subheader("Parameter robustness")
        st.dataframe(robustness, use_container_width=True, hide_index=True)
        st.write("Selected-alpha distribution", (view.get("stability") or {}).get("selected_alpha_distribution"))
    features = view.get("feature_stability_table")
    if features is not None and not features.empty:
        st.subheader("Feature stability (PRICE_TECH_V1 order)")
        st.dataframe(features, use_container_width=True, hide_index=True)
    if view.get("cost"):
        st.subheader("Cost / slippage stress (5 / 10 / 20 bps)")
        st.write(view["cost"])
    st.caption(
        "Supported Stage 2 threshold keys (unassigned on V1): {0}".format(
            ", ".join(view.get("supported_threshold_keys") or [])
        )
    )
    if run_research is not None and not run_research.empty:
        st.dataframe(run_research, use_container_width=True, hide_index=True)
    if selected_run and engine is not None:
        trials = load_stage2_trials(engine, selected_run)
        models = load_stage2_models(engine, selected_run)
        features = load_stage2_feature_diagnostics(engine, selected_run)
        signals = load_stage2_signal_points(engine, selected_run)
        if not trials.empty:
            st.subheader("Internal ML trials")
            st.dataframe(trials, use_container_width=True, hide_index=True)
        if not models.empty:
            st.subheader("Selected models")
            st.dataframe(models, use_container_width=True, hide_index=True)
        if not features.empty:
            st.subheader("Feature diagnostics")
            st.dataframe(features, use_container_width=True, hide_index=True)
        if not signals.empty:
            st.subheader("Rank IC history")
            st.dataframe(signals, use_container_width=True, hide_index=True)
        pair = load_stage2_artifact_payload(engine, selected_run, "pair_diagnostics")
        if pair:
            st.subheader("Pair diagnostics")
            st.write(pair)
            if pair.get("selection_used_oos"):
                st.error("Pair selection used OOS — invalid research.")
        fi = load_stage2_artifact_payload(engine, selected_run, "fixed_income_risk")
        if fi:
            st.subheader("Fixed-income diagnostics")
            st.write(fi)
            st.caption("Unsupported cash-bond metrics are Unavailable / Not applicable, never zero-filled.")
    if holdout is not None and not holdout.empty:
        with st.expander("Stage 2 holdout (excluded from research gate)"):
            st.dataframe(holdout, use_container_width=True, hide_index=True)
