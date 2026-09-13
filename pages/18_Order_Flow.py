"""Order Flow (Market Intelligence, DB-only).

Corporate Bond Trading Activity from canonical PostgreSQL. No FINRA/FRED/IBKR calls.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_order_flow

render_order_flow()
