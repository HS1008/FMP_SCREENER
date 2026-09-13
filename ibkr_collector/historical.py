"""Read-only IBKR daily-bar helpers: contracts, pacing, parsing.

This module never places orders. It talks to TWS only through the existing
read-only client, and only when a caller on the TWS host constructs a session.
DigitalOcean must not import this to open a TWS socket.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Mapping

from ibkr_collector.values import finite_or_none, utcnow
from market_intelligence.calendars import CAL_NYSE, last_completed_session
from market_intelligence.taxonomy import UNIVERSE_SYMBOLS

# Official TWS API (interactivebrokers.github.io/tws-api/historical_bars.html,
# confirmed 2026-09-11): ADJUSTED_LAST is split- and dividend-adjusted last trade.
# TRADES is split-adjusted only. Do not label TRADES as adjusted close.
WHAT_TO_SHOW_ADJUSTED = "ADJUSTED_LAST"
WHAT_TO_SHOW_TRADES = "TRADES"
ADJUSTMENT_BASIS_ADJUSTED_LAST = "IBKR_ADJUSTED_LAST"
ADJUSTMENT_BASIS_TRADES = "IBKR_TRADES_UNADJUSTED"
BAR_SIZE_1_DAY = "1 day"
DEFAULT_EOD_CLIENT_ID = 72
DEFAULT_REQUEST_TIMEOUT_SEC = 60.0
# Official pacing: no identical request within 15s; no more than 60 / 10 min;
# max 50 simultaneous. Sequential with a 2s gap and a 50 / 10 min budget (46-symbol universe).
PACING_MIN_INTERVAL_SEC = 2.0
PACING_IDENTICAL_COOLDOWN_SEC = 16.0
PACING_MAX_PER_10_MIN = 50
PACING_WINDOW_SEC = 600.0
BACKFILL_DURATION = "2 Y"
INCREMENTAL_DURATION = "1 W"
PHASE3_SYMBOLS = ("SPY", "XLK", "XLE", "SMH", "NVDA")

# Deterministic US listings for the current-context dashboard universe.
# SMART + USD + this primaryExchange. Ambiguous leftovers fail closed.
PRIMARY_EXCHANGE: dict[str, str] = {
    "SPY": "ARCA",
    "XLC": "ARCA",
    "XLY": "ARCA",
    "XLP": "ARCA",
    "XLE": "ARCA",
    "XLF": "ARCA",
    "XLV": "ARCA",
    "XLI": "ARCA",
    "XLB": "ARCA",
    "XLU": "ARCA",
    "XLRE": "ARCA",
    "XLK": "ARCA",
    "XSD": "ARCA",
    "KRE": "ARCA",
    "XBI": "ARCA",
    "XOP": "ARCA",
    "XRT": "ARCA",
    "SMH": "NASDAQ",
    "NVDA": "NASDAQ",
    "AMD": "NASDAQ",
    "ASML": "NASDAQ",
    "AMAT": "NASDAQ",
    "LRCX": "NASDAQ",
    "KLAC": "NASDAQ",
    "MU": "NASDAQ",
    "AVGO": "NASDAQ",
    "MRVL": "NASDAQ",
    "TXN": "NASDAQ",
    "ADI": "NASDAQ",
    "ON": "NASDAQ",
    "QCOM": "NASDAQ",
    "CRWD": "NASDAQ",
    "PANW": "NASDAQ",
    "ZS": "NASDAQ",
    "FTNT": "NASDAQ",
    "MSFT": "NASDAQ",
    "AMZN": "NASDAQ",
    "GOOGL": "NASDAQ",
    "META": "NASDAQ",
    "TSM": "NYSE",
    "CRM": "NYSE",
    "NOW": "NYSE",
    "ORCL": "NYSE",
    "VRT": "NYSE",
    "ETN": "NYSE",
    "NVT": "NYSE",
}

US_PRIMARY = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "IEX"})
STATUS_OK = "ok"
STATUS_NO_ENTITLEMENT = "no_entitlement"
STATUS_INVALID = "invalid_contract"
STATUS_AMBIGUOUS = "ambiguous_contract"
STATUS_NO_DATA = "no_historical_data"
STATUS_TIMEOUT = "timeout"
STATUS_PACING = "pacing"
STATUS_OTHER = "other"
STATUS_ADJUSTED_LAST_REJECTED = "adjusted_last_rejected"
OVERALL_FULL_SUCCESS = "FULL_SUCCESS"
OVERALL_PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
OVERALL_FAILED = "FAILED"
EXIT_FULL_SUCCESS = 0
EXIT_PARTIAL_SUCCESS = 1
EXIT_FAILED = 2
EXIT_CONFIG = 3
EXIT_LOCK = 4


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str | None = None


@dataclass(frozen=True)
class QualifiedContract:
    symbol: str
    con_id: int
    exchange: str
    primary_exchange: str | None
    currency: str
    sec_type: str
    local_symbol: str | None = None
    long_name: str | None = None


@dataclass(frozen=True)
class HistoricalBarRaw:
    bar_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None
    what_to_show: str
    timezone_note: str = "TWS_LOGIN_TZ_DAILY_YYYYMMDD"


@dataclass
class SymbolFetchResult:
    symbol: str
    status: str
    reason: str | None = None
    qualified: QualifiedContract | None = None
    bars: list[HistoricalBarRaw] = field(default_factory=list)
    latency_ms: int = 0
    error_codes: list[int] = field(default_factory=list)
    what_to_show: str = WHAT_TO_SHOW_ADJUSTED
    duration: str = BACKFILL_DURATION
    market_data_mode: str | None = None


def dashboard_universe() -> tuple[str, ...]:
    return UNIVERSE_SYMBOLS


def spec_for(symbol: str) -> ContractSpec:
    primary = PRIMARY_EXCHANGE.get(symbol.upper())
    return ContractSpec(symbol=symbol.upper(), primary_exchange=primary)


def catalog_covers_universe() -> list[str]:
    return [s for s in UNIVERSE_SYMBOLS if s not in PRIMARY_EXCHANGE]


def duration_for_lookback(*, start: date, end: date, incremental: bool) -> str:
    if incremental:
        return INCREMENTAL_DURATION
    span = (end - start).days
    if span <= 7:
        return "1 W"
    if span <= 31:
        return "1 M"
    return BACKFILL_DURATION


def parse_ibkr_bar_date(raw: Any) -> date | None:
    """Daily bars are yyyyMMdd (official). Ignore any time suffix."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    digits = text[:8]
    if len(digits) != 8 or not digits.isdigit():
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def parse_historical_bar(raw: Mapping[str, Any], *, what_to_show: str) -> HistoricalBarRaw | None:
    day = parse_ibkr_bar_date(raw.get("date") or raw.get("time"))
    close = finite_or_none(raw.get("close"))
    if day is None or close is None:
        return None
    return HistoricalBarRaw(
        bar_date=day,
        open=finite_or_none(raw.get("open")),
        high=finite_or_none(raw.get("high")),
        low=finite_or_none(raw.get("low")),
        close=float(close),
        volume=finite_or_none(raw.get("volume")),
        what_to_show=what_to_show,
    )


