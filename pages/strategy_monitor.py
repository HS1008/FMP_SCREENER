import json

import pandas as pd
import streamlit as st
from sqlalchemy import text

from db.connection import engine


st.set_page_config(
    page_title="Strategy Monitor",
    page_icon="📈",
    layout="wide",
)

st.title("Strategy Monitor")
st.caption(
    "QuantConnect strategy status, paper performance, "
    "positions, execution, and backtest monitoring."
)


# =========================================================
# DATABASE LOADERS
# =========================================================

def load_strategies():
    return pd.read_sql(
        """
        SELECT
            strategy_id,
            name,
            environment,
            status,
            qc_project_id,
            qc_deployment_id,
            git_commit,
            rules_json,
            created_at,
            updated_at
        FROM strategies
        ORDER BY name
        """,
        engine,
    )


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


# =========================================================
# STRATEGY SELECTOR
# =========================================================

strategies = load_strategies()

if strategies.empty:
    st.warning("No strategies are registered.")
    st.stop()

selected_name = st.selectbox(
    "Strategy",
    strategies["name"].tolist(),
)

strategy = strategies[
    strategies["name"] == selected_name
].iloc[0]

strategy_id = strategy["strategy_id"]

snapshot = load_latest_snapshot(strategy_id)
history = load_equity_history(strategy_id)
positions = load_latest_positions(strategy_id)
orders = load_orders(strategy_id)
trades = load_trades(strategy_id)
backtests = load_backtests(strategy_id)


# =========================================================
# HEADER
# =========================================================

header_left, header_right = st.columns([4, 1])

with header_left:
    st.subheader(strategy["name"])

    st.caption(
        f"{strategy['environment']}  •  "
        f"QC Project {strategy['qc_project_id']}  •  "
        f"Deployment {strategy['qc_deployment_id']}"
    )

with header_right:
    st.markdown(
        f"### {status_badge(strategy['status'])}"
    )


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
# STRATEGY RULESET
# =========================================================

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


# =========================================================
# BACKTEST VS PAPER
# =========================================================

st.markdown("### Backtest vs Paper")

if backtests.empty:

    st.info(
        "No QuantConnect backtests have been synced yet."
    )

else:

    latest_backtest = backtests.iloc[0]

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
                fmt_pct(
                    latest_backtest["cagr"]
                ),
                fmt_num(
                    latest_backtest["sharpe_ratio"]
                ),
                fmt_num(
                    latest_backtest["sortino_ratio"]
                ),
                fmt_pct(
                    latest_backtest[
                        "max_drawdown"
                    ]
                ),
                fmt_pct(
                    latest_backtest[
                        "net_profit"
                    ]
                ),
                fmt_num(
                    latest_backtest["alpha"]
                ),
                fmt_num(
                    latest_backtest["beta"]
                ),
                fmt_pct(
                    latest_backtest["win_rate"]
                ),
                latest_backtest[
                    "trade_count"
                ],
            ],
            "Paper": [
                "—",
                "—",
                "—",
                (
                    fmt_pct(
                        snapshot["drawdown"]
                    )
                    if snapshot
                    else "—"
                ),
                (
                    fmt_pct(
                        snapshot["total_return"]
                    )
                    if snapshot
                    else "—"
                ),
                "—",
                "—",
                "—",
                len(trades),
            ],
        }
    )

    st.caption(
        f"Latest backtest: "
        f"{latest_backtest['name']}"
    )

    st.dataframe(
        comparison,
        use_container_width=True,
        hide_index=True,
    )

    with st.expander(
        "Backtest History"
    ):

        st.dataframe(
            backtests,
            use_container_width=True,
            hide_index=True,
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

        "QuantConnect Project ID":
            strategy["qc_project_id"],

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
