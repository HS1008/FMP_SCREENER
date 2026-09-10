import json
import logging
import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import text

from db.dashboard_engine import dashboard_engine

engine = dashboard_engine()
from qc_research.aggregation import smoke_backtests, stage1_backtests
from qc_research.monitor_ui import (
    render_backtest_vs_paper,
    render_smoke_section,
    render_stage1_section,
)
from qc_research.ml_monitor_ui import (
    load_platform_run_ids,
    render_platform_section,
    render_stage2_section,
)
from qc_research.platform_presentation import UNAVAILABLE, display_strategy_name, picker_label
from qc_research.research_library import (
    filter_library,
    library_display_frame,
    load_research_library,
    load_strategy_runs,
)
from qc_research.streamlit_tables import arrow_safe_frame


logger = logging.getLogger(__name__)

LIVE_MONITOR_REFRESH = "30s"
_LAST_OK_DATA_KEY = "strategy_monitor_last_ok_data"


try:
    st.set_page_config(
        page_title="Strategy Monitor",
        page_icon="📈",
        layout="wide",
    )
except Exception:
    pass

st.title("Strategy Monitor")
st.caption(
    "Read-only research library and backtest results from PostgreSQL. "
    "This page does not launch backtests, train models, approve strategies, or place orders. "
    "Live monitor data updates automatically as new synchronized results become available."
)


# =========================================================
# DATABASE LOADERS
# =========================================================

def load_strategies():
    registered = pd.read_sql(
        """
        SELECT
            strategy_id,
            name,
            environment,
            status,
            qc_project_id,
            qc_deployment_id,
            qc_research_project_id,
            qc_research_project_name,
            git_commit,
            rules_json,
            created_at,
            updated_at
        FROM strategies
        ORDER BY name
        """,
        engine,
    )
    try:
        extra = pd.read_sql(
            """
            SELECT DISTINCT ON (strategy_id)
                strategy_id,
                strategy_id AS name,
                'research' AS environment,
                CASE
                    WHEN COALESCE(run_status, '') IN ('', 'HUMAN_REVIEW_REQUIRED', 'RESEARCH_COMPLETE')
                    THEN 'COMPLETE'
                    ELSE run_status
                END AS status,
                NULL::varchar AS qc_project_id,
                NULL::varchar AS qc_deployment_id,
                NULL::varchar AS qc_research_project_id,
                NULL::varchar AS qc_research_project_name,
                NULL::varchar AS git_commit,
                NULL::jsonb AS rules_json,
                first_seen_at AS created_at,
                last_seen_at AS updated_at,
                research_mode,
                research_kind,
                asset_class
            FROM research_runs
            WHERE research_kind = 'platform_research'
              AND strategy_id IS NOT NULL
              AND strategy_id <> ''
            ORDER BY strategy_id, last_seen_at DESC NULLS LAST
            """,
            engine,
        )
    except Exception:
        extra = pd.DataFrame()
    if extra is None or extra.empty:
        combined = registered
    elif registered is None or registered.empty:
        combined = extra
    else:
        have = set(registered["strategy_id"].astype(str))
        add = extra[~extra["strategy_id"].astype(str).isin(have)]
        combined = registered if add.empty else pd.concat([registered, add], ignore_index=True)
    return _enrich_strategy_research_labels(combined)


def _enrich_strategy_research_labels(strategies):
    if strategies is None or strategies.empty:
        return strategies
    work = strategies.copy()
    for column in ("research_mode", "research_kind", "asset_class", "delivery_status"):
        if column not in work.columns:
            work[column] = None
    try:
        meta = pd.read_sql(
            """
            SELECT DISTINCT ON (strategy_id)
                strategy_id,
                research_mode,
                research_kind,
                asset_class,
                delivery_status,
                last_seen_at
            FROM research_runs
            WHERE strategy_id IS NOT NULL
              AND strategy_id <> ''
            ORDER BY strategy_id, last_seen_at DESC NULLS LAST
            """,
            engine,
        )
    except Exception:
        try:
            meta = pd.read_sql(
                """
                SELECT DISTINCT ON (strategy_id)
                    strategy_id,
                    research_mode,
                    research_kind,
                    asset_class,
                    last_seen_at
                FROM research_runs
                WHERE strategy_id IS NOT NULL
                  AND strategy_id <> ''
                ORDER BY strategy_id, last_seen_at DESC NULLS LAST
                """,
                engine,
            )
        except Exception:
            return work
    if meta is None or meta.empty:
        return work
    work = work.drop(columns=[col for col in ("research_mode", "research_kind", "asset_class", "delivery_status", "last_seen_at") if col in work.columns], errors="ignore")
    return work.merge(meta, on="strategy_id", how="left")


