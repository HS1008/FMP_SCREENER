"""Order Flow (Market Intelligence, DB-only).

Corporate Bond Trading Activity from canonical PostgreSQL. No FINRA/FRED/IBKR calls.
"""

from db.dashboard_engine import strip_writer_database_env

strip_writer_database_env()

from market_intelligence.pages_ui import render_order_flow

render_order_flow()
