"""PIT Sector Internals (Market Intelligence, DB-only).

Renders hash-verified ``sector_internals_v1`` aggregates that the isolated quant-strategies
producer built locally and the backend consumer ingested. Reads curated `mi_v_*` views through the
read-only role only. No QuantConnect, FMP, FRED or IBKR calls; no ingestion; no writer credentials.
"""

from market_intelligence.pages_ui import render_pit_sector_internals

render_pit_sector_internals()