def load_strategy_by_id(strategy_id):
    """Reload one strategy row so fragment refreshes see updated status."""
    rows = pd.read_sql(
        text(
            """
            SELECT
                strategy_id,
                name,
                environment,
                status,
                qc_project_id,
                qc_deployment_id,
                qc_research_project_id,
                qc_research_project_name,
                git_commit,
                rules_json,
                created_at,
                updated_at
            FROM strategies
            WHERE strategy_id = :strategy_id
            """
        ),
        engine,
        params={"strategy_id": strategy_id},
    )
    if rows is None or rows.empty:
        return None
    return rows.iloc[0]


def load_latest_snapshot(strategy_id):
    query = text("""
        SELECT
            timestamp,
            equity,
            cash,
            holdings_value,
            daily_return,
            total_return,
            drawdown,
            status
        FROM live_snapshots
        WHERE strategy_id = :strategy_id
          AND equity > 0
        ORDER BY timestamp DESC
        LIMIT 1
    """)

    with engine.connect() as conn:
        return conn.execute(
            query,
            {"strategy_id": strategy_id},
        ).mappings().first()


def load_equity_history(strategy_id):
    history = pd.read_sql(
        text("""
            SELECT
                timestamp,
                equity,
                cash,
                holdings_value,
                total_return,
                drawdown
            FROM live_snapshots
            WHERE strategy_id = :strategy_id
            ORDER BY timestamp ASC
        """),
        engine,
        params={"strategy_id": strategy_id},
    )

    return filter_valid_equity_history(history)


def load_latest_positions(strategy_id):
    return pd.read_sql(
        text("""
            SELECT
                symbol,
                quantity,
                price,
                market_value,
                weight,
                timestamp
            FROM positions
            WHERE strategy_id = :strategy_id
              AND timestamp = (
                  SELECT MAX(timestamp)
                  FROM positions
                  WHERE strategy_id = :strategy_id
              )
            ORDER BY ABS(market_value) DESC
        """),
        engine,
        params={"strategy_id": strategy_id},
    )


def load_orders(strategy_id):
    return pd.read_sql(
        text("""
            SELECT
                timestamp,
                symbol,
                direction,
                quantity,
                order_type,
                status,
                fill_price,
                qc_order_id
            FROM orders
            WHERE strategy_id = :strategy_id
            ORDER BY timestamp DESC NULLS LAST
            LIMIT 50
        """),
        engine,
        params={"strategy_id": strategy_id},
    )


def load_trades(strategy_id):
    return pd.read_sql(
        text("""
            SELECT
                symbol,
                entry_time,
                exit_time,
                quantity,
                entry_price,
                exit_price,
                pnl
            FROM trades
            WHERE strategy_id = :strategy_id
            ORDER BY exit_time DESC NULLS LAST
            LIMIT 50
        """),
        engine,
        params={"strategy_id": strategy_id},
    )


def load_backtests(strategy_id):
    query = """
        SELECT
            backtest_id,
            strategy_id,
            name,
            status,
            created_at,
            sharpe_ratio,
            sortino_ratio,
            alpha,
            beta,
            cagr,
            max_drawdown,
            net_profit,
            win_rate,
            loss_rate,
            trade_count,
            psr,
            research_suite_version,
            research_run_id,
            research_experiment_id,
            research_test_type,
            research_phase,
            research_window_id,
            research_git_commit,
            research_is_holdout,
            research_dirty,
            train_start,
            train_end,
            test_start,
            test_end,
            parameters_json,
            objective_name,
            objective_value,
            raw_statistics_json,
            research_guide_json,
            research_thresholds_json,
            research_primary_parameter,
            research_selection_summary_json,
            research_lineage_id,
            economic_parameter_count,
            research_metadata_count,
            backtest_start,
            backtest_end,
            error_message
        FROM backtests
        WHERE strategy_id = :strategy_id
        ORDER BY created_at DESC NULLS LAST
    """
    try:
        return pd.read_sql(
            text(query),
            engine,
            params={"strategy_id": strategy_id},
        )
    except Exception:
        return pd.read_sql(
            text("""
                SELECT
                    backtest_id,
                    name,
                    status,
                    created_at,
                    sharpe_ratio,
                    sortino_ratio,
                    alpha,
                    beta,
                    cagr,
                    max_drawdown,
                    net_profit,
                    win_rate,
                    loss_rate,
                    trade_count,
                    psr
                FROM backtests
                WHERE strategy_id = :strategy_id
                ORDER BY created_at DESC NULLS LAST
            """),
            engine,
            params={"strategy_id": strategy_id},
        )


