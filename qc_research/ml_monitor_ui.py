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
        "metric_source": dict(aggregate.get("metric_source") or {}),
        "windows": window_comparison_frame(aggregate),
        "feature_stability_table": feature_stability_frame(aggregate),
        "robustness_table": robustness_frame(aggregate),
        "show_section": True,
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
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("ML Sortino", view["ml"].get("sortino_ratio"))
        s2.metric("Baseline Sortino", view["baseline"].get("sortino_ratio"))
        s3.metric("Sharpe ML-baseline", (view.get("comparison") or {}).get("sharpe_ratio_diff"))
        s4.metric("CAGR ML-baseline", (view.get("comparison") or {}).get("cagr_diff"))
        source = ((view.get("metric_source") or {}).get("qc_runtime_statistics") or {})
        if source.get("available") is False:
            st.caption(source.get("reason") or "")
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
    if holdout is not None and not holdout.empty:
        with st.expander("Stage 2 holdout (excluded from research gate)"):
            st.dataframe(holdout, use_container_width=True, hide_index=True)
