"""Map bounded IBKR option quotes onto the canonical NormalizedChain schema.

Does not call TWS, OpenBB, or PostgreSQL. Recurring collection stays off.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from market_intelligence.catalog import EXPORT_INTERNAL_ONLY
from market_intelligence.openbb_provider.client import RawChainFetch
from market_intelligence.openbb_provider.normalize import normalize_chain

IBKR_OPTIONS_SOURCE_ID = "IBKR_OPTIONS"
IBKR_OPTIONS_PROVIDER = "ibkr"
IBKR_OPTIONS_ENDPOINT = "tws:reqSecDefOptParams+reqMktData"
IBKR_OPTIONS_COVERAGE = (
    "IBKR TWS option chain via reqSecDefOptParams and bounded reqMktData. "
    "Delayed unless this TWS username has live OPRA. Not a Cboe website JSON feed."
)


def quotes_to_contract_rows(quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for quote in quotes:
        right = str(quote.get("right") or "").upper()
        if right == "C":
            option_type = "call"
        elif right == "P":
            option_type = "put"
        else:
            option_type = None
        local = str(quote.get("local_symbol") or "").replace(" ", "")
        expiration = quote.get("expiration")
        if expiration and len(str(expiration)) == 8 and str(expiration).isdigit():
            expiration = "{0}-{1}-{2}".format(str(expiration)[0:4], str(expiration)[4:6], str(expiration)[6:8])
        rows.append(
            {
                "contract_symbol": local,
                "expiration": expiration,
                "strike": quote.get("strike"),
                "option_type": option_type,
                "bid": quote.get("bid"),
                "ask": quote.get("ask"),
                "last": quote.get("last"),
                "bid_size": quote.get("bid_size"),
                "ask_size": quote.get("ask_size"),
                "volume": quote.get("volume"),
                "open_interest": quote.get("open_interest"),
                "implied_volatility": quote.get("implied_volatility"),
                "delta": quote.get("delta"),
                "gamma": quote.get("gamma"),
                "theta": quote.get("theta"),
                "vega": quote.get("vega"),
                "theoretical_price": quote.get("theoretical_price"),
                "last_trade_time": quote.get("last_timestamp"),
            }
        )
    return rows


def normalize_ibkr_chain(result: Any, *, clock: datetime | None = None) -> Any:
    """Reuse NormalizedChain / analytics. Does not publish to PostgreSQL."""
    quotes = getattr(result, "quotes", None)
    symbol = getattr(result, "symbol", None)
    underlying = getattr(result, "underlying", None) or {}
    collected_at = getattr(result, "collected_at", None)
    fetched = clock
    if fetched is None:
        try:
            fetched = datetime.fromisoformat(str(collected_at))
        except (TypeError, ValueError):
            fetched = datetime.now(timezone.utc)
    raw = RawChainFetch(
        symbol=str(symbol or ""),
        contracts=quotes_to_contract_rows(list(quotes or [])),
        metadata={"current_price": underlying.get("spot"), "provider": IBKR_OPTIONS_PROVIDER},
        extra={"export_scope": EXPORT_INTERNAL_ONLY, "source_id": IBKR_OPTIONS_SOURCE_ID},
        fetched_at=fetched,
    )
    md_types = list(getattr(result, "market_data_types", None) or [])
    md_label = None
    if md_types:
        md_label = str(md_types[0])
    elif isinstance(underlying, dict):
        md_label = underlying.get("market_data_type")
    return normalize_chain(
        raw,
        clock=fetched,
        provider=IBKR_OPTIONS_PROVIDER,
        source_id=IBKR_OPTIONS_SOURCE_ID,
        coverage_note=IBKR_OPTIONS_COVERAGE,
        endpoint=IBKR_OPTIONS_ENDPOINT,
        delay_label="IBKR_{0}".format(md_label) if md_label else None,
        market_data_type=md_label,
    )
