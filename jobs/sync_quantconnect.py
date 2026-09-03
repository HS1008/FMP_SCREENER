import argparse
import os
from base64 import b64encode
from hashlib import sha256
from time import time

import requests
from dotenv import load_dotenv
from sqlalchemy import text

from db.connection import engine
from jobs.stage1_backtests import (
    audit_holdout_exposures,
    discover_run_summary_paths,
    existing_backtest_map,
    equity_point_counts,
    import_run_summaries,
    insert_equity_points,
    json_param,
    legacy_hydration_fields,
    list_metrics_from_summary,
    merge_stage1_lightweight_metrics,
    needs_detail_read,
    needs_equity_curve,
    needs_legacy_date_hydration,
    refresh_research_run_progress,
    stage1_upsert_fields,
    upsert_research_run,
)
from qc_research.dates import chart_request_window
from qc_research.parsing import is_stage1_name, parse_equity_chart


# Shared lock for the one-minute backtests-only cron and production
# verification. Live-only sync must not take this lock.
BACKTEST_SYNC_LOCK_RELATIVE = "outputs/backtest_sync.flock"
BACKTEST_SYNC_LOCK_WAIT_SECONDS = 180

# =========================================================
# CONFIG
# =========================================================

load_dotenv()

QC_USER_ID = os.getenv("QC_USER_ID")
QC_API_TOKEN = os.getenv("QC_API_TOKEN")

BASE_URL = "https://www.quantconnect.com/api/v2"


def require_credentials():
    if not QC_USER_ID or not QC_API_TOKEN:
        raise RuntimeError(
            "Missing QuantConnect credentials. "
            "Add QC_USER_ID and QC_API_TOKEN to .env"
        )


# =========================================================
# ENUM MAPS
# =========================================================

ORDER_TYPES = {
    0: "Market",
    1: "Limit",
    2: "StopMarket",
    3: "StopLimit",
    4: "MarketOnOpen",
    5: "MarketOnClose",
    6: "OptionExercise",
    7: "LimitIfTouched",
    8: "ComboMarket",
    9: "ComboLimit",
    10: "ComboLegLimit",
    11: "TrailingStop",
}

ORDER_STATUSES = {
    0: "New",
    1: "Submitted",
    2: "PartiallyFilled",
    3: "Filled",
    5: "Canceled",
    6: "None",
    7: "Invalid",
    8: "CancelPending",
    9: "UpdateSubmitted",
}

ORDER_DIRECTIONS = {
    0: "Buy",
    1: "Sell",
    2: "Hold",
}


# =========================================================
# AUTH
# =========================================================

def get_headers():
    require_credentials()
    timestamp = str(int(time()))

    token = f"{QC_API_TOKEN}:{timestamp}".encode("utf-8")
    hashed_token = sha256(token).hexdigest()

    auth_string = f"{QC_USER_ID}:{hashed_token}".encode("utf-8")
    encoded_auth = b64encode(auth_string).decode("ascii")

    return {
        "Authorization": f"Basic {encoded_auth}",
        "Timestamp": timestamp,
        "Content-Type": "application/json",
    }


# =========================================================
# DATABASE SCHEMA
# =========================================================

def ensure_schema():
    with engine.begin() as conn:

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS backtests (
                id BIGSERIAL PRIMARY KEY,
                backtest_id VARCHAR(100) UNIQUE NOT NULL,
                strategy_id VARCHAR(100) NOT NULL,
                qc_project_id VARCHAR(100),
                name VARCHAR(255),
                status VARCHAR(50),
                created_at TIMESTAMPTZ,
                sharpe_ratio NUMERIC,
                sortino_ratio NUMERIC,
                alpha NUMERIC,
                beta NUMERIC,
                cagr NUMERIC,
                max_drawdown NUMERIC,
                net_profit NUMERIC,
                win_rate NUMERIC,
                loss_rate NUMERIC,
                trade_count INTEGER,
                psr NUMERIC,
                synced_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_live_snapshots_strategy_time
            ON live_snapshots(strategy_id, timestamp)
        """))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_positions_strategy_time
            ON positions(strategy_id, timestamp)
        """))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_orders_strategy
            ON orders(strategy_id)
        """))

        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_trades_strategy
            ON trades(strategy_id)
        """))


# =========================================================
# STRATEGY REGISTRY
# =========================================================

def get_strategies():
    with engine.connect() as conn:
        return conn.execute(
            text("""
                SELECT
                    strategy_id,
                    name,
                    qc_project_id,
                    qc_deployment_id,
                    qc_research_project_id,
                    qc_research_project_name
                FROM strategies
                WHERE qc_project_id IS NOT NULL
                ORDER BY strategy_id
            """)
        ).mappings().all()