def load_backtest_equity(backtest_id):
    try:
        return pd.read_sql(
            text("""
                SELECT
                    timestamp,
                    equity,
                    period_return,
                    series_name
                FROM backtest_equity_points
                WHERE backtest_id = :backtest_id
                ORDER BY timestamp ASC
            """),
            engine,
            params={"backtest_id": backtest_id},
        )
    except Exception:
        return pd.DataFrame()


def load_research_run(run_id):
    try:
        with engine.connect() as conn:
            return conn.execute(
                text("""
                    SELECT
                        research_run_id,
                        strategy_id,
                        suite_version,
                        git_commit,
                        dirty,
                        first_seen_at,
                        last_seen_at,
                        holdout_accessed,
                        holdout_access_count,
                        config_json,
                        research_lineage_id,
                        expected_experiment_count,
                        synced_experiment_count,
                        completed_count,
                        failed_count,
                        skipped_count,
                        run_status,
                        holdout_exposure_status,
                        holdout_start,
                        holdout_end,
                        orchestrator_summary_json
                    FROM research_runs
                    WHERE research_run_id = :run_id
                """),
                {"run_id": run_id},
            ).mappings().first()
    except Exception:
        return None


# =========================================================
# HELPERS
# =========================================================

def filter_valid_equity_history(history):
    """Drop snapshots that cannot be a real equity path.

    Always drops equity <= 0. Also drops cash-only collapses that are
    inconsistent with later holdings-bearing equity (the signature of
    the prior QuantConnect holdings parser bug).

    This is relative to observed equity, not a hardcoded dollar floor,
    so other strategies with different starting capital still work.
    """

    if history is None or history.empty:
        return history

    df = history.copy()

    df["equity"] = pd.to_numeric(
        df["equity"],
        errors="coerce",
    )
    df["cash"] = pd.to_numeric(
        df.get("cash"),
        errors="coerce",
    )
    df["holdings_value"] = pd.to_numeric(
        df.get("holdings_value"),
        errors="coerce",
    )

    df = df.dropna(subset=["equity"])
    df = df[df["equity"] > 0]

    if df.empty:
        return df

    holdings = df["holdings_value"].fillna(0)
    invested = df.loc[holdings.abs() > 1e-6]

    if not invested.empty:
        reference_equity = float(invested["equity"].median())
        cash = df["cash"].fillna(0)
        cash_only = holdings.abs() < 1e-6
        cash_equals_equity = (
            (cash - df["equity"]).abs()
            <= (df["equity"].abs() * 0.02 + 1.0)
        )
        collapsed = df["equity"] < (0.5 * reference_equity)

        df = df.loc[~(cash_only & cash_equals_equity & collapsed)]

    return df.reset_index(drop=True)


def fmt_money(value):
    if value is None or pd.isna(value):
        return "—"

    return f"${float(value):,.2f}"


def fmt_pct(value):
    if value is None or pd.isna(value):
        return "—"

    value = float(value)

    # QC may return some metrics either as decimals or percentage units.
    if abs(value) <= 1:
        value *= 100

    return f"{value:.2f}%"


def fmt_num(value, decimals=2):
    if value is None or pd.isna(value):
        return "—"

    return f"{float(value):.{decimals}f}"


def parse_rules(value):
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    try:
        return json.loads(value)
    except Exception:
        return {}


def status_badge(status):
    status_text = str(status or "UNKNOWN")
    status_lower = status_text.lower()

    if status_lower == "running":
        return f"🟢 {status_text}"

    if status_lower in {
        "initializing",
        "deploying",
    }:
        return f"🟡 {status_text}"

    if status_lower in {
        "runtimeerror",
        "error",
        "invalid",
    }:
        return f"🔴 {status_text}"

    if status_lower in {
        "stopped",
        "liquidated",
    }:
        return f"⚪ {status_text}"

    return f"⚫ {status_text}"


