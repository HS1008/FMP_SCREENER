"""Options (Market Intelligence, DB-only).

Reads stored Cboe volatility metrics. Does not call Cboe or any other provider.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_options

render_options()