def update_strategy_status(
    strategy_id,
    status,
    deployment_id=None,
):
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE strategies
                SET
                    status = :status,
                    qc_deployment_id =
                        COALESCE(:deployment_id, qc_deployment_id),
                    updated_at = NOW()
                WHERE strategy_id = :strategy_id
            """),
            {
                "strategy_id": strategy_id,
                "status": status,
                "deployment_id": deployment_id,
            },
        )


# =========================================================
# API HELPERS
# =========================================================

def qc_post(endpoint, payload):
    response = requests.post(
        f"{BASE_URL}{endpoint}",
        headers=get_headers(),
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("success", False):
        raise RuntimeError(
            f"{endpoint} failed: "
            f"{result.get('errors') or result.get('message')}"
        )

    return result


# =========================================================
# LIVE STATUS
# =========================================================

def get_live_status(project_id):
    return qc_post(
        "/live/read",
        {
            "projectId": int(project_id)
        },
    )


# =========================================================
# PORTFOLIO
# =========================================================

def get_live_portfolio(project_id):
    return qc_post(
        "/live/portfolio/read",
        {
            "projectId": int(project_id)
        },
    )


def _safe_float(value, default=0.0):
    if value is None:
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_number(data, keys, default=0.0):
    if not isinstance(data, dict):
        return default

    for key in keys:
        if key not in data:
            continue

        value = data.get(key)

        if value is None:
            continue

        try:
            return float(value)
        except (TypeError, ValueError):
            continue

    return default


def _extract_portfolio(result):
    if not isinstance(result, dict):
        return {}

    portfolio = result.get("portfolio")

    if isinstance(portfolio, dict):
        return portfolio

    if "cash" in result or "holdings" in result:
        return result

    return {}


def _extract_holdings(portfolio):
    if not isinstance(portfolio, dict):
        return {}

    holdings = portfolio.get("holdings") or {}

    if isinstance(holdings, dict):
        return holdings

    if isinstance(holdings, list):
        extracted = {}

        for index, holding in enumerate(holdings):
            if not isinstance(holding, dict):
                continue

            symbol_obj = holding.get("symbol")

            if isinstance(symbol_obj, dict):
                key = (
                    symbol_obj.get("value")
                    or symbol_obj.get("symbol")
                    or f"holding-{index}"
                )
            elif symbol_obj:
                key = str(symbol_obj)
            else:
                key = (
                    holding.get("s")
                    or holding.get("ticker")
                    or f"holding-{index}"
                )

            extracted[str(key)] = holding

        return extracted

    return {}


def _holding_fields_recognized(holding):
    if not isinstance(holding, dict):
        return False

    return any(
        key in holding
        for key in (
            "q",
            "p",
            "v",
            "quantity",
            "price",
            "marketPrice",
            "marketValue",
        )
    )


def _infer_symbol(symbol_key, holding):
    if isinstance(symbol_key, str) and symbol_key.strip():
        return symbol_key.strip().split()[0]

    if isinstance(holding, dict):
        symbol_obj = holding.get("symbol")

        if isinstance(symbol_obj, dict):
            value = (
                symbol_obj.get("value")
                or symbol_obj.get("ticker")
                or symbol_obj.get("symbol")
            )

            if isinstance(value, str) and value.strip():
                return value.strip().split()[0]

        elif isinstance(symbol_obj, str) and symbol_obj.strip():
            return symbol_obj.strip().split()[0]

        for field in ("s", "ticker", "symbolValue"):
            value = holding.get(field)

            if isinstance(value, str) and value.strip():
                return value.strip().split()[0]

    if symbol_key:
        return str(symbol_key).split()[0]

    return "UNKNOWN"


def parse_portfolio(result):
    """Parse a QuantConnect live portfolio/read payload.

    Live holdings use compact fields:
      q = quantity, p = price, v = market value
    Cash uses valueInAccountCurrency on each currency entry.
    """

    empty = {
        "cash": 0.0,
        "holdings_value": 0.0,
        "equity": 0.0,
        "positions": [],
    }

    if not isinstance(result, dict):
        return empty

    portfolio = _extract_portfolio(result)
    cash_data = portfolio.get("cash") or {}
    holdings = _extract_holdings(portfolio)

    total_cash = 0.0

    if isinstance(cash_data, dict):
        for currency in cash_data.values():
            if isinstance(currency, dict):
                total_cash += _safe_float(
                    currency.get("valueInAccountCurrency")
                )
            else:
                total_cash += _safe_float(currency)

    else:
        total_cash = _safe_float(cash_data)

    positions = []
    holdings_value = 0.0

    for symbol_key, holding in holdings.items():

        if not isinstance(holding, dict):
            continue

        symbol = _infer_symbol(symbol_key, holding)

        quantity = _first_number(
            holding,
            ("q", "quantity"),
        )

        price = _first_number(
            holding,
            ("p", "price", "marketPrice"),
        )

        if any(
            holding.get(key) is not None
            for key in ("v", "marketValue")
        ):
            market_value = _first_number(
                holding,
                ("v", "marketValue"),
            )
        else:
            market_value = quantity * price

        holdings_value += market_value

        if abs(quantity) < 1e-12 and abs(market_value) < 1e-6:
            continue

        positions.append({
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "market_value": market_value,
        })

    equity = total_cash + holdings_value

    for position in positions:
        position["weight"] = (
            position["market_value"] / equity
            if equity
            else 0.0
        )

    return {
        "cash": total_cash,
        "holdings_value": holdings_value,
        "equity": equity,
        "positions": positions,
    }


def snapshot_is_malformed(portfolio, result):
    """Return a warning message if this snapshot should not be inserted."""

    if not isinstance(portfolio, dict):
        return "portfolio parser returned a non-dict payload"

    equity = _safe_float(portfolio.get("equity"))
    cash = _safe_float(portfolio.get("cash"))
    holdings_value = _safe_float(
        portfolio.get("holdings_value")
    )

    if equity <= 0:
        return (
            f"skipping live snapshot: equity is {equity:.4f} (<= 0)"
        )

    raw_holdings = _extract_holdings(
        _extract_portfolio(result)
    )

    if (
        cash > 0
        and raw_holdings
        and abs(holdings_value) < 1e-9
    ):
        recognized = any(
            _holding_fields_recognized(holding)
            for holding in raw_holdings.values()
        )

        if not recognized:
            return (
                "skipping live snapshot: cash is positive and the "
                "QuantConnect holdings payload is non-empty, but "
                "parsed holdings_value is 0 (unrecognized holding "
                "fields; likely a parser error)"
            )

    return None


# =========================================================
# LIVE SNAPSHOTS
# =========================================================

def _historical_snapshot_valid(row, peak_equity):
    equity = _safe_float(row.get("equity"), default=None)

    if equity is None or equity <= 0:
        return False

    cash = _safe_float(row.get("cash"))
    holdings_value = _safe_float(row.get("holdings_value"))

    cash_only = (
        abs(holdings_value) < 1e-6
        and cash > 0
        and abs(equity - cash) <= max(1.0, 0.02 * abs(equity))
    )

    if (
        cash_only
        and peak_equity
        and equity < 0.5 * peak_equity
    ):
        return False

    return True


def load_snapshot_equity_rows(strategy_id):
    with engine.connect() as conn:
        return conn.execute(
            text("""
                SELECT
                    timestamp,
                    equity,
                    cash,
                    holdings_value
                FROM live_snapshots
                WHERE strategy_id = :strategy_id
                ORDER BY timestamp ASC
            """),
            {"strategy_id": strategy_id},
        ).mappings().all()


def get_previous_peak(strategy_id, incoming_equity=None):
    rows = load_snapshot_equity_rows(strategy_id)

    candidates = [
        _safe_float(row["equity"])
        for row in rows
        if row["equity"] is not None
        and _safe_float(row["equity"]) > 0
    ]

    if incoming_equity is not None and incoming_equity > 0:
        candidates.append(float(incoming_equity))

    raw_peak = max(candidates) if candidates else None

    valid_equities = [
        _safe_float(row["equity"])
        for row in rows
        if _historical_snapshot_valid(row, raw_peak)
    ]

    if incoming_equity is not None and incoming_equity > 0:
        valid_equities.append(float(incoming_equity))

    if not valid_equities:
        return None

    return max(valid_equities)


def get_first_equity(strategy_id, incoming_equity=None):
    rows = load_snapshot_equity_rows(strategy_id)

    candidates = [
        _safe_float(row["equity"])
        for row in rows
        if row["equity"] is not None
        and _safe_float(row["equity"]) > 0
    ]

    if incoming_equity is not None and incoming_equity > 0:
        candidates.append(float(incoming_equity))

    raw_peak = max(candidates) if candidates else None

    for row in rows:
        if _historical_snapshot_valid(row, raw_peak):
            return _safe_float(row["equity"])

    if incoming_equity is not None and incoming_equity > 0:
        return float(incoming_equity)

    return None


def insert_live_snapshot(
    strategy_id,
    status,
    portfolio,
):
    equity = _safe_float(portfolio["equity"])

    if equity <= 0:
        print(
            "WARNING: skipping live snapshot insert: "
            f"equity is {equity:.4f} (<= 0)"
        )
        return False

    first_equity = get_first_equity(
        strategy_id,
        incoming_equity=equity,
    )
    previous_peak = get_previous_peak(
        strategy_id,
        incoming_equity=equity,
    )

    total_return = None
    drawdown = None

    if first_equity:
        total_return = equity / first_equity - 1

    peak = max(
        equity,
        previous_peak or equity,
    )

    if peak:
        drawdown = equity / peak - 1

    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO live_snapshots (
                    strategy_id,
                    timestamp,
                    equity,
                    cash,
                    holdings_value,
                    total_return,
                    drawdown,
                    status
                )
                VALUES (
                    :strategy_id,
                    NOW(),
                    :equity,
                    :cash,
                    :holdings_value,
                    :total_return,
                    :drawdown,
                    :status
                )
            """),
            {
                "strategy_id": strategy_id,
                "equity": equity,
                "cash": portfolio["cash"],
                "holdings_value":
                    portfolio["holdings_value"],
                "total_return": total_return,
                "drawdown": drawdown,
                "status": status,
            },
        )

    return True


