"""Rates & Curve (Market Intelligence, DB-only).

Reads curated `mi_v_*` PostgreSQL views through the read-only role only. No FMP/FRED/
IBKR/QC calls, no filesystem precomputed fallback, no ingestion, no writer credentials.
"""

from db.dashboard_engine import load_streamlit_env

load_streamlit_env()

from market_intelligence.pages_ui import render_rates_curve

render_rates_curve()
