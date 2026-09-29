"""US Markets (Market Intelligence, DB-only).

Index charts, badges, ratios, and drawdowns read stored MARKET_MONITOR_EOD bars.
Sector and subsector heatmaps read stored EQUITY_EOD bars on one shared SPY
session endpoint. No Yahoo, IBKR, FRED, or PostgreSQL calls from the browser.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_us_markets

render_us_markets()