# =========================================================
# POSITIONS
# =========================================================

def insert_positions(
    strategy_id,
    positions,
):
    if not positions:
        return

    with engine.begin() as conn:
        for position in positions:
            conn.execute(
                text("""
                    INSERT INTO positions (
                        strategy_id,
                        timestamp,
                        symbol,
                        quantity,
                        price,
                        market_value,
                        weight
                    )
                    VALUES (
                        :strategy_id,
                        NOW(),
                        :symbol,
                        :quantity,
                        :price,
                        :market_value,
                        :weight
                    )
                """),
                {
                    "strategy_id": strategy_id,
                    **position,
                },
            )


# =========================================================
# LIVE ORDERS
# =========================================================

def get_live_orders(
    project_id,
    deployment_id,
):
    return qc_post(
        "/live/orders/read",
        {
            "projectId": int(project_id),
            "algorithmId": deployment_id,
            "start": 0,
            "end": 1000,
        },
    )


def extract_fill_price(order):
    events = order.get("events", []) or []

    filled_events = [
        event
        for event in events
        if float(event.get("fillQuantity", 0) or 0) != 0
    ]

    if not filled_events:
        return None

    return float(
        filled_events[-1].get("fillPrice", 0) or 0
    )


def sync_orders(
    strategy_id,
    orders_result,
):
    if orders_result.get("status") == "loading":
        return 0

    orders = orders_result.get("orders", []) or []

    with engine.begin() as conn:

        conn.execute(
            text("""
                DELETE FROM orders
                WHERE strategy_id = :strategy_id
            """),
            {"strategy_id": strategy_id},
        )

        for order in orders:

            symbol_obj = order.get("symbol", {})

            if isinstance(symbol_obj, dict):
                symbol = symbol_obj.get("value")
            else:
                symbol = str(symbol_obj)

            order_type = ORDER_TYPES.get(
                order.get("type"),
                str(order.get("type")),
            )

            status = ORDER_STATUSES.get(
                order.get("status"),
                str(order.get("status")),
            )

            direction = ORDER_DIRECTIONS.get(
                order.get("direction"),
                str(order.get("direction")),
            )

            conn.execute(
                text("""
                    INSERT INTO orders (
                        strategy_id,
                        qc_order_id,
                        timestamp,
                        symbol,
                        direction,
                        quantity,
                        order_type,
                        status,
                        fill_price
                    )
                    VALUES (
                        :strategy_id,
                        :qc_order_id,
                        :timestamp,
                        :symbol,
                        :direction,
                        :quantity,
                        :order_type,
                        :status,
                        :fill_price
                    )
                """),
                {
                    "strategy_id": strategy_id,
                    "qc_order_id": str(
                        order.get("id")
                    ),
                    "timestamp":
                        order.get("time")
                        or order.get("createdTime"),
                    "symbol": symbol,
                    "direction": direction,
                    "quantity":
                        order.get("quantity"),
                    "order_type": order_type,
                    "status": status,
                    "fill_price":
                        extract_fill_price(order),
                },
            )

    return len(orders)


# =========================================================
# CLOSED TRADES
# =========================================================

def get_live_trades(
    project_id,
    deployment_id,
):
    return qc_post(
        "/live/trades/read",
        {
            "projectId": int(project_id),
            "deployId": deployment_id,
            "start": 0,
            "end": 1000,
        },
    )


