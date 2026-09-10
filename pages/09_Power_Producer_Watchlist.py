"""Multipage wrapper — delegates to the standalone power_producer_watchlist module."""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from power_producer_watchlist import render_dashboard

render_dashboard()