def drop_incomplete_session(bars: list[HistoricalBarRaw], *, now: datetime | None = None) -> list[HistoricalBarRaw]:
    """Keep only bars on or before the last completed NYSE session."""
    completed = last_completed_session(now or utcnow(), CAL_NYSE)
    return [b for b in bars if b.bar_date <= completed]


def eod_end_datetime(now: datetime | None = None) -> str:
    """TWS endDateTime for completed daily bars (NYSE regular close).

    Empty endDateTime means 'now' and triggers IBKR 2188 unless the session has
    a streaming subscription covering the last 15 minutes. EOD collection must
    not request an incomplete current session.
    """
    completed = last_completed_session(now or utcnow(), CAL_NYSE)
    return "{0} 16:00:00 US/Eastern".format(completed.strftime("%Y%m%d"))


def adjustment_basis_for(what_to_show: str) -> str:
    if what_to_show == WHAT_TO_SHOW_ADJUSTED:
        return ADJUSTMENT_BASIS_ADJUSTED_LAST
    if what_to_show == WHAT_TO_SHOW_TRADES:
        return ADJUSTMENT_BASIS_TRADES
    return "IBKR_{0}".format(what_to_show)


def classify_historical_failure(errors: list[Mapping[str, Any]]) -> str:
    if not errors:
        return STATUS_OTHER
    codes = [int(e.get("error_code") or 0) for e in errors]
    messages = " ".join(str(e.get("error_string") or "").lower() for e in errors)
    kinds = {e.get("kind") for e in errors}
    if "pacing" in messages or 420 in codes:
        return STATUS_PACING
    if "entitlement" in kinds or 354 in codes or 10167 in codes or 10168 in codes:
        return STATUS_NO_ENTITLEMENT
    if "adjusted_last" in messages and ("end date" in messages or "rejected" in messages):
        return STATUS_ADJUSTED_LAST_REJECTED
    if 200 in codes or 321 in codes:
        return STATUS_INVALID
    if 162 in codes or 366 in codes or "no data" in messages or "hm ds" in messages or "hmds" in messages:
        return STATUS_NO_DATA
    return STATUS_OTHER


