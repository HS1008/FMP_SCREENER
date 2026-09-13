"""IBKR equity EOD adapter: TWS historical bars -> canonical EquityBar.

Production path is Windows TWS (localhost) -> collector fetch-eod -> private
ingest API -> PostgreSQL. DigitalOcean sets ``MI_EQUITY_PROVIDER=ibkr`` or
``ibkr_collector`` to consume stored bars and never opens a TWS socket.
``IBKRAdapter`` is for an explicit TWS session on the collector host only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from ibkr_collector.historical import (
    ADJUSTMENT_BASIS_ADJUSTED_LAST,
    BACKFILL_DURATION,
    HistoricalSession,
    INCREMENTAL_DURATION,
    STATUS_ADJUSTED_LAST_REJECTED,
    STATUS_OK,
    SymbolFetchResult,
    WHAT_TO_SHOW_ADJUSTED,
    adjustment_basis_for,
    connect_historical_session,
    duration_for_lookback,
    fetch_universe,
)
from market_intelligence.equity_eod import EQUITY_SOURCE_ID, AdapterUnavailable, EquityBar

IBKR_PROVIDER = "IBKR"
ALLOW_TWS_SOCKET_ENV = "IBKR_ALLOW_TWS_SOCKET"
TWS_HOST_ENV = "IBKR_TWS_HOST"
TWS_PORT_ENV = "IBKR_TWS_PORT"
EOD_CLIENT_ID_ENV = "IBKR_EOD_CLIENT_ID"


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def tws_socket_allowed(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return _truthy(environ.get(ALLOW_TWS_SOCKET_ENV))


@dataclass
class IBKRAdapter:
    """EquityDailyAdapter for Interactive Brokers historical daily bars.

    ``source_id`` here is the *provider* identity written to the EQUITY_EOD
    registry row. Bars themselves keep ``EquityBar.source_id = EQUITY_EOD``.
    """

    source_id: str = IBKR_PROVIDER
    access_status: str = "CONFIGURED"
    reason: str = (
        "IBKR daily bars use ADJUSTED_LAST (split and dividend adjusted). "
        "TWS sockets stay on the Windows collector host. Remote AI export remains INTERNAL_ONLY."
    )
    session: HistoricalSession | None = None
    env: Mapping[str, str] | None = None
    incremental: bool = True
    recorded: list[SymbolFetchResult] = field(default_factory=list)
    last_results: list[SymbolFetchResult] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "IBKRAdapter":
        return cls(env=env)

    def fetch(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        if self.recorded:
            return self._from_recorded(symbols, start, end)
        session = self.session
        owned_client = None
        if session is None:
            if not tws_socket_allowed(self.env):
                raise AdapterUnavailable(
                    "IBKR daily bars are ingested by the Windows collector "
                    "(python -m ibkr_collector fetch-eod). This host does not open a TWS socket."
                )
            host = str((self.env or os.environ).get(TWS_HOST_ENV) or "127.0.0.1")
            port = int((self.env or os.environ).get(TWS_PORT_ENV) or 7496)
            client_id = int((self.env or os.environ).get(EOD_CLIENT_ID_ENV) or 72)
            session, owned_client, error = connect_historical_session(host=host, port=port, client_id=client_id)
            if session is None:
                raise AdapterUnavailable("TWS session unavailable: {0}".format(error))
        try:
            duration = duration_for_lookback(start=start, end=end, incremental=self.incremental)
            results = fetch_universe(session, symbols, duration=duration, what_to_show=WHAT_TO_SHOW_ADJUSTED)
            self.last_results = results
            rejected = [r for r in results if r.status == STATUS_ADJUSTED_LAST_REJECTED]
            if rejected:
                raise AdapterUnavailable(
                    "ADJUSTED_LAST rejected for {0}; not falling back to TRADES (would change return economics).".format(
                        ",".join(r.symbol for r in rejected)
                    )
                )
            bars: list[EquityBar] = []
            for result in results:
                bars.extend(equity_bars_from_result(result, start=start, end=end))
            return bars
        finally:
            if owned_client is not None:
                try:
                    owned_client.disconnect()
                except Exception:
                    pass

    def _from_recorded(self, symbols: list[str], start: date, end: date) -> list[EquityBar]:
        wanted = set(symbols)
        bars: list[EquityBar] = []
        for result in self.recorded:
            if result.symbol not in wanted:
                continue
            bars.extend(equity_bars_from_result(result, start=start, end=end))
        self.last_results = list(self.recorded)
        return bars


def equity_bars_from_result(result: SymbolFetchResult, *, start: date, end: date) -> list[EquityBar]:
    if result.status != STATUS_OK:
        return []
    out: list[EquityBar] = []
    basis = adjustment_basis_for(result.what_to_show or WHAT_TO_SHOW_ADJUSTED)
    con_id = result.qualified.con_id if result.qualified else None
    for raw in result.bars:
        if raw.bar_date < start or raw.bar_date > end:
            continue
        out.append(
            EquityBar(
                instrument_id=result.symbol,
                bar_date=raw.bar_date,
                adj_close=raw.close,
                close=raw.close,
                open_price=raw.open,
                high=raw.high,
                low=raw.low,
                volume=raw.volume,
                adjustment_basis=basis,
                provider_symbol=result.symbol,
                source_id=EQUITY_SOURCE_ID,
                con_id=con_id,
                provider=IBKR_PROVIDER,
            )
        )
    return out


def results_to_ingest_payload(
    results: list[SymbolFetchResult],
    *,
    collector_id: str,
    requested: list[str] | None = None,
    request_mode: str = "incremental",
) -> dict[str, Any]:
    from ibkr_collector.historical import coverage_summary

    bars: list[dict[str, Any]] = []
    for result in results:
        if result.status != STATUS_OK or not result.qualified:
            continue
        basis = adjustment_basis_for(result.what_to_show or WHAT_TO_SHOW_ADJUSTED)
        if basis != ADJUSTMENT_BASIS_ADJUSTED_LAST or result.what_to_show != WHAT_TO_SHOW_ADJUSTED:
            continue
        for raw in result.bars:
            bars.append(
                {
                    "symbol": result.symbol,
                    "con_id": result.qualified.con_id,
                    "sec_type": result.qualified.sec_type,
                    "exchange": result.qualified.exchange,
                    "primary_exchange": result.qualified.primary_exchange,
                    "currency": result.qualified.currency,
                    "bar_date": raw.bar_date.isoformat(),
                    "open": raw.open,
                    "high": raw.high,
                    "low": raw.low,
                    "close": raw.close,
                    "adj_close": raw.close,
                    "volume": raw.volume,
                    "adjustment_basis": basis,
                    "what_to_show": result.what_to_show,
                    "provider_symbol": result.symbol,
                }
            )
    coverage = coverage_summary(results, requested=requested)
    coverage["request_mode"] = request_mode
    return {
        "collector_id": collector_id,
        "provider": IBKR_PROVIDER,
        "source_id": EQUITY_SOURCE_ID,
        "what_to_show": WHAT_TO_SHOW_ADJUSTED,
        "adjustment_basis": ADJUSTMENT_BASIS_ADJUSTED_LAST,
        "request_mode": request_mode,
        "bars": bars,
        "coverage": coverage,
    }


__all__ = [
    "ALLOW_TWS_SOCKET_ENV",
    "IBKRAdapter",
    "IBKR_PROVIDER",
    "equity_bars_from_result",
    "results_to_ingest_payload",
    "tws_socket_allowed",
]
