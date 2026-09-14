"""Fixed Income workspace (Market Intelligence, DB-only).

Calculator inputs stay in the session. They do not write PostgreSQL or fetch brokers.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_fixed_income

render_fixed_income()
