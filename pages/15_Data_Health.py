"""Data Health (Market Intelligence, DB-only).

Reads curated `mi_v_*` PostgreSQL views through the read-only role only. No FMP/FRED/
IBKR/QC calls, no filesystem precomputed fallback, no ingestion, no writer credentials.
"""

from market_intelligence.pages_ui import render_data_health

render_data_health()
