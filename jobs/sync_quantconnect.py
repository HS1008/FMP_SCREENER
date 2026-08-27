import os
from base64 import b64encode
from hashlib import sha256
from time import time

import requests
from dotenv import load_dotenv
from sqlalchemy import text

from db.connection import engine


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

QC_USER_ID = os.getenv("QC_USER_ID")
QC_API_TOKEN = os.getenv("QC_API_TOKEN")

BASE_URL = "https://www.quantconnect.com/api/v2"

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
                    qc_project_id,
                    qc_deployment_id
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


def parse_portfolio(result):
    portfolio = result.get("portfolio", {})

    cash_data = portfolio.get("cash", {}) or {}
    holdings = portfolio.get("holdings", {}) or {}

    total_cash = 0.0

    for currency in cash_data.values():
        total_cash += float(
            currency.get("valueInAccountCurrency", 0) or 0
        )

    positions = []
    holdings_value = 0.0

    for symbol_key, holding in holdings.items():

        symbol_obj = holding.get("symbol", {})

        if isinstance(symbol_obj, dict):
            symbol = (
                symbol_obj.get("value")
                or symbol_key
            )
        else:
            symbol = symbol_key

        quantity = float(
            holding.get("quantity", 0) or 0
        )

        price = float(
            holding.get("price", 0)
            or holding.get("marketPrice", 0)
            or 0
        )

        market_value = holding.get("marketValue")

        if market_value is None:
            market_value = quantity * price

        market_value = float(market_value or 0)

        holdings_value += market_value

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
            else 0
        )

    return {
        "cash": total_cash,
        "holdings_value": holdings_value,
        "equity": equity,
        "positions": positions,
    }


# =========================================================
# LIVE SNAPSHOTS
# =========================================================

def get_previous_peak(strategy_id):
    with engine.connect() as conn:
        value = conn.execute(
            text("""
                SELECT MAX(equity)
                FROM live_snapshots
                WHERE strategy_id = :strategy_id
            """),
            {"strategy_id": strategy_id},
        ).scalar()

    return float(value) if value is not None else None


def get_first_equity(strategy_id):
    with engine.connect() as conn:
        value = conn.execute(
            text("""
                SELECT equity
                FROM live_snapshots
                WHERE strategy_id = :strategy_id
                ORDER BY timestamp ASC
                LIMIT 1
            """),
            {"strategy_id": strategy_id},
        ).scalar()

    return float(value) if value is not None else None


def insert_live_snapshot(
    strategy_id,
    status,
    portfolio,
):
    equity = float(portfolio["equity"])

    first_equity = get_first_equity(strategy_id)
    previous_peak = get_previous_peak(strategy_id)

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


def sync_backtests(
    strategy_id,
    project_id,
    result,
):
    backtests = result.get("backtests", []) or []

    with engine.begin() as conn:

        for backtest in backtests:

            conn.execute(
                text("""
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
                        sharpe_ratio =
                            EXCLUDED.sharpe_ratio,
                        sortino_ratio =
                            EXCLUDED.sortino_ratio,
                        alpha =
                            EXCLUDED.alpha,
                        beta =
                            EXCLUDED.beta,
                        cagr =
                            EXCLUDED.cagr,
                        max_drawdown =
                            EXCLUDED.max_drawdown,
                        net_profit =
                            EXCLUDED.net_profit,
                        win_rate =
                            EXCLUDED.win_rate,
                        loss_rate =
                            EXCLUDED.loss_rate,
                        trade_count =
                            EXCLUDED.trade_count,
                        psr =
                            EXCLUDED.psr,
                        synced_at = NOW()
                """),
                {
                    "backtest_id":
                        backtest.get("backtestId"),
                    "strategy_id":
                        strategy_id,
                    "qc_project_id":
                        str(project_id),
                    "name":
                        backtest.get("name"),
                    "status":
                        backtest.get("status"),
                    "created_at":
                        backtest.get("created"),
                    "sharpe_ratio":
                        backtest.get("sharpeRatio"),
                    "sortino_ratio":
                        backtest.get("sortinoRatio"),
                    "alpha":
                        backtest.get("alpha"),
                    "beta":
                        backtest.get("beta"),
                    "cagr":
                        backtest.get(
                            "compoundingAnnualReturn"
                        ),
                    "max_drawdown":
                        backtest.get("drawdown"),
                    "net_profit":
                        backtest.get("netProfit"),
                    "win_rate":
                        backtest.get("winRate"),
                    "loss_rate":
                        backtest.get("lossRate"),
                    "trade_count":
                        backtest.get("trades"),
                    "psr":
                        backtest.get("psr"),
                },
            )

    return len(backtests)


# =========================================================
# MAIN
# =========================================================

def main():

    ensure_schema()

    strategies = get_strategies()

    print(
        f"Found {len(strategies)} registered strategies."
    )

    for strategy in strategies:

        strategy_id = strategy["strategy_id"]
        project_id = strategy["qc_project_id"]
        deployment_id = strategy["qc_deployment_id"]

        print()
        print("=" * 60)
        print(f"Syncing {strategy_id}")
        print("=" * 60)

        # -------------------------------------------------
        # BACKTESTS
        # -------------------------------------------------

        try:
            backtests_result = get_backtests(project_id)

            backtest_count = sync_backtests(
                strategy_id,
                project_id,
                backtests_result,
            )

            print(
                f"Backtests synced: {backtest_count}"
            )

        except Exception as exc:
            print(
                f"Backtest sync error: {exc}"
            )

        # No live deployment yet
        if not deployment_id:
            print(
                "No live deployment ID. "
                "Skipping live sync."
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

            print(
                f"Live status: {status}"
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

            insert_live_snapshot(
                strategy_id,
                status,
                portfolio,
            )

            insert_positions(
                strategy_id,
                portfolio["positions"],
            )

            print(
                f"Equity: "
                f"${portfolio['equity']:,.2f}"
            )

            print(
                f"Cash: "
                f"${portfolio['cash']:,.2f}"
            )

            print(
                f"Holdings: "
                f"${portfolio['holdings_value']:,.2f}"
            )

            print(
                f"Positions: "
                f"{len(portfolio['positions'])}"
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

            print(
                f"Orders synced: {order_count}"
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

            print(
                f"Closed trades synced: {trade_count}"
            )

        except Exception as exc:
            print(
                f"Trades sync error: {exc}"
            )

        print(
            f"{strategy_id} sync complete."
        )


if __name__ == "__main__":
    main()
