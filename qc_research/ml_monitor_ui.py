"""Stage 2 Strategy Monitor skeleton.

Reads PostgreSQL only. Does not train models. Does not call QuantConnect.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import text

from qc_research.ml_aggregation import (
    assess_stage2,
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


def render_stage2_section(
    strategy_id: str,
    backtests: pd.DataFrame,
    *,
    engine=None,
) -> None:
    """Render a Stage 2 summary when Stage 2 rows exist. No QC / no ML."""
    stage = stage2_backtests(backtests)
    if stage is None or stage.empty:
        return

    st.header("STAGE 2 ML RESEARCH")
    st.caption(
        "Read-only PostgreSQL view of Stage 2 research. "
        "This page does not train models, launch QuantConnect jobs, "
        "or access the 2025+ holdout."
    )

    research = stage2_research_rows(stage)
    holdout = stage2_holdout_rows(stage)
    run_ids = []
    if "research_run_id" in stage.columns:
        run_ids = [
            value
            for value in stage["research_run_id"].dropna().astype(str).unique().tolist()
            if value
        ]
    selected_run = None
    if run_ids:
        selected_run = st.selectbox(
            "Stage 2 research run",
            run_ids,
            key="strategy_monitor_stage2_research_run",
        )
        run_research = research
        if selected_run and "research_run_id" in research.columns:
            run_research = research[research["research_run_id"].astype(str) == selected_run]
        assessment = assess_stage2(
            run_research,
            expected=31,
            holdout_rows=holdout,
        )
        st.write("Status: **{0}**".format(assessment["status"]))
        st.caption("Holdout rows are displayed separately and never change PASS/WATCH/FAIL.")
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
    _ = strategy_id
