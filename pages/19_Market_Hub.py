"""Market Hub (Market Intelligence, DB-only)."""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_market_hub

render_market_hub()