def sync_trades(
    strategy_id,
    trades_result,
):
    if trades_result.get("status") == "loading":
        return 0

    trades = trades_result.get("trades", []) or []

    with engine.begin() as conn:

        conn.execute(
            text("""
                DELETE FROM trades
                WHERE strategy_id = :strategy_id
            """),
            {"strategy_id": strategy_id},
        )

        for index, trade in enumerate(trades):

            symbol_obj = trade.get("symbol", {})

            if isinstance(symbol_obj, dict):
                symbol = symbol_obj.get("value")
            else:
                symbol = (
                    trade.get("symbolValue")
                    or str(symbol_obj)
                )

            trade_id = (
                trade.get("id")
                or trade.get("tradeId")
                or f"{strategy_id}-{index}"
            )

            conn.execute(
                text("""
                    INSERT INTO trades (
                        strategy_id,
                        trade_id,
                        symbol,
                        entry_time,
                        exit_time,
                        quantity,
                        entry_price,
                        exit_price,
                        pnl
                    )
                    VALUES (
                        :strategy_id,
                        :trade_id,
                        :symbol,
                        :entry_time,
                        :exit_time,
                        :quantity,
                        :entry_price,
                        :exit_price,
                        :pnl
                    )
                """),
                {
                    "strategy_id": strategy_id,
                    "trade_id": str(trade_id),
                    "symbol": symbol,
                    "entry_time":
                        trade.get("entryTime")
                        or trade.get("entryDateTime"),
                    "exit_time":
                        trade.get("exitTime")
                        or trade.get("exitDateTime"),
                    "quantity":
                        trade.get("quantity"),
                    "entry_price":
                        trade.get("entryPrice"),
                    "exit_price":
                        trade.get("exitPrice"),
                    "pnl":
                        trade.get("profitLoss")
                        or trade.get("pnl"),
                },
            )

    return len(trades)


# =========================================================
# RESEARCH vs EXECUTION PROJECTS
# =========================================================

RESEARCH_PROJECT_NOT_BOOTSTRAPPED = (
    "Dedicated research project {0} is not initialized. "
    "Run the research-project bootstrap first."
)


def list_qc_projects():
    result = qc_post("/projects/read", {})
    projects = result.get("projects") or result.get("project") or []
    if isinstance(projects, dict):
        return [projects]
    return list(projects)


def exact_project_match(projects, name):
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    matches = []
    for row in projects or []:
        if str(row.get("name") or "").strip().lower() == needle:
            matches.append(row)
    if len(matches) > 1:
        raise RuntimeError(
            "Ambiguous QuantConnect project name {0!r}".format(name)
        )
    return matches[0] if matches else None


def project_id_from_row(row):
    if not row:
        return None
    value = row.get("projectId") or row.get("id")
    if value is None:
        return None
    return str(value)


def store_research_project_id(strategy_id, project_id):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE strategies
                SET
                    qc_research_project_id = :project_id,
                    updated_at = NOW()
                WHERE strategy_id = :strategy_id
                  AND qc_research_project_id IS NULL
                """
            ),
            {
                "strategy_id": strategy_id,
                "project_id": str(project_id),
            },
        )


def resolve_research_project_id(
    strategy,
    projects=None,
    *,
    persist=True,
    fetch_projects=None,
):
    """Return the dedicated research project ID. Never falls back to execution."""
    stored = strategy.get("qc_research_project_id")
    if stored:
        return str(stored)
    name = str(strategy.get("qc_research_project_name") or "").strip()
    if not name:
        print(
            "No qc_research_project_name configured for {0}.".format(
                strategy.get("strategy_id")
            )
        )
        return None
    rows = projects
    if rows is None:
        getter = fetch_projects or list_qc_projects
        rows = getter()
    match = exact_project_match(rows, name)
    if match is None:
        print(RESEARCH_PROJECT_NOT_BOOTSTRAPPED.format(name))
        return None
    project_id = project_id_from_row(match)
    if persist and project_id and strategy.get("strategy_id"):
        store_research_project_id(strategy["strategy_id"], project_id)
    return project_id


def execution_project_id(strategy):
    value = strategy.get("qc_project_id")
    return str(value) if value else None


# =========================================================
# BACKTESTS
# =========================================================

def get_backtests(project_id):
    return qc_post(
        "/backtests/list",
        {
            "projectId": int(project_id),
            "includeStatistics": True,
        },
    )


def get_backtest_detail(project_id, backtest_id):
    return qc_post(
        "/backtests/read",
        {
            "projectId": int(project_id),
            "backtestId": str(backtest_id),
        },
    )


def get_backtest_chart(project_id, backtest_id, start=0, end=None, count=1000):
    payload = {
        "projectId": int(project_id),
        "backtestId": str(backtest_id),
        "name": "Strategy Equity",
        "count": int(count),
        "start": int(start or 0),
        "end": int(end or int(time())),
    }
    return qc_post("/backtests/chart/read", payload)


STAGE1_UPSERT_SQL = """
    INSERT INTO backtests (
        backtest_id,
        strategy_id,
        qc_project_id,
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
        synced_at,
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
    )
    VALUES (
        :backtest_id,
        :strategy_id,
        :qc_project_id,
        :name,
        :status,
        :created_at,
        :sharpe_ratio,
        :sortino_ratio,
        :alpha,
        :beta,
        :cagr,
        :max_drawdown,
        :net_profit,
        :win_rate,
        :loss_rate,
        :trade_count,
        :psr,
        NOW(),
        :research_suite_version,
        :research_run_id,
        :research_experiment_id,
        :research_test_type,
        :research_phase,
        :research_window_id,
        :research_git_commit,
        :research_is_holdout,
        :research_dirty,
        :train_start,
        :train_end,
        :test_start,
        :test_end,
        CAST(:parameters_json AS jsonb),
        :objective_name,
        :objective_value,
        CAST(:raw_statistics_json AS jsonb),
        CAST(:research_guide_json AS jsonb),
        CAST(:research_thresholds_json AS jsonb),
        :research_primary_parameter,
        CAST(:research_selection_summary_json AS jsonb),
        :research_lineage_id,
        :economic_parameter_count,
        :research_metadata_count,
        :backtest_start,
        :backtest_end,
        :error_message
    )
    ON CONFLICT (backtest_id)
    DO UPDATE SET
        name = EXCLUDED.name,
        status = EXCLUDED.status,
        sharpe_ratio = EXCLUDED.sharpe_ratio,
        sortino_ratio = EXCLUDED.sortino_ratio,
        alpha = EXCLUDED.alpha,
        beta = EXCLUDED.beta,
        cagr = EXCLUDED.cagr,
        max_drawdown = EXCLUDED.max_drawdown,
        net_profit = EXCLUDED.net_profit,
        win_rate = EXCLUDED.win_rate,
        loss_rate = EXCLUDED.loss_rate,
        trade_count = EXCLUDED.trade_count,
        psr = EXCLUDED.psr,
        synced_at = NOW(),
        research_suite_version = COALESCE(EXCLUDED.research_suite_version, backtests.research_suite_version),
        research_run_id = COALESCE(EXCLUDED.research_run_id, backtests.research_run_id),
        research_experiment_id = COALESCE(EXCLUDED.research_experiment_id, backtests.research_experiment_id),
        research_test_type = COALESCE(EXCLUDED.research_test_type, backtests.research_test_type),
        research_phase = COALESCE(EXCLUDED.research_phase, backtests.research_phase),
        research_window_id = COALESCE(EXCLUDED.research_window_id, backtests.research_window_id),
        research_git_commit = COALESCE(EXCLUDED.research_git_commit, backtests.research_git_commit),
        research_is_holdout = COALESCE(EXCLUDED.research_is_holdout, backtests.research_is_holdout),
        research_dirty = COALESCE(EXCLUDED.research_dirty, backtests.research_dirty),
        train_start = COALESCE(EXCLUDED.train_start, backtests.train_start),
        train_end = COALESCE(EXCLUDED.train_end, backtests.train_end),
        test_start = COALESCE(EXCLUDED.test_start, backtests.test_start),
        test_end = COALESCE(EXCLUDED.test_end, backtests.test_end),
        parameters_json = COALESCE(EXCLUDED.parameters_json, backtests.parameters_json),
        objective_name = COALESCE(EXCLUDED.objective_name, backtests.objective_name),
        objective_value = COALESCE(EXCLUDED.objective_value, backtests.objective_value),
        raw_statistics_json = COALESCE(EXCLUDED.raw_statistics_json, backtests.raw_statistics_json),
        research_guide_json = COALESCE(EXCLUDED.research_guide_json, backtests.research_guide_json),
        research_thresholds_json = COALESCE(EXCLUDED.research_thresholds_json, backtests.research_thresholds_json),
        research_primary_parameter = COALESCE(EXCLUDED.research_primary_parameter, backtests.research_primary_parameter),
        research_selection_summary_json = COALESCE(EXCLUDED.research_selection_summary_json, backtests.research_selection_summary_json),
        research_lineage_id = COALESCE(EXCLUDED.research_lineage_id, backtests.research_lineage_id),
        economic_parameter_count = COALESCE(EXCLUDED.economic_parameter_count, backtests.economic_parameter_count),
        research_metadata_count = COALESCE(EXCLUDED.research_metadata_count, backtests.research_metadata_count),
        backtest_start = COALESCE(EXCLUDED.backtest_start, backtests.backtest_start),
        backtest_end = COALESCE(EXCLUDED.backtest_end, backtests.backtest_end),
        error_message = COALESCE(EXCLUDED.error_message, backtests.error_message)