def select_unique_us_listing(rows: list[Mapping[str, Any]], spec: ContractSpec) -> tuple[QualifiedContract | None, str | None]:
    """Require one STK/USD US listing. Do not pick a foreign listing by ticker."""
    candidates: list[Mapping[str, Any]] = []
    for row in rows:
        if str(row.get("sec_type") or "").upper() not in {"STK", "ETF"}:
            continue
        if str(row.get("currency") or "").upper() != spec.currency:
            continue
        if str(row.get("symbol") or "").upper() != spec.symbol.upper():
            continue
        primary = str(row.get("primary_exchange") or "").upper()
        if spec.primary_exchange and primary and primary != spec.primary_exchange.upper():
            continue
        if primary and primary not in US_PRIMARY and not spec.primary_exchange:
            continue
        candidates.append(row)
    if not candidates:
        return None, STATUS_INVALID
    if spec.primary_exchange:
        preferred = [r for r in candidates if str(r.get("primary_exchange") or "").upper() == spec.primary_exchange.upper()]
        if len(preferred) == 1:
            candidates = preferred
        elif len(preferred) > 1:
            return None, STATUS_AMBIGUOUS
    if len(candidates) > 1:
        us = [r for r in candidates if str(r.get("primary_exchange") or "").upper() in US_PRIMARY]
        if len(us) == 1:
            candidates = us
        else:
            return None, STATUS_AMBIGUOUS
    row = candidates[0]
    con_id = row.get("con_id")
    if con_id in (None, ""):
        return None, STATUS_INVALID
    return (
        QualifiedContract(
            symbol=spec.symbol,
            con_id=int(con_id),
            exchange=str(row.get("exchange") or spec.exchange),
            primary_exchange=str(row.get("primary_exchange") or spec.primary_exchange or "") or None,
            currency=str(row.get("currency") or spec.currency),
            sec_type=str(row.get("sec_type") or spec.sec_type),
            local_symbol=row.get("local_symbol"),
            long_name=row.get("long_name"),
        ),
        None,
    )