def has_execution_deployment(strategy, snapshot=None, history=None, positions=None, orders=None, trades=None):
    """True only when a paper/live deployment actually exists."""
    if strategy is None:
        return False
    if strategy.get("qc_deployment_id") or strategy.get("qc_project_id"):
        return True
    environment = str(strategy.get("environment") or "").strip().lower()
    if environment in {"paper", "live", "trading"}:
        return True
    if snapshot:
        return True
    for frame in (history, positions, orders, trades):
        if frame is not None and getattr(frame, "empty", True) is False:
            return True
    return False


def strategy_has_platform_research(strategy_id):
    try:
        return bool(load_platform_run_ids(engine, strategy_id))
    except Exception:
        return False


def _query_live_monitor_data(strategy_id, fallback_strategy):
    """PostgreSQL reads only. Never calls QuantConnect or launches backtests."""
    latest = load_strategy_by_id(strategy_id)
    strategy = latest if latest is not None else fallback_strategy
    return {
        "strategy": strategy,
        "snapshot": load_latest_snapshot(strategy_id),
        "history": load_equity_history(strategy_id),
        "positions": load_latest_positions(strategy_id),
        "orders": load_orders(strategy_id),
        "trades": load_trades(strategy_id),
        "backtests": load_backtests(strategy_id),
    }


