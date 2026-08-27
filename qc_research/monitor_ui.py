"""Streamlit rendering for Stage 1 validation. Imported by strategy_monitor."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from qc_research.aggregation import (
    assess_stage1,
    filter_test_type,
    holdout_access_count,
    legacy_backtests,
    research_runs,
    select_comparison_backtest,
    stage1_backtests,
    walk_forward_aggregates,
)


def fmt_num(value, decimals=2):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_decimal_pct(value, decimals=2):
    """Format a stored decimal (0.12 -> 12.00%). Never uses abs<=1 heuristics."""
    if value is None:
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        return "—"
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def _parse_json(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _params_text(value) -> str:
    params = _parse_json(value)
    if not params:
        return "—"
    parts = []
    for key in sorted(params):
        if str(key).startswith("research_"):
            continue
        parts.append(f"{key}={params[key]}")
    return ", ".join(parts) if parts else "—"


def _plotly_line(df, x, y, title):
    try:
        import plotly.express as px

        fig = px.line(df, x=x, y=y, markers=True, title=title)
        fig.update_layout(margin=dict(l=10, r=10, t=40, b=10), height=360)
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        chart = df[[x, y]].copy()
        chart = chart.set_index(x)
        st.line_chart(chart, use_container_width=True)


def render_stage1_section(
    strategy_id: str,
    backtests: pd.DataFrame,
    *,
    load_equity,
    load_run_row,
):
    st.markdown("### Stage 1 Validation")
    st.caption(
        "Research results are not a live or paper promotion signal. "
        "Holdout stays sealed unless you explicitly ran it."
    )

    if backtests is None or backtests.empty:
        st.info("No backtests have been synced yet.")
        return None

    if "research_suite_version" not in backtests.columns:
        st.warning(
            "Stage 1 columns are not in PostgreSQL yet. "
            "Run `python -m jobs.apply_migrations` then sync backtests."
        )
        return None

    stage = stage1_backtests(backtests)
    runs = research_runs(backtests)

    if stage.empty:
        st.info(
            "No Stage 1 research backtests for this strategy yet. "
            "Legacy / other backtests remain listed below."
        )
        render_legacy(backtests)
        return None

    run_ids = runs["research_run_id"].dropna().astype(str).tolist()
    if not run_ids:
        st.info("Stage 1 backtests are missing research_run_id metadata.")
        render_legacy(backtests)
        return None

    selected_run = st.selectbox(
        "Research Run",
        run_ids,
        index=0,
        help="Defaults to the latest Stage 1 run for this strategy.",
    )
    run_df = stage[stage["research_run_id"].astype(str) == selected_run].copy()
    run_meta = runs[runs["research_run_id"].astype(str) == selected_run]
    run_row = run_meta.iloc[0] if not run_meta.empty else None
    db_run = load_run_row(selected_run) if load_run_row else None

    git_commit = None
    if run_row is not None:
        git_commit = run_row.get("git_commit")
    holdout_flag = bool(run_row["holdout_accessed"]) if run_row is not None else False
    holdout_count = holdout_access_count(backtests, git_commit)
    if db_run and db_run.get("holdout_access_count") is not None:
        holdout_count = max(holdout_count, int(db_run.get("holdout_access_count") or 0))
        holdout_flag = holdout_flag or bool(db_run.get("holdout_accessed"))

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Run ID", selected_run)
    c2.metric("Git commit", str(git_commit or "—")[:12])
    c3.metric("Suite", str((run_row or {}).get("suite_version") or "S1"))
    c4.metric("Backtests", int(len(run_df)))
    c5.metric("Holdout accessed?", "Yes" if holdout_flag else "No")

    st.caption(
        f"Holdout access count (this git commit): {holdout_count}  •  "
        f"Run date: {(run_row or {}).get('last_created') or '—'}"
    )
    if holdout_count > 1:
        st.error(
            "This Git commit has accessed final holdout data more than once. "
            "Repeated holdout evaluation contaminates the holdout."
        )

    tabs = st.tabs(
        [
            "Summary",
            "Split Tests",
            "Parameter Robustness",
            "Walk-Forward",
            "All Backtests",
        ]
    )
    with tabs[0]:
        render_summary(run_df, holdout_flag, holdout_count)
    with tabs[1]:
        render_split_tests(run_df)
    with tabs[2]:
        render_parameter_robustness(run_df)
    with tabs[3]:
        render_walk_forward(run_df)
    with tabs[4]:
        render_all_backtests(run_df, load_equity)

    render_legacy(backtests)
    return selected_run


def _thresholds_from_run(run_df: pd.DataFrame) -> dict[str, Any] | None:
    if "research_guide_json" not in run_df.columns:
        return None
    for value in run_df["research_guide_json"]:
        parsed = _parse_json(value)
        if parsed and "min_validation_sharpe" in parsed:
            return parsed
    return None


def render_summary(run_df: pd.DataFrame, holdout_flag: bool, holdout_count: int):
    assessment = assess_stage1(run_df, _thresholds_from_run(run_df))
    st.markdown(f"#### Overall: **{assessment['label']}**")
    st.caption(assessment.get("note") if assessment.get("note") else "The label is supplemental. Underlying metrics are shown below. Holdout is not used for PASS/WATCH/FAIL.")

    baseline = assessment.get("baseline") or {}
    validation = assessment.get("validation") or {}
    holdout = assessment.get("holdout")

    cols = st.columns(3)
    with cols[0]:
        st.markdown("**Development Baseline**")
        st.metric("Sharpe", fmt_num(baseline.get("sharpe")))
        st.metric("CAGR", fmt_decimal_pct(baseline.get("cagr")))
        st.metric("Max DD", fmt_decimal_pct(baseline.get("max_drawdown")))
    with cols[1]:
        st.markdown("**Fixed Validation**")
        st.metric("Sharpe", fmt_num(validation.get("sharpe")))
        st.metric("CAGR", fmt_decimal_pct(validation.get("cagr")))
        st.metric("Max DD", fmt_decimal_pct(validation.get("max_drawdown")))
    with cols[2]:
        st.markdown("**Final Holdout**")
        if not holdout_flag or holdout is None:
            st.success("Final holdout remains untouched.")
            st.caption("This is expected and good. Default Stage 1 does not spend the holdout.")
        else:
            st.metric("Sharpe", fmt_num(holdout.get("sharpe")))
            st.metric("CAGR", fmt_decimal_pct(holdout.get("cagr")))
            st.metric("Max DD", fmt_decimal_pct(holdout.get("max_drawdown")))
            if holdout_count > 1:
                st.warning("Holdout has been inspected more than once.")

    extra = st.columns(4)
    extra[0].metric("OOS / IS Sharpe", fmt_num(validation.get("oos_is_sharpe_ratio")))
    extra[1].metric(
        "Param robustness",
        str((assessment.get("robustness") or {}).get("robustness_label") or "—"),
    )
    extra[2].metric(
        "WFO profitable",
        fmt_decimal_pct((assessment.get("walk_forward") or {}).get("profitable_fraction")),
    )
    extra[3].metric(
        "Median WFO Sharpe",
        fmt_num((assessment.get("walk_forward") or {}).get("median_oos_sharpe")),
    )
    st.caption(
        "Worst WFO OOS Sharpe: "
        + fmt_num((assessment.get("walk_forward") or {}).get("worst_oos_sharpe"))
    )

    st.markdown("**Threshold checks**")
    for check in assessment.get("checks") or []:
        mark = "✓" if check.get("passed") else ("⚠" if check.get("name") == "parameter_neighborhood" else "✗")
        st.write(f"{mark} {check.get('detail')}")


def _metric_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in df.iterrows():
        rows.append(
            {
                "Test": row.get("research_test_type"),
                "Start": row.get("test_start") or row.get("backtest_start"),
                "End": row.get("test_end") or row.get("backtest_end"),
                "Parameters": _params_text(row.get("parameters_json")),
                "CAGR": fmt_decimal_pct(row.get("cagr")),
                "Sharpe": fmt_num(row.get("sharpe_ratio")),
                "Sortino": fmt_num(row.get("sortino_ratio")),
                "Max DD": fmt_decimal_pct(row.get("max_drawdown")),
                "Net Profit": fmt_decimal_pct(row.get("net_profit")),
                "Alpha": fmt_num(row.get("alpha")),
                "Beta": fmt_num(row.get("beta")),
                "Win Rate": fmt_decimal_pct(row.get("win_rate")),
                "Trades": row.get("trade_count"),
                "PSR": fmt_decimal_pct(row.get("psr")),
                "Backtest ID": row.get("backtest_id"),
            }
        )
    return pd.DataFrame(rows)


def render_split_tests(run_df: pd.DataFrame):
    wanted = ["BASELINE_DEV", "VALIDATION", "FINAL_HOLDOUT"]
    subset = run_df[run_df["research_test_type"].isin(wanted)].copy()
    if subset.empty:
        st.info("No split tests in this run yet.")
        return
    order = {name: i for i, name in enumerate(wanted)}
    subset["_ord"] = subset["research_test_type"].map(order)
    subset = subset.sort_values("_ord")
    st.dataframe(_metric_table(subset), use_container_width=True, hide_index=True)


def render_parameter_robustness(run_df: pd.DataFrame):
    grid = filter_test_type(run_df, "PARAM_SENS")
    if grid is None or grid.empty:
        st.info("No in-sample parameter sensitivity backtests in this run.")
        return
    assessment = assess_stage1(run_df, _thresholds_from_run(run_df))
    rob = assessment.get("robustness") or {}
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Default parameter", fmt_num(rob.get("default_parameter"), 0))
    k2.metric("Raw best parameter", fmt_num(rob.get("raw_best_parameter"), 0))
    k3.metric("Robust selected", fmt_num(rob.get("selected_parameter"), 0))
    k4.metric("Robustness", str(rob.get("robustness_label") or "—"))
    k5, k6, k7, k8 = st.columns(4)
    k5.metric("Best Sharpe", fmt_num(rob.get("best_sharpe")))
    k6.metric("Selected Sharpe", fmt_num(rob.get("selected_sharpe")))
    k7.metric("Positive fraction", fmt_decimal_pct(rob.get("positive_parameter_fraction")))
    k8.metric("Plateau width", fmt_num(rob.get("plateau_width"), 0))
    st.caption("The raw maximum is recorded for audit. Stage 1 does not automatically prefer it.")

    plot_rows = []
    for _, row in grid.iterrows():
        params = _parse_json(row.get("parameters_json"))
        sma = params.get("sma_period")
        try:
            sma = float(sma)
        except (TypeError, ValueError):
            sma = None
        sharpe = pd.to_numeric(pd.Series([row.get("sharpe_ratio")]), errors="coerce").iloc[0]
        if sma is not None and not pd.isna(sharpe):
            plot_rows.append({"sma_period": sma, "sharpe_ratio": float(sharpe)})
    if plot_rows:
        _plotly_line(pd.DataFrame(plot_rows).sort_values("sma_period"), "sma_period", "sharpe_ratio", "IS Sharpe vs sma_period")
    else:
        st.info("Primary parameter is not a single numeric series; showing table only.")

    display = grid.copy()
    display["parameters"] = display["parameters_json"].map(_params_text)
    cols = [
        "parameters",
        "sharpe_ratio",
        "cagr",
        "max_drawdown",
        "net_profit",
        "psr",
        "backtest_id",
        "status",
    ]
    cols = [c for c in cols if c in display.columns]
    st.dataframe(display[cols], use_container_width=True, hide_index=True)


def render_walk_forward(run_df: pd.DataFrame):
    tests = filter_test_type(run_df, "WFO_TEST")
    holdout_tests = filter_test_type(run_df, "WFO_TEST_HOLDOUT")
    trains = filter_test_type(run_df, "WFO_TRAIN")
    holdout_trains = filter_test_type(run_df, "WFO_TRAIN_HOLDOUT")
    agg = walk_forward_aggregates(run_df, include_holdout=False)

    kpis = st.columns(4)
    kpis[0].metric("Windows tested", agg.get("n_windows") or 0)
    kpis[1].metric("Profitable windows", agg.get("n_profitable") or 0)
    kpis[2].metric("Profitable fraction", fmt_decimal_pct(agg.get("profitable_fraction")))
    kpis[3].metric("Median OOS Sharpe", fmt_num(agg.get("median_oos_sharpe")))
    kpis2 = st.columns(4)
    kpis2[0].metric("Mean OOS Sharpe", fmt_num(agg.get("mean_oos_sharpe")))
    kpis2[1].metric("Worst OOS Sharpe", fmt_num(agg.get("worst_oos_sharpe")))
    kpis2[2].metric("Median OOS CAGR", fmt_decimal_pct(agg.get("median_oos_cagr")))
    kpis2[3].metric("Worst Max DD", fmt_decimal_pct(agg.get("worst_max_drawdown")))

    if tests is None or tests.empty:
        st.info("No walk-forward OOS tests in this run.")
    else:
        table = tests.copy()
        table["Selected Parameters"] = table["parameters_json"].map(_params_text)
        show = pd.DataFrame(
            {
                "Window": table.get("research_window_id"),
                "Train Start": table.get("train_start"),
                "Train End": table.get("train_end"),
                "Test Start": table.get("test_start"),
                "Test End": table.get("test_end"),
                "Selected Parameters": table["Selected Parameters"],
                "OOS CAGR": [fmt_decimal_pct(v) for v in table.get("cagr")],
                "OOS Sharpe": [fmt_num(v) for v in table.get("sharpe_ratio")],
                "OOS Sortino": [fmt_num(v) for v in table.get("sortino_ratio")],
                "OOS Max DD": [fmt_decimal_pct(v) for v in table.get("max_drawdown")],
                "Net Profit": [fmt_decimal_pct(v) for v in table.get("net_profit")],
                "Backtest ID": table.get("backtest_id"),
            }
        )
        st.dataframe(show, use_container_width=True, hide_index=True)

        chart_df = tests.copy()
        chart_df["sharpe_ratio"] = pd.to_numeric(chart_df["sharpe_ratio"], errors="coerce")
        chart_df = chart_df.dropna(subset=["sharpe_ratio"])
        if not chart_df.empty:
            _plotly_line(
                chart_df.sort_values("research_window_id"),
                "research_window_id",
                "sharpe_ratio",
                "OOS Sharpe by test window",
            )
            param_rows = []
            for _, row in chart_df.iterrows():
                params = _parse_json(row.get("parameters_json"))
                try:
                    sma = float(params.get("sma_period"))
                except (TypeError, ValueError):
                    sma = None
                if sma is not None:
                    param_rows.append(
                        {
                            "research_window_id": row.get("research_window_id"),
                            "sma_period": sma,
                        }
                    )
            if param_rows:
                _plotly_line(
                    pd.DataFrame(param_rows),
                    "research_window_id",
                    "sma_period",
                    "Selected sma_period by test window",
                )

    if holdout_tests is not None and not holdout_tests.empty:
        st.markdown("#### HOLDOUT walk-forward OOS")
        st.warning("These windows used holdout-period test years. They are not part of default Stage 1.")
        holdout_tests = holdout_tests.copy()
        holdout_tests["Selected Parameters"] = holdout_tests["parameters_json"].map(_params_text)
        st.dataframe(
            holdout_tests[
                [
                    c
                    for c in [
                        "research_window_id",
                        "train_start",
                        "train_end",
                        "test_start",
                        "test_end",
                        "Selected Parameters",
                        "cagr",
                        "sharpe_ratio",
                        "max_drawdown",
                        "backtest_id",
                    ]
                    if c in holdout_tests.columns
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Walk-Forward Training Grid Details"):
        if trains is None or trains.empty:
            st.info("No WFO_TRAIN backtests in this run.")
        else:
            trains = trains.copy()
            trains["parameters"] = trains["parameters_json"].map(_params_text)
            st.dataframe(
                trains[
                    [
                        c
                        for c in [
                            "research_window_id",
                            "parameters",
                            "train_start",
                            "train_end",
                            "sharpe_ratio",
                            "cagr",
                            "max_drawdown",
                            "status",
                            "backtest_id",
                        ]
                        if c in trains.columns
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )
        if holdout_trains is not None and not holdout_trains.empty:
            st.markdown("**HOLDOUT training grid**")
            holdout_trains = holdout_trains.copy()
            holdout_trains["parameters"] = holdout_trains["parameters_json"].map(_params_text)
            st.dataframe(holdout_trains, use_container_width=True, hide_index=True)


def render_all_backtests(run_df: pd.DataFrame, load_equity):
    df = run_df.copy()
    types = ["(all)"] + sorted(df["research_test_type"].dropna().astype(str).unique().tolist())
    windows = ["(all)"] + sorted(df["research_window_id"].dropna().astype(str).unique().tolist())
    statuses = ["(all)"] + sorted(df["status"].dropna().astype(str).unique().tolist())
    f1, f2, f3, f4 = st.columns(4)
    test_type = f1.selectbox("Test Type", types)
    window = f2.selectbox("Window", windows)
    status = f3.selectbox("Status", statuses)
    holdout_mode = f4.selectbox("Holdout", ["Non-Holdout", "Holdout", "All"])

    filtered = df
    if test_type != "(all)":
        filtered = filtered[filtered["research_test_type"] == test_type]
    if window != "(all)":
        filtered = filtered[filtered["research_window_id"].astype(str) == window]
    if status != "(all)":
        filtered = filtered[filtered["status"] == status]
    if "research_is_holdout" in filtered.columns:
        flag = filtered["research_is_holdout"].fillna(False).astype(bool)
        if holdout_mode == "Non-Holdout":
            filtered = filtered[~flag]
        elif holdout_mode == "Holdout":
            filtered = filtered[flag]

    display = filtered.copy()
    display["Parameters"] = display["parameters_json"].map(_params_text) if "parameters_json" in display.columns else "—"
    columns = {
        "name": "Name",
        "backtest_id": "Backtest ID",
        "research_test_type": "Test Type",
        "research_phase": "Phase",
        "research_window_id": "Window",
        "test_start": "Start",
        "test_end": "End",
        "research_git_commit": "Git Commit",
        "Parameters": "Parameters",
        "cagr": "CAGR",
        "sharpe_ratio": "Sharpe",
        "sortino_ratio": "Sortino",
        "max_drawdown": "Max DD",
        "net_profit": "Net Profit",
        "alpha": "Alpha",
        "beta": "Beta",
        "win_rate": "Win Rate",
        "trade_count": "Trades",
        "psr": "PSR",
        "status": "Status",
        "created_at": "Created",
    }
    keep = [c for c in columns if c in display.columns]
    table = display[keep].rename(columns=columns)
    st.dataframe(table, use_container_width=True, hide_index=True)

    ids = filtered["backtest_id"].astype(str).tolist()
    if not ids:
        return
    labels = [
        f"{row.get('name')} ({row.get('backtest_id')})"
        for _, row in filtered.iterrows()
    ]
    choice = st.selectbox("Selected backtest", labels)
    selected = filtered.iloc[labels.index(choice)]
    st.markdown(f"**{selected.get('name')}**")
    st.write(f"QuantConnect backtest ID: `{selected.get('backtest_id')}`")
    st.write(f"Git commit: `{selected.get('research_git_commit')}`")
    st.write(
        f"{selected.get('research_test_type')} / {selected.get('research_phase')} / "
        f"window {selected.get('research_window_id')}"
    )
    st.write(f"Dates: {selected.get('test_start')} → {selected.get('test_end')}")
    st.write("Parameters: " + _params_text(selected.get("parameters_json")))
    metrics = {
        "Sharpe": fmt_num(selected.get("sharpe_ratio")),
        "Sortino": fmt_num(selected.get("sortino_ratio")),
        "CAGR": fmt_decimal_pct(selected.get("cagr")),
        "Max DD": fmt_decimal_pct(selected.get("max_drawdown")),
        "Net Profit": fmt_decimal_pct(selected.get("net_profit")),
        "Alpha": fmt_num(selected.get("alpha")),
        "Beta": fmt_num(selected.get("beta")),
        "Win Rate": fmt_decimal_pct(selected.get("win_rate")),
        "Trades": selected.get("trade_count"),
        "PSR": fmt_decimal_pct(selected.get("psr")),
        "Status": selected.get("status"),
    }
    st.json(metrics)
    if selected.get("error_message"):
        st.error(str(selected.get("error_message")))

    with st.expander("Raw statistics"):
        st.json(_parse_json(selected.get("raw_statistics_json")))
    with st.expander("QuantConnect researchGuide"):
        st.json(_parse_json(selected.get("research_guide_json")))

    equity = load_equity(selected.get("backtest_id")) if load_equity else pd.DataFrame()
    if equity is None or equity.empty:
        st.info("Equity curve not synced yet.")
    else:
        chart = equity.copy()
        chart["equity"] = pd.to_numeric(chart["equity"], errors="coerce")
        chart = chart.dropna(subset=["timestamp", "equity"]).set_index("timestamp")
        st.line_chart(chart["equity"], use_container_width=True)


def render_legacy(backtests: pd.DataFrame):
    legacy = legacy_backtests(backtests)
    with st.expander("Legacy / Other Backtests"):
        if legacy is None or legacy.empty:
            st.info("No legacy backtests. Older QuantConnect backtests are never deleted.")
            return
        st.caption(
            "These backtests have no Stage 1 research metadata and are excluded "
            "from Stage 1 aggregate statistics."
        )
        st.dataframe(legacy, use_container_width=True, hide_index=True)


def render_backtest_vs_paper(backtests: pd.DataFrame, snapshot, trades, fmt_num_paper, fmt_pct_paper):
    st.markdown("### Backtest vs Paper")
    if backtests is None or backtests.empty:
        st.info("No QuantConnect backtests have been synced yet.")
        return
    choice = select_comparison_backtest(backtests)
    if choice is None:
        st.info("No comparison backtest available.")
        return
    row = choice["row"]
    st.caption(f"Paper compared with: {choice['label']}")
    st.caption(
        "Short paper history does not support meaningful Sharpe/CAGR until enough "
        "live data exists. Those paper cells stay blank on purpose."
    )
    comparison = pd.DataFrame(
        {
            "Metric": [
                "CAGR",
                "Sharpe",
                "Sortino",
                "Max Drawdown",
                "Net Profit",
                "Alpha",
                "Beta",
                "Win Rate",
                "Trades",
            ],
            "Backtest": [
                fmt_decimal_pct(row.get("cagr")) if choice["source"] == "stage1" else fmt_pct_paper(row.get("cagr")),
                fmt_num_paper(row.get("sharpe_ratio")),
                fmt_num_paper(row.get("sortino_ratio")),
                fmt_decimal_pct(row.get("max_drawdown")) if choice["source"] == "stage1" else fmt_pct_paper(row.get("max_drawdown")),
                fmt_decimal_pct(row.get("net_profit")) if choice["source"] == "stage1" else fmt_pct_paper(row.get("net_profit")),
                fmt_num_paper(row.get("alpha")),
                fmt_num_paper(row.get("beta")),
                fmt_decimal_pct(row.get("win_rate")) if choice["source"] == "stage1" else fmt_pct_paper(row.get("win_rate")),
                row.get("trade_count"),
            ],
            "Paper": [
                "—",
                "—",
                "—",
                fmt_pct_paper(snapshot["drawdown"]) if snapshot else "—",
                fmt_pct_paper(snapshot["total_return"]) if snapshot else "—",
                "—",
                "—",
                "—",
                len(trades) if trades is not None else "—",
            ],
        }
    )
    st.dataframe(comparison, use_container_width=True, hide_index=True)
    st.write(f"QuantConnect backtest ID: `{row.get('backtest_id')}`")
