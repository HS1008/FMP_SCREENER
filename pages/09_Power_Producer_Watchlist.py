"""Multipage wrapper — delegates to the standalone power_producer_watchlist module."""

from db.dashboard_engine import strip_writer_database_env

strip_writer_database_env()

from power_producer_watchlist import render_dashboard

render_dashboard()