"""


STAGE1_LIGHTWEIGHT_UPSERT_SQL = """
    INSERT INTO backtests (
        backtest_id,
        strategy_id,
        qc_project_id,
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
        synced_at
    )
    VALUES (
        :backtest_id,
        :strategy_id,
        :qc_project_id,
        :name,
        :status,
        :created_at,
        :sharpe_ratio,
        :sortino_ratio,
        :alpha,
        :beta,
        :cagr,
        :max_drawdown,
        :net_profit,
        :win_rate,
        :loss_rate,
        :trade_count,
        :psr,
        NOW()
    )
    ON CONFLICT (backtest_id)
    DO UPDATE SET
        name = EXCLUDED.name,
        status = EXCLUDED.status,
        sharpe_ratio = COALESCE(EXCLUDED.sharpe_ratio, backtests.sharpe_ratio),
        sortino_ratio = COALESCE(EXCLUDED.sortino_ratio, backtests.sortino_ratio),
        alpha = COALESCE(EXCLUDED.alpha, backtests.alpha),
        beta = COALESCE(EXCLUDED.beta, backtests.beta),
        cagr = COALESCE(EXCLUDED.cagr, backtests.cagr),
        max_drawdown = COALESCE(EXCLUDED.max_drawdown, backtests.max_drawdown),
        net_profit = COALESCE(EXCLUDED.net_profit, backtests.net_profit),
        win_rate = COALESCE(EXCLUDED.win_rate, backtests.win_rate),
        loss_rate = COALESCE(EXCLUDED.loss_rate, backtests.loss_rate),
        trade_count = COALESCE(EXCLUDED.trade_count, backtests.trade_count),
        psr = COALESCE(EXCLUDED.psr, backtests.psr),
        synced_at = NOW()
"""


LEGACY_UPSERT_SQL = """
    INSERT INTO backtests (
        backtest_id,
        strategy_id,
        qc_project_id,
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
        synced_at
    )
    VALUES (
        :backtest_id,
        :strategy_id,
        :qc_project_id,
        :name,
        :status,
        :created_at,
        :sharpe_ratio,
        :sortino_ratio,
        :alpha,
        :beta,
        :cagr,
        :max_drawdown,
        :net_profit,
        :win_rate,
        :loss_rate,
        :trade_count,
        :psr,
        NOW()
    )
    ON CONFLICT (backtest_id)
    DO UPDATE SET
        name = EXCLUDED.name,
        status = EXCLUDED.status,
        sharpe_ratio = EXCLUDED.sharpe_ratio,
        sortino_ratio = EXCLUDED.sortino_ratio,
        alpha = EXCLUDED.alpha,
        beta = EXCLUDED.beta,
        cagr = EXCLUDED.cagr,
        max_drawdown = EXCLUDED.max_drawdown,
        net_profit = EXCLUDED.net_profit,
        win_rate = EXCLUDED.win_rate,
        loss_rate = EXCLUDED.loss_rate,
        trade_count = EXCLUDED.trade_count,
        psr = EXCLUDED.psr,
        synced_at = NOW()