class HistoricalPacer:
    """Sequential historical-request budget. Never issues identical keys within 16s."""

    def __init__(
        self,
        *,
        min_interval_sec: float = PACING_MIN_INTERVAL_SEC,
        identical_cooldown_sec: float = PACING_IDENTICAL_COOLDOWN_SEC,
        max_per_window: int = PACING_MAX_PER_10_MIN,
        window_sec: float = PACING_WINDOW_SEC,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.min_interval_sec = min_interval_sec
        self.identical_cooldown_sec = identical_cooldown_sec
        self.max_per_window = max_per_window
        self.window_sec = window_sec
        self._sleep = sleeper or time.sleep
        self._clock = clock or time.monotonic
        self._last_any = 0.0
        self._last_key: dict[str, float] = {}
        self._stamps: list[float] = []

    def wait(self, key: str) -> None:
        now = self._clock()
        self._stamps = [t for t in self._stamps if now - t < self.window_sec]
        if len(self._stamps) >= self.max_per_window:
            sleep_for = self.window_sec - (now - self._stamps[0]) + 0.1
            if sleep_for > 0:
                self._sleep(sleep_for)
                now = self._clock()
                self._stamps = [t for t in self._stamps if now - t < self.window_sec]
        gap = now - self._last_any
        if self._last_any and gap < self.min_interval_sec:
            self._sleep(self.min_interval_sec - gap)
            now = self._clock()
        last_same = self._last_key.get(key)
        if last_same is not None and now - last_same < self.identical_cooldown_sec:
            self._sleep(self.identical_cooldown_sec - (now - last_same))
            now = self._clock()
        self._last_any = now
        self._last_key[key] = now
        self._stamps.append(now)


def request_fingerprint(symbol: str, duration: str, what_to_show: str) -> str:
    return "{0}|{1}|{2}|{3}".format(symbol.upper(), duration, BAR_SIZE_1_DAY, what_to_show)


class HistoricalSession:
    """Thin wrapper over ReadOnlyTwsClient for daily ADJUSTED_LAST bars."""

    def __init__(self, client: Any, *, pacer: HistoricalPacer | None = None, timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC) -> None:
        self.client = client
        self.pacer = pacer or HistoricalPacer()
        self.timeout_sec = timeout_sec

    def qualify(self, spec: ContractSpec) -> tuple[QualifiedContract | None, str | None, list[int]]:
        req_id = self.client.next_req_id()
        self.client.wait_event(req_id, self.client.contract_details_done)
        contract = _ib_contract(spec)
        self.client.reqContractDetails(req_id, contract)
        done = self.client.contract_details_done[req_id].wait(self.timeout_sec)
        errors = [e for e in list(self.client.errors) if e.get("req_id") == req_id]
        codes = [int(e["error_code"]) for e in errors if e.get("error_code") is not None]
        if not done:
            return None, STATUS_TIMEOUT, codes
        rows = list(self.client.contract_details.get(req_id) or [])
        if not rows:
            status = classify_historical_failure(errors) if errors else STATUS_INVALID
            return None, status, codes
        qualified, status = select_unique_us_listing(rows, spec)
        if status:
            return None, status, codes
        return qualified, None, codes

    def daily_bars(self, qualified: QualifiedContract, *, duration: str, what_to_show: str = WHAT_TO_SHOW_ADJUSTED) -> tuple[list[HistoricalBarRaw], str | None, list[int]]:
        key = request_fingerprint(qualified.symbol, duration, what_to_show)
        self.pacer.wait(key)
        req_id = self.client.next_req_id()
        self.client.mark_historical(req_id)
        contract = _ib_contract(
            ContractSpec(
                symbol=qualified.symbol,
                sec_type=qualified.sec_type,
                exchange=qualified.exchange,
                currency=qualified.currency,
                primary_exchange=qualified.primary_exchange,
            )
        )
        contract.conId = qualified.con_id
        # Empty endDateTime is the official "now" query. Dated ends are rejected (TWS 321).
        # IBKR 2188 may warn about the last 15 minutes; completed EOD bars can still arrive.
        # drop_incomplete_session removes any in-progress current session bar.
        self.client.reqHistoricalData(req_id, contract, "", duration, BAR_SIZE_1_DAY, what_to_show, 1, 1, False, [])
        done = self.client.historical_done[req_id].wait(self.timeout_sec)
        errors = [e for e in list(self.client.errors) if e.get("req_id") == req_id]
        codes = [int(e["error_code"]) for e in errors if e.get("error_code") is not None]
        raw_bars = list(self.client.historical_bars.get(req_id) or [])
        if not done:
            try:
                self.client.cancelHistoricalData(req_id)
            except Exception:
                pass
            if not raw_bars:
                return [], STATUS_TIMEOUT, codes
        parsed = [b for b in (parse_historical_bar(row, what_to_show=what_to_show) for row in raw_bars) if b is not None]
        parsed = drop_incomplete_session(parsed)
        if parsed:
            return parsed, None, codes
        return [], classify_historical_failure(errors) if errors else STATUS_NO_DATA, codes


def fetch_symbol(session: HistoricalSession, symbol: str, *, duration: str, what_to_show: str = WHAT_TO_SHOW_ADJUSTED) -> SymbolFetchResult:
    started = time.perf_counter()
    spec = spec_for(symbol)
    qualified, status, codes = session.qualify(spec)
    if status or qualified is None:
        return SymbolFetchResult(
            symbol=symbol,
            status=status or STATUS_INVALID,
            reason=status,
            error_codes=codes,
            latency_ms=int((time.perf_counter() - started) * 1000),
            what_to_show=what_to_show,
            duration=duration,
        )
    bars, bar_status, bar_codes = session.daily_bars(qualified, duration=duration, what_to_show=what_to_show)
    if bar_status == STATUS_PACING:
        bars, bar_status, bar_codes = session.daily_bars(qualified, duration=duration, what_to_show=what_to_show)
    all_codes = codes + bar_codes
    if bar_status:
        return SymbolFetchResult(
            symbol=symbol,
            status=bar_status,
            reason=bar_status,
            qualified=qualified,
            bars=[],
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_codes=all_codes,
            what_to_show=what_to_show,
            duration=duration,
        )
    return SymbolFetchResult(
        symbol=symbol,
        status=STATUS_OK,
        qualified=qualified,
        bars=bars,
        latency_ms=int((time.perf_counter() - started) * 1000),
        error_codes=all_codes,
        what_to_show=what_to_show,
        duration=duration,
    )


def fetch_universe(
    session: HistoricalSession,
    symbols: list[str] | None = None,
    *,
    duration: str = BACKFILL_DURATION,
    what_to_show: str = WHAT_TO_SHOW_ADJUSTED,
) -> list[SymbolFetchResult]:
    wanted = list(symbols or UNIVERSE_SYMBOLS)
    return [fetch_symbol(session, symbol, duration=duration, what_to_show=what_to_show) for symbol in wanted]


def coverage_summary(results: list[SymbolFetchResult], *, requested: list[str] | None = None) -> dict[str, Any]:
    wanted = list(requested) if requested is not None else [row.symbol for row in results]
    by_symbol = {row.symbol: row for row in results}
    successful_symbols: list[str] = []
    failed_symbols: dict[str, str] = {}
    counts: dict[str, int] = {}
    for symbol in wanted:
        row = by_symbol.get(symbol)
        if row is None:
            failed_symbols[symbol] = STATUS_NO_DATA
            counts[STATUS_NO_DATA] = counts.get(STATUS_NO_DATA, 0) + 1
            continue
        counts[row.status] = counts.get(row.status, 0) + 1
        ok = row.status == STATUS_OK and bool(row.bars)
        if ok:
            successful_symbols.append(symbol)
        else:
            failed_symbols[symbol] = row.status if row.status != STATUS_OK else STATUS_NO_DATA
    success_n = len(successful_symbols)
    failed_n = len(failed_symbols)
    requested_n = len(wanted)
    if requested_n == 0 or success_n == 0:
        overall = OVERALL_FAILED
    elif failed_n == 0:
        overall = OVERALL_FULL_SUCCESS
    else:
        overall = OVERALL_PARTIAL_SUCCESS
    ratio = success_n / float(requested_n) if requested_n else 0.0
    earliest = min((min(b.bar_date for b in row.bars) for row in results if row.bars), default=None)
    latest = max((max(b.bar_date for b in row.bars) for row in results if row.bars), default=None)
    return {
        "overall": overall,
        "requested": requested_n,
        "requested_symbols": wanted,
        "successful": success_n,
        "successful_symbols": successful_symbols,
        "failed": failed_n,
        "failed_symbols": failed_symbols,
        "coverage_ratio": ratio,
        "by_status": counts,
        "provider": "IBKR",
        "what_to_show": WHAT_TO_SHOW_ADJUSTED,
        "adjustment_basis": ADJUSTMENT_BASIS_ADJUSTED_LAST,
        "history_days": {
            row.symbol: (max(b.bar_date for b in row.bars) - min(b.bar_date for b in row.bars)).days
            for row in results
            if row.bars
        },
        "bar_counts": {row.symbol: len(row.bars) for row in results if row.bars},
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
    }


def exit_code_for_coverage(summary: Mapping[str, Any]) -> int:
    overall = summary.get("overall")
    if overall == OVERALL_FULL_SUCCESS:
        return EXIT_FULL_SUCCESS
    if overall == OVERALL_PARTIAL_SUCCESS:
        return EXIT_PARTIAL_SUCCESS
    return EXIT_FAILED


def _ib_contract(spec: ContractSpec):
    from ibapi.contract import Contract

    contract = Contract()
    contract.symbol = spec.symbol
    contract.secType = spec.sec_type
    contract.exchange = spec.exchange
    contract.currency = spec.currency
    if spec.primary_exchange:
        contract.primaryExchange = spec.primary_exchange
    return contract


def connect_historical_session(
    *,
    host: str = "127.0.0.1",
    port: int = 7496,
    client_id: int = DEFAULT_EOD_CLIENT_ID,
    timeout_sec: float = 20.0,
) -> tuple[HistoricalSession | None, Any, str | None]:
    """Localhost TWS only. Caller must disconnect the returned client."""
    from ibkr_collector.diagnostic import probe_socket
    from ibkr_collector.readonly_client import ReadOnlyTwsClient
    import threading

    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None, None, "refused_non_localhost_tws"
    probe = probe_socket(host, port)
    if not probe["ok"]:
        return None, None, "tws_socket_unavailable"
    client = ReadOnlyTwsClient()
    connected = threading.Event()
    connect_error: list[str] = []

    def _connect() -> None:
        try:
            client.connect(host, port, client_id)
            connected.set()
        except Exception as exc:
            connect_error.append("{0}:{1}".format(exc.__class__.__name__, exc))

    worker = threading.Thread(target=_connect, name="ibkr-eod-connect", daemon=True)
    worker.start()
    if not connected.wait(timeout_sec):
        try:
            client.disconnect()
        except Exception:
            pass
        return None, None, "api_version_handshake_timeout"
    if connect_error or not client.isConnected():
        try:
            client.disconnect()
        except Exception:
            pass
        return None, None, connect_error[0] if connect_error else "tws_connect_failed"
    reader = threading.Thread(target=client.run, name="ibkr-eod-reader", daemon=True)
    reader.start()
    if not client.handshake.wait(timeout_sec):
        try:
            client.disconnect()
        except Exception:
            pass
        errors = list(getattr(client, "errors", []) or [])
        codes = [e.get("error_code") for e in errors if e.get("error_code") is not None]
        if 326 in codes:
            return None, None, "client_id_in_use"
        return None, None, "handshake_timeout"
    return HistoricalSession(client), client, None


__all__ = [
    "ADJUSTMENT_BASIS_ADJUSTED_LAST",
    "ADJUSTMENT_BASIS_TRADES",
    "BACKFILL_DURATION",
    "EXIT_CONFIG",
    "EXIT_FAILED",
    "EXIT_FULL_SUCCESS",
    "EXIT_LOCK",
    "EXIT_PARTIAL_SUCCESS",
    "OVERALL_FAILED",
    "OVERALL_FULL_SUCCESS",
    "OVERALL_PARTIAL_SUCCESS",
    "ContractSpec",
    "DEFAULT_EOD_CLIENT_ID",
    "HistoricalBarRaw",
    "HistoricalPacer",
    "HistoricalSession",
    "INCREMENTAL_DURATION",
    "PHASE3_SYMBOLS",
    "PRIMARY_EXCHANGE",
    "QualifiedContract",
    "STATUS_ADJUSTED_LAST_REJECTED",
    "STATUS_AMBIGUOUS",
    "STATUS_INVALID",
    "STATUS_NO_DATA",
    "STATUS_NO_ENTITLEMENT",
    "STATUS_OK",
    "STATUS_PACING",
    "STATUS_TIMEOUT",
    "SymbolFetchResult",
    "WHAT_TO_SHOW_ADJUSTED",
    "WHAT_TO_SHOW_TRADES",
    "adjustment_basis_for",
    "catalog_covers_universe",
    "classify_historical_failure",
    "connect_historical_session",
    "coverage_summary",
    "dashboard_universe",
    "drop_incomplete_session",
    "eod_end_datetime",
    "duration_for_lookback",
    "exit_code_for_coverage",
    "fetch_symbol",
    "fetch_universe",
    "parse_historical_bar",
    "parse_ibkr_bar_date",
    "select_unique_us_listing",
    "spec_for",
]