def _render_refresh_debug():
    flag = str(os.environ.get("STREAMLIT_REFRESH_DEBUG") or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    st.caption("Monitor fragment refreshed: {0}".format(stamp))


def _render_live_strategy_monitor(strategy_id, strategy):
    _render_refresh_debug()
    # A widget click inside a fragment reruns only this fragment (Streamlit 1.37+).
    # Do not trigger a full-app rerun — that remounts the whole page.
    st.button(
        "Refresh now",
        key="strategy_monitor_refresh",
        help="Reload monitor data from PostgreSQL without reloading the page.",
    )

    cache_key = "{0}:{1}".format(_LAST_OK_DATA_KEY, strategy_id)
    try:
        data = _query_live_monitor_data(strategy_id, strategy)
        st.session_state[cache_key] = data
    except Exception:
        logger.exception(
            "Strategy Monitor live query failed for strategy_id=%s",
            strategy_id,
        )
        st.error(
            "Unable to refresh latest Strategy Monitor data. "
            "Last successfully rendered data may be stale."
        )
        data = st.session_state.get(cache_key)
        if not data:
            return

    strategy = data.get("strategy") if data.get("strategy") is not None else strategy
    snapshot = data.get("snapshot")
    history = data.get("history")
    positions = data.get("positions")
    orders = data.get("orders")
    trades = data.get("trades")
    backtests = data.get("backtests")
    if history is None:
        history = pd.DataFrame()
    if positions is None:
        positions = pd.DataFrame()
    if orders is None:
        orders = pd.DataFrame()
    if trades is None:
        trades = pd.DataFrame()
    if backtests is None:
        backtests = pd.DataFrame()

    _render_live_monitor_body(
        strategy_id,
        strategy,
        snapshot,
        history,
        positions,
        orders,
        trades,
        backtests,
    )


@st.fragment(run_every=LIVE_MONITOR_REFRESH)
def render_live_strategy_monitor_auto(strategy_id, strategy):
    _render_live_strategy_monitor(strategy_id, strategy)


@st.fragment
def render_live_strategy_monitor_manual(strategy_id, strategy):
    _render_live_strategy_monitor(strategy_id, strategy)


def _render_paper_and_execution(snapshot, history, positions, orders, trades, backtests):
    # =========================================================
    # CURRENT PAPER STATE
    # =========================================================

    st.markdown("### Current Paper State")

    if snapshot:

        equity = snapshot["equity"]
        cash = snapshot["cash"]
        holdings_value = snapshot["holdings_value"]
        total_return = snapshot["total_return"]
        drawdown = snapshot["drawdown"]

        col1, col2, col3, col4, col5 = st.columns(5)

        col1.metric(
            "Equity",
            fmt_money(equity),
        )

        col2.metric(
            "Cash",
            fmt_money(cash),
        )

        col3.metric(
            "Holdings",
            fmt_money(holdings_value),
        )

        col4.metric(
            "Paper Return",
            fmt_pct(total_return),
        )

        col5.metric(
            "Drawdown",
            fmt_pct(drawdown),
        )

        st.caption(
            f"Last portfolio sync: {snapshot['timestamp']}"
        )

    else:
        st.info("No live paper snapshot available yet.")


    # =========================================================
    # PAPER EQUITY CURVE
    # =========================================================

    st.markdown("### Paper Performance")

    if history.empty:
        st.info("No historical paper snapshots available.")

    else:
        chart_data = history[
            ["timestamp", "equity"]
        ].copy()

        chart_data["equity"] = pd.to_numeric(
            chart_data["equity"],
            errors="coerce",
        )

        chart_data = (
            chart_data
            .dropna()
            .set_index("timestamp")
        )

        st.line_chart(
            chart_data["equity"],
            use_container_width=True,
        )


    # =========================================================
    # CURRENT POSITIONS
    # =========================================================

    st.markdown("### Current Positions")

    if positions.empty:

        st.info("No open positions currently recorded.")

    else:

        display = positions.copy()

        display["quantity"] = pd.to_numeric(
            display["quantity"],
            errors="coerce",
        )

        display["price"] = pd.to_numeric(
            display["price"],
            errors="coerce",
        ).map(
            lambda x: fmt_money(x)
        )

        display["market_value"] = pd.to_numeric(
            display["market_value"],
            errors="coerce",
        ).map(
            lambda x: fmt_money(x)
        )

        display["weight"] = pd.to_numeric(
            display["weight"],
            errors="coerce",
        ).map(
            lambda x: fmt_pct(x)
        )

        display = display[
            [
                "symbol",
                "quantity",
                "price",
                "market_value",
                "weight",
            ]
        ]

        display.columns = [
            "Symbol",
            "Quantity",
            "Price",
            "Market Value",
            "Weight",
        ]

        st.dataframe(
            display,
            use_container_width=True,
            hide_index=True,
        )


    # =========================================================
    # BACKTEST VS PAPER
    # =========================================================

    render_backtest_vs_paper(
        backtests,
        snapshot,
        trades,
        fmt_num,
        fmt_pct,
    )


    # =========================================================
    # EXECUTION
    # =========================================================

    st.markdown("### Execution")

    tab_orders, tab_trades = st.tabs(
        [
            "Orders / Fills",
            "Closed Trades",
        ]
    )


    with tab_orders:

        if orders.empty:

            st.info(
                "No orders have been synced."
            )

        else:

            display_orders = orders.copy()

            if "fill_price" in display_orders:
                display_orders["fill_price"] = (
                    pd.to_numeric(
                        display_orders[
                            "fill_price"
                        ],
                        errors="coerce",
                    )
                    .map(
                        lambda x:
                        fmt_money(x)
                        if not pd.isna(x)
                        else "—"
                    )
                )

            st.dataframe(
                display_orders,
                use_container_width=True,
                hide_index=True,
            )


    with tab_trades:

        if trades.empty:

            st.info(
                "No closed trades have been synced."
            )

        else:

            display_trades = trades.copy()

            for column in [
                "entry_price",
                "exit_price",
                "pnl",
            ]:
                display_trades[column] = (
                    pd.to_numeric(
                        display_trades[column],
                        errors="coerce",
                    )
                    .map(
                        lambda x:
                        fmt_money(x)
                        if not pd.isna(x)
                        else "—"
                    )
                )

            st.dataframe(
                display_trades,
                use_container_width=True,
                hide_index=True,
            )


def _render_live_monitor_body(
    strategy_id,
    strategy,
    snapshot,
    history,
    positions,
    orders,
    trades,
    backtests,
):
    # Existing Strategy Monitor body. Invoked only from the live fragment.


    # =========================================================
    # HEADER
    # =========================================================

    show_execution = has_execution_deployment(
        strategy,
        snapshot=snapshot,
        history=history,
        positions=positions,
        orders=orders,
        trades=trades,
    )
    show_platform = strategy_has_platform_research(strategy_id)
    stage1_rows = stage1_backtests(backtests)
    smoke_rows = smoke_backtests(backtests)
    has_stage1 = stage1_rows is not None and not getattr(stage1_rows, "empty", True)
    has_smoke = smoke_rows is not None and not getattr(smoke_rows, "empty", True)

    header_left, header_right = st.columns([4, 1])

    with header_left:
        st.subheader(
            display_strategy_name(
                strategy_id,
                strategy.get("name") if str(strategy.get("name") or "") != str(strategy_id) else None,
            )
        )

        if show_execution:
            st.caption(
                f"{strategy['environment']}  •  "
                f"Research Project: {strategy.get('qc_research_project_name') or '—'}  •  "
                f"Execution Project: {strategy['strategy_id']}"
            )
        elif show_platform:
            st.caption("Platform research · paper/live not deployed")
        else:
            st.caption(
                f"{strategy['environment']}  •  "
                f"Research Project: {strategy.get('qc_research_project_name') or '—'}  •  "
                "paper/live not deployed"
            )

    with header_right:
        st.markdown(
            f"### {status_badge(strategy['status'])}"
        )


    # =========================================================
    # PLATFORM RESEARCH (primary when research_kind=platform_research)
    # =========================================================

    if show_platform:
        render_platform_section(
            strategy_id,
            engine=engine,
        )


    # =========================================================
    # STAGE 2 ML RESEARCH (read-only PostgreSQL)
    # =========================================================

    render_stage2_section(
        strategy_id,
        backtests,
        engine=engine,
    )


    # =========================================================
    # STAGE 1 VALIDATION (only when Stage 1 rows exist)
    # =========================================================

    if has_stage1:
        render_stage1_section(
            strategy_id,
            backtests,
            load_equity=load_backtest_equity,
            load_run_row=load_research_run,
            strategy_row=strategy,
        )


    # =========================================================
    # SMOKE TESTS (hidden unless rows exist; collapsed for platform)
    # =========================================================

    if has_smoke:
        if show_platform:
            with st.expander("Smoke Tests", expanded=False):
                render_smoke_section(
                    backtests,
                    load_equity=load_backtest_equity,
                )
        else:
            render_smoke_section(
                backtests,
                load_equity=load_backtest_equity,
            )


    # =========================================================
    # STRATEGY RULESET (legacy only; platform uses StrategySpec)
    # =========================================================

    if not show_platform:
        st.markdown("### Strategy Rules")

        rules = parse_rules(
            strategy["rules_json"]
        )

        if not rules:

            st.info(
                "No structured rules stored for this strategy."
            )

        else:

            for key, value in rules.items():

                col_rule, col_value = st.columns(
                    [1, 3]
                )

                with col_rule:
                    st.markdown(
                        f"**{key.replace('_', ' ').title()}**"
                    )

                with col_value:
                    st.write(value)


    if show_execution:
        _render_paper_and_execution(
            snapshot,
            history,
            positions,
            orders,
            trades,
            backtests,
        )
    else:
        with st.expander("Paper / live (not deployed)", expanded=False):
            st.caption(
                "Paper, live, IBKR/orders, and execution stay hidden until a human "
                "authorizes those gates. Research results above do not require promotion."
            )
            st.info("No live paper snapshot available yet.")


    # =========================================================
    # TECHNICAL / DEPLOYMENT DETAILS
    # =========================================================

    with st.expander(
        "Strategy Metadata"
    ):

        metadata = {
            "Strategy ID":
                strategy["strategy_id"],

            "Environment":
                strategy["environment"],

            "QuantConnect Execution Project ID":
                strategy["qc_project_id"],

            "QuantConnect Research Project":
                strategy.get("qc_research_project_name") or "—",

            "QuantConnect Research Project ID":
                strategy.get("qc_research_project_id") or "not initialized",

            "QuantConnect Deployment ID":
                strategy["qc_deployment_id"],

            "Git Commit":
                strategy["git_commit"],

            "Status":
                strategy["status"],

            "Created":
                strategy["created_at"],

            "Updated":
                strategy["updated_at"],
        }

        st.json(metadata)


# =========================================================
# STRATEGY SELECTOR (outside the auto-refresh fragment)
# =========================================================

strategies = load_strategies()

if strategies.empty:
    st.warning("No strategies are registered.")
    st.stop()

library = load_research_library(engine)
filter_col, asset_col, status_col, smoke_col, auto_col = st.columns([2, 2, 2, 2, 1])
with filter_col:
    scope = st.radio(
        "Show",
        ["All", "Research", "Paper", "Live"],
        horizontal=True,
        key="strategy_monitor_scope",
    )
asset_options = ["All"]
status_options = ["All", "Complete", "Incomplete", "Failed"]
if library is not None and not library.empty:
    asset_options.extend(
        sorted(
            {
                str(value)
                for value in library["asset_class"].dropna()
                if str(value) and str(value) != UNAVAILABLE
            }
        )
    )
with asset_col:
    asset_filter = st.selectbox("Asset class", asset_options, key="strategy_monitor_asset_class")
with status_col:
    status_filter = st.selectbox("Research status", status_options, key="strategy_monitor_research_status")
with smoke_col:
    include_smoke = st.checkbox(
        "Include smoke tests",
        value=False,
        key="strategy_monitor_include_smoke",
        help="Smoke tests stay out of the default summary. They remain accessible here.",
    )
with auto_col:
    st.checkbox(
        "Auto refresh",
        value=True,
        key="strategy_monitor_auto_refresh",
        help="Update live monitor data every 30 seconds without reloading the page.",
    )

visible_library = filter_library(
    library,
    asset_class=asset_filter,
    research_status=status_filter,
    include_smoke=include_smoke,
)
display = library_display_frame(visible_library, include_smoke=include_smoke)
if display is not None and not display.empty:
    st.subheader("Research library")
    st.caption("Default run is the latest completed eligible non-holdout result — not the highest-performing run. Failed research stays visible. Completed is not approved.")
    st.dataframe(arrow_safe_frame(display), use_container_width=True, hide_index=True)

visible = strategies.copy()
if scope != "All" and "environment" in visible.columns:
    env = visible["environment"].fillna("").astype(str).str.lower()
    if scope == "Research":
        kind = (
            visible["research_kind"].fillna("").astype(str)
            if "research_kind" in visible.columns
            else pd.Series([""] * len(visible), index=visible.index)
        )
        visible = visible[env.eq("research") | kind.eq("platform_research")]
    else:
        visible = visible[env.eq(scope.lower())]
if visible_library is not None and not visible_library.empty:
    allowed = set(visible_library["strategy_id"].astype(str))
    scoped = visible[visible["strategy_id"].astype(str).isin(allowed)]
    if not scoped.empty:
        visible = scoped
if visible.empty:
    visible = strategies
labels = {
    str(row["strategy_id"]): picker_label(row)
    for _, row in visible.iterrows()
}
selected_id = st.selectbox(
    "Strategy",
    list(labels.keys()),
    format_func=lambda sid: labels.get(sid, sid),
    key="strategy_monitor_selected_strategy",
)
previous = st.session_state.get("strategy_monitor_last_strategy")
if previous is not None and str(previous) != str(selected_id):
    for stale_key in (
        "strategy_monitor_platform_research_run",
        "strategy_monitor_stage2_research_run",
        "strategy_monitor_research_run",
        "strategy_monitor_experiment_backtest",
        "stage1_equity_select",
        "smoke_test_select",
    ):
        st.session_state.pop(stale_key, None)
st.session_state["strategy_monitor_last_strategy"] = selected_id
runs = load_strategy_runs(engine, selected_id)
if runs is not None and not runs.empty and len(runs) > 1:
    run_labels = []
    for _, run in runs.iterrows():
        run_id = str(run.get("research_run_id") or "")
        stamp = str(run.get("last_seen_at") or "")[:19]
        holdout = str(run.get("holdout_status") or "")
        status = str(run.get("run_status") or "")
        run_labels.append("{0} · {1} · {2}{3}".format(run_id, status or "unknown", stamp or "no timestamp", " · holdout" if holdout == "ACCESSED" else ""))
    st.selectbox(
        "Run / version",
        list(runs["research_run_id"].astype(str)),
        format_func=lambda rid: next((label for label, value in zip(run_labels, runs["research_run_id"].astype(str)) if str(value) == str(rid)), rid),
        key="strategy_monitor_run_version",
        help="Defaults stay with the latest completed eligible non-holdout run in each research family. This list does not rank by performance.",
    )

strategy = visible[visible["strategy_id"].astype(str) == str(selected_id)].iloc[0]
strategy_id = strategy["strategy_id"]

if st.session_state["strategy_monitor_auto_refresh"]:
    render_live_strategy_monitor_auto(strategy_id, strategy)
else:
    render_live_strategy_monitor_manual(strategy_id, strategy)