"""


LEGACY_DATE_UPSERT_SQL = """
    INSERT INTO backtests (
        backtest_id,
        strategy_id,
        qc_project_id,
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
        synced_at,
        backtest_start,
        backtest_end,
        parameters_json
    )
    VALUES (
        :backtest_id,
        :strategy_id,
        :qc_project_id,
        :name,
        :status,
        :created_at,
        :sharpe_ratio,
        :sortino_ratio,
        :alpha,
        :beta,
        :cagr,
        :max_drawdown,
        :net_profit,
        :win_rate,
        :loss_rate,
        :trade_count,
        :psr,
        NOW(),
        :backtest_start,
        :backtest_end,
        CAST(:parameters_json AS jsonb)
    )
    ON CONFLICT (backtest_id)
    DO UPDATE SET
        name = EXCLUDED.name,
        status = EXCLUDED.status,
        sharpe_ratio = EXCLUDED.sharpe_ratio,
        sortino_ratio = EXCLUDED.sortino_ratio,
        alpha = EXCLUDED.alpha,
        beta = EXCLUDED.beta,
        cagr = EXCLUDED.cagr,
        max_drawdown = EXCLUDED.max_drawdown,
        net_profit = EXCLUDED.net_profit,
        win_rate = EXCLUDED.win_rate,
        loss_rate = EXCLUDED.loss_rate,
        trade_count = EXCLUDED.trade_count,
        psr = EXCLUDED.psr,
        synced_at = NOW(),
        backtest_start = COALESCE(EXCLUDED.backtest_start, backtests.backtest_start),
        backtest_end = COALESCE(EXCLUDED.backtest_end, backtests.backtest_end),
        parameters_json = COALESCE(EXCLUDED.parameters_json, backtests.parameters_json)
