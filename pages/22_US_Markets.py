"""US Markets (Market Intelligence, DB-only).

Reads stored EQUITY_EOD bars and sector snapshots. No Yahoo, IBKR, FRED, or
PostgreSQL calls from the browser.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_us_markets

render_us_markets()
