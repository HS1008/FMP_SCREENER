"""Global Markets (Market Intelligence, DB-only).

Reads stored MARKET_MONITOR_EOD bars for the curated USD ETF proxies. No provider
calls from the page.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_global_markets

render_global_markets()