"""


def _legacy_metric_payload(backtest):
    metrics = list_metrics_from_summary(backtest)
    return {
        "sharpe_ratio": metrics.get("sharpe_ratio") if metrics.get("sharpe_ratio") is not None else backtest.get("sharpeRatio"),
        "sortino_ratio": metrics.get("sortino_ratio") if metrics.get("sortino_ratio") is not None else backtest.get("sortinoRatio"),
        "alpha": metrics.get("alpha") if metrics.get("alpha") is not None else backtest.get("alpha"),
        "beta": metrics.get("beta") if metrics.get("beta") is not None else backtest.get("beta"),
        "cagr": metrics.get("cagr") if metrics.get("cagr") is not None else backtest.get("compoundingAnnualReturn"),
        "max_drawdown": metrics.get("max_drawdown") if metrics.get("max_drawdown") is not None else backtest.get("drawdown"),
        "net_profit": metrics.get("net_profit") if metrics.get("net_profit") is not None else backtest.get("netProfit"),
        "win_rate": metrics.get("win_rate") if metrics.get("win_rate") is not None else backtest.get("winRate"),
        "loss_rate": metrics.get("loss_rate") if metrics.get("loss_rate") is not None else backtest.get("lossRate"),
        "trade_count": metrics.get("trade_count") if metrics.get("trade_count") is not None else backtest.get("trades"),
        "psr": metrics.get("psr") if metrics.get("psr") is not None else backtest.get("psr"),
    }


def sync_backtests(
    strategy_id,
    project_id,
    result,
):
    backtests = result.get("backtests", []) or []
    detail_reads = 0
    chart_reads = 0

    with engine.begin() as conn:
        existing = existing_backtest_map(conn, strategy_id)
        equity_counts = equity_point_counts(conn, strategy_id)

        for backtest in backtests:
            backtest_id = backtest.get("backtestId")
            if not backtest_id:
                continue
            name = backtest.get("name")
            row_existing = existing.get(backtest_id)
            if is_stage1_name(name):
                metrics = list_metrics_from_summary(backtest)
            else:
                metrics = _legacy_metric_payload(backtest)
            base = {
                "backtest_id": backtest_id,
                "strategy_id": strategy_id,
                "qc_project_id": str(project_id),
                "name": name,
                "status": backtest.get("status"),
                "created_at": backtest.get("created"),
                **metrics,
            }

            fetch_detail = needs_detail_read(row_existing, backtest)
            point_count = equity_counts.get(backtest_id, 0)
            fetch_chart = needs_equity_curve(row_existing, backtest, point_count)
            has_dates = bool(
                row_existing
                and (row_existing.get("backtest_start") or row_existing.get("backtest_end"))
            )
            if fetch_chart and not has_dates and not fetch_detail and is_stage1_name(name):
                fetch_detail = True

            detail = None
            if fetch_detail:
                try:
                    detail_result = get_backtest_detail(project_id, backtest_id)
                    detail_reads += 1
                    detail = detail_result.get("backtest") or detail_result
                    fields = stage1_upsert_fields(detail, name)
                    payload = {
                        **base,
                        "sharpe_ratio": fields.get("sharpe_ratio"),
                        "sortino_ratio": fields.get("sortino_ratio"),
                        "alpha": fields.get("alpha"),
                        "beta": fields.get("beta"),
                        "cagr": fields.get("cagr"),
                        "max_drawdown": fields.get("max_drawdown"),
                        "net_profit": fields.get("net_profit"),
                        "win_rate": fields.get("win_rate"),
                        "loss_rate": fields.get("loss_rate"),
                        "trade_count": fields.get("trade_count"),
                        "psr": fields.get("psr"),
                        "research_suite_version": fields.get("research_suite_version"),
                        "research_run_id": fields.get("research_run_id"),
                        "research_experiment_id": fields.get("research_experiment_id"),
                        "research_test_type": fields.get("research_test_type"),
                        "research_phase": fields.get("research_phase"),
                        "research_window_id": fields.get("research_window_id"),
                        "research_git_commit": fields.get("research_git_commit"),
                        "research_is_holdout": fields.get("research_is_holdout"),
                        "research_dirty": fields.get("research_dirty"),
                        "train_start": fields.get("train_start"),
                        "train_end": fields.get("train_end"),
                        "test_start": fields.get("test_start"),
                        "test_end": fields.get("test_end"),
                        "parameters_json": json_param(fields.get("parameters_json")),
                        "objective_name": fields.get("objective_name"),
                        "objective_value": fields.get("objective_value"),
                        "raw_statistics_json": json_param(fields.get("raw_statistics_json")),
                        "research_guide_json": json_param(fields.get("research_guide_json")),
                        "research_thresholds_json": json_param(fields.get("research_thresholds_json")),
                        "research_primary_parameter": fields.get("research_primary_parameter"),
                        "research_selection_summary_json": json_param(
                            fields.get("research_selection_summary_json")
                        ),
                        "research_lineage_id": fields.get("research_lineage_id"),
                        "economic_parameter_count": fields.get("economic_parameter_count"),
                        "research_metadata_count": fields.get("research_metadata_count"),
                        "backtest_start": fields.get("backtest_start"),
                        "backtest_end": fields.get("backtest_end"),
                        "error_message": fields.get("error_message"),
                    }
                    conn.execute(text(STAGE1_UPSERT_SQL), payload)
                    upsert_research_run(conn, strategy_id, fields)
                except Exception as exc:
                    print(
                        "Stage 1 detail read failed for "
                        f"{name} ({backtest_id}): {exc}"
                    )
                    conn.execute(text(LEGACY_UPSERT_SQL), base)
            elif is_stage1_name(name) and row_existing and row_existing.get("research_run_id"):
                merged = merge_stage1_lightweight_metrics(row_existing, metrics)
                conn.execute(text(STAGE1_LIGHTWEIGHT_UPSERT_SQL), {**base, **merged})
            elif needs_legacy_date_hydration(row_existing, backtest):
                try:
                    detail_result = get_backtest_detail(project_id, backtest_id)
                    detail_reads += 1
                    detail = detail_result.get("backtest") or detail_result
                    dates = legacy_hydration_fields(detail)
                    conn.execute(
                        text(LEGACY_DATE_UPSERT_SQL),
                        {
                            **base,
                            "backtest_start": dates.get("backtest_start"),
                            "backtest_end": dates.get("backtest_end"),
                            "parameters_json": json_param(dates.get("parameters_json")),
                        },
                    )
                except Exception as exc:
                    print(
                        "Legacy date hydration failed for "
                        f"{name} ({backtest_id}): {exc}"
                    )
                    conn.execute(text(LEGACY_UPSERT_SQL), base)
            else:
                conn.execute(text(LEGACY_UPSERT_SQL), base)

            if fetch_chart:
                try:
                    bounds = chart_request_window(
                        detail if isinstance(detail, dict) else None,
                        existing_row=row_existing,
                    )
                    chart = get_backtest_chart(
                        project_id,
                        backtest_id,
                        start=bounds["start"],
                        end=bounds["end"],
                        count=bounds["count"],
                    )
                    chart_reads += 1
                    points = parse_equity_chart(chart)
                    if points:
                        insert_equity_points(
                            conn,
                            strategy_id,
                            backtest_id,
                            points,
                        )
                    else:
                        print(
                            "Equity curve not available yet for "
                            f"{name} ({backtest_id})"
                        )
                except Exception as exc:
                    print(
                        "Equity chart sync failed for "
                        f"{name} ({backtest_id}): {exc}"
                    )

        try:
            audit_holdout_exposures(conn, strategy_id)
        except Exception as exc:
            print(f"Holdout exposure audit skipped: {exc}")
        try:
            refresh_research_run_progress(conn, strategy_id)
        except Exception as exc:
            print(f"Research run progress refresh skipped: {exc}")

    print(
        f"Backtest sync: {len(backtests)} listed, "
        f"{detail_reads} detail reads, {chart_reads} chart reads"
    )
    return len(backtests)


# =========================================================
# MAIN
# =========================================================

def print_sync_summary(
    strategy_id,
    strategy_name,
    status,
    portfolio,
    position_count,
    order_count,
    trade_count,
    backtest_count,
):
    label = strategy_id

    if strategy_name and strategy_name != strategy_id:
        label = f"{strategy_name} ({strategy_id})"

    print()
    print("-" * 60)
    print(f"Sync summary: {label}")
    print(f"  Live status:          {status}")

    if portfolio:
        print(
            f"  Equity:               "
            f"${portfolio['equity']:,.2f}"
        )
        print(
            f"  Cash:                 "
            f"${portfolio['cash']:,.2f}"
        )
        print(
            f"  Holdings value:       "
            f"${portfolio['holdings_value']:,.2f}"
        )
    else:
        print("  Equity:               —")
        print("  Cash:                 —")
        print("  Holdings value:       —")

    print(f"  Positions:            {position_count}")
    print(f"  Orders synced:        {order_count}")
    print(f"  Closed trades synced: {trade_count}")
    print(f"  Backtests synced:     {backtest_count}")
    print("-" * 60)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Sync QuantConnect live state and/or backtests into PostgreSQL.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live-only",
        action="store_true",
        help="Skip backtest sync; keep live/paper reconciliation only.",
    )
    mode.add_argument(
        "--backtests-only",
        action="store_true",
        help="Skip live APIs; sync research/legacy backtests only.",
    )
    parser.add_argument(
        "--import-run-summary",
        metavar="PATH",
        default=None,
        help=(
            "Import an orchestrator run_summary.json so skipped/no-QC "
            "experiments update research_runs instead of leaving 80/81 "
            "IN_PROGRESS. --backtests-only also auto-imports files under "
            "stage1_results/**/run_summary.json."
        ),
    )
    return parser.parse_args(argv)


def migration_failure_exit_code(migration_error, sync_backtests_requested: bool):
    if migration_error and sync_backtests_requested:
        return 1
    return None


def main(argv=None):
    args = parse_args(argv)
    sync_live = not args.backtests_only
    sync_bts = not args.live_only

    ensure_schema()

    migration_error = None
    try:
        from jobs.apply_migrations import apply_migrations

        apply_migrations()
    except Exception as exc:
        migration_error = exc
        print(f"WARNING: migrations were not applied: {exc}")

    blocked = migration_failure_exit_code(migration_error, sync_bts)
    if blocked is not None:
        print(
            "ERROR: Stage 1 backtest sync requires a successful migration. "
            "Refusing to continue and downgrade Stage 1 rows to legacy upserts."
        )
        return blocked
    if migration_error and not sync_bts:
        print("WARNING: continuing --live-only without Stage 1 schema updates.")

    strategies = get_strategies()

    print(
        f"Found {len(strategies)} registered strategies."
    )

    sync_live = not args.backtests_only
    sync_bts = not args.live_only

    for strategy in strategies:

        strategy_id = strategy["strategy_id"]
        strategy_name = strategy.get("name") or strategy_id
        project_id = strategy["qc_project_id"]
        deployment_id = strategy["qc_deployment_id"]

        status = "UNKNOWN"
        portfolio = None
        position_count = 0
        order_count = 0
        trade_count = 0
        backtest_count = 0

        print()
        print("=" * 60)
        print(f"Syncing {strategy_name} ({strategy_id})")
        print("=" * 60)

        # -------------------------------------------------
        # BACKTESTS
        # -------------------------------------------------

        if sync_bts:
            try:
                research_id = resolve_research_project_id(strategy)
                execution_id = execution_project_id(strategy)
                if research_id and execution_id and str(research_id) == str(execution_id):
                    print(
                        "Research and execution QuantConnect projects must be "
                        "separate. Skipping research backtest sync rather than "
                        "falling back to the execution project."
                    )
                elif research_id:
                    backtests_result = get_backtests(research_id)
                    backtest_count = sync_backtests(
                        strategy_id,
                        research_id,
                        backtests_result,
                    )
                    try:
                        from qc_research.object_store_sync import sync_stage2_object_store

                        store_summary = sync_stage2_object_store(
                            engine,
                            strategy_id=strategy_id,
                            qc_post=qc_post,
                        )
                        if store_summary.get("runs"):
                            print(
                                "Stage 2 Object Store sync: "
                                "{0} run(s), ingested={1}, skipped={2}, errors={3}".format(
                                    store_summary.get("runs"),
                                    store_summary.get("ingested"),
                                    store_summary.get("skipped"),
                                    len(store_summary.get("errors") or []),
                                )
                            )
                    except Exception as store_exc:
                        print("Stage 2 Object Store sync error: {0}".format(store_exc))
                else:
                    print(
                        "Skipping research backtest sync; dedicated research "
                        "project is not initialized."
                    )

            except Exception as exc:
                print(
                    f"Backtest sync error: {exc}"
                )
        else:
            print("Skipping backtest sync (--live-only).")

        if not sync_live:
            print("Skipping live sync (--backtests-only).")
            print_sync_summary(
                strategy_id,
                strategy_name,
                status,
                portfolio,
                position_count,
                order_count,
                trade_count,
                backtest_count,
            )
            continue

        # No live deployment yet
        if not deployment_id:
            print(
                "No live deployment ID. "
                "Skipping live sync."
            )
            print_sync_summary(
                strategy_id,
                strategy_name,
                status,
                portfolio,
                position_count,
                order_count,
                trade_count,
                backtest_count,
            )
            continue

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        try:
            live_result = get_live_status(project_id)

            status = live_result.get(
                "status",
                "UNKNOWN",
            )

            current_deployment = (
                live_result.get("deployId")
                or deployment_id
            )

            update_strategy_status(
                strategy_id,
                status,
                current_deployment,
            )

        except Exception as exc:
            print(
                f"Status sync error: {exc}"
            )

            status = "UNKNOWN"

        # -------------------------------------------------
        # PORTFOLIO
        # -------------------------------------------------

        try:
            portfolio_result = get_live_portfolio(
                project_id
            )

            portfolio = parse_portfolio(
                portfolio_result
            )

            position_count = len(
                portfolio["positions"]
            )

            skip_reason = snapshot_is_malformed(
                portfolio,
                portfolio_result,
            )

            if skip_reason:
                print(f"WARNING: {skip_reason}")
                print(
                    "  parsed equity="
                    f"${portfolio['equity']:,.2f} "
                    "cash="
                    f"${portfolio['cash']:,.2f} "
                    "holdings="
                    f"${portfolio['holdings_value']:,.2f} "
                    f"positions={position_count}"
                )

            else:
                inserted = insert_live_snapshot(
                    strategy_id,
                    status,
                    portfolio,
                )

                if inserted:
                    insert_positions(
                        strategy_id,
                        portfolio["positions"],
                    )

        except Exception as exc:
            print(
                f"Portfolio sync error: {exc}"
            )

        # -------------------------------------------------
        # ORDERS
        # -------------------------------------------------

        try:
            orders_result = get_live_orders(
                project_id,
                deployment_id,
            )

            order_count = sync_orders(
                strategy_id,
                orders_result,
            )

        except Exception as exc:
            print(
                f"Orders sync error: {exc}"
            )

        # -------------------------------------------------
        # TRADES
        # -------------------------------------------------

        try:
            trades_result = get_live_trades(
                project_id,
                deployment_id,
            )

            trade_count = sync_trades(
                strategy_id,
                trades_result,
            )

        except Exception as exc:
            print(
                f"Trades sync error: {exc}"
            )

        print_sync_summary(
            strategy_id,
            strategy_name,
            status,
            portfolio,
            position_count,
            order_count,
            trade_count,
            backtest_count,
        )

    if sync_bts:
        from pathlib import Path

        summary_paths = []
        if args.import_run_summary:
            summary_paths.append(Path(args.import_run_summary))
        summary_paths.extend(discover_run_summary_paths())
        if summary_paths:
            try:
                with engine.begin() as conn:
                    imported = import_run_summaries(conn, summary_paths)
                for row in imported:
                    print(
                        "Imported orchestrator run summary: {0} ({1})".format(
                            row["path"], row.get("run_status") or "unknown"
                        )
                    )
            except Exception as exc:
                print("ERROR: failed to import run summary: {0}".format(exc))
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
