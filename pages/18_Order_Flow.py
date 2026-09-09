"""Order Flow (Market Intelligence, DB-only).

Corporate Bond Trading Activity from canonical PostgreSQL. No FINRA/FRED/IBKR calls.
"""

from market_intelligence.pages_ui import render_order_flow

render_order_flow()
