"""One frozen Market Overview snapshot shaped like ``Market_Overview_Template.xlsx``.

The Streamlit page and the Excel export both consume the dictionary built here.
Neither re-fetches or recalculates: the page renders it, the export writes it.

Every section reuses the loaders and window definitions of its full page so the
numbers agree with that page:

* US Indexes, Market Ratios, Global Equity Performance: ``MARKET_MONITOR_EOD``
  adjusted closes through :func:`markets_read.load_monitor_history` and the
  stored-session windows of :data:`markets_analytics.HORIZONS`.
* Sectors: the shared ``EQUITY_EOD`` session panel of
  :func:`markets_analytics.build_aligned_us_panel` (absolute mode of the US
  Markets sector heatmap).
* Yield Curve and Credit Spreads: :func:`read_models.rates_context` /
  :func:`read_models.credit_context` stored metrics; 6M and 1Y moves use the same
  ``transforms.calendar_change`` rule the ingest uses for 1W-3M.
* VIX term structure: ``mi_v_yahoo_vol_history`` tenors (``^VIX``, ``^VIX3M``,
  ``^VIX6M``, ``^VIX1Y``).
* MOVE, Commodities, FOREX, Crypto: ``mi_v_yahoo_cross_asset_history`` closes with
  the provider-observation windows of the FOREX/Commodities pages and the
  calendar-day windows of the Crypto page.

Missing values stay ``None``; a row older than :data:`STALE_AFTER_DAYS` is marked
``stale`` rather than being dropped or zeroed.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from market_intelligence import read_models
from market_intelligence.catalog import CREDIT_BROAD_TILES, CURVE_TENORS, FLY_2S5S10S_METRIC, SLOPE_10Y2Y_METRIC
from market_intelligence.commodity_analytics import close_points
from market_intelligence.cross_asset_read import yahoo_cross_asset_bars
from market_intelligence.cross_asset_universe import (
    COMMODITY_INSTRUMENTS,
    CRYPTO_INSTRUMENTS,
    FX_PAIRS,
    FX_WINDOWS,
    MOVE_INSTRUMENT_ID,
    MOVE_SOURCE_NOTE,
    USDCNH_SOURCE_NOTE,
)
from market_intelligence.crypto_analytics import calendar_return
from market_intelligence.ibkr_live_universe import SECTOR_ETFS
from market_intelligence.markets_analytics import HORIZONS, as_day, build_aligned_us_panel, price_ratio_points, session_window_returns
from market_intelligence.markets_read import load_equity_eod_closes, load_monitor_history
from market_intelligence.nulls import strict_dumps
from market_intelligence.overview_metrics import (
    CRYPTO_DAYS_PER_YEAR,
    RISK_METHODOLOGY,
    level_difference,
    observation_returns,
    percent_change_of_level,
    points_from_rows,
    risk_window_stats,
)
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.taxonomy import BENCHMARK_SPY, GLOBAL_MARKET_ETFS, SECTOR_PROXIES, US_LEADERSHIP
from market_intelligence.transforms import calendar_change, previous_observation_change

SNAPSHOT_VERSION = "market_overview_template_v1"
STALE_AFTER_DAYS = 5
HISTORY_LOOKBACK_DAYS = 420
CALENDAR_LOOKBACK_OBSERVATIONS = 300

SHORT_WINDOWS: tuple[str, ...] = ("1D", "1W", "1M", "3M")
LONG_WINDOWS: tuple[str, ...] = ("1D", "1W", "1M", "3M", "6M", "1Y")
CRYPTO_TEMPLATE_WINDOWS: tuple[tuple[str, int], ...] = (("1D", 1), ("1W", 7), ("1M", 30), ("3M", 90))

SECTION_US_INDEXES = "us_indexes"
SECTION_MARKET_RATIOS = "market_ratios"
SECTION_SECTORS = "sectors"
SECTION_YIELD_CURVE = "yield_curve"
SECTION_CREDIT = "credit_spreads"
SECTION_VIX_TERM = "vix_term_structure"
SECTION_GLOBAL = "global_equity"
SECTION_COMMODITIES = "commodities"
SECTION_FOREX = "forex"
SECTION_CRYPTO = "crypto"

SECTION_ORDER: tuple[str, ...] = (
    SECTION_US_INDEXES,
    SECTION_MARKET_RATIOS,
    SECTION_SECTORS,
    SECTION_YIELD_CURVE,
    SECTION_CREDIT,
    SECTION_VIX_TERM,
    SECTION_GLOBAL,
    SECTION_COMMODITIES,
    SECTION_FOREX,
    SECTION_CRYPTO,
)

US_INDEX_ROWS: tuple[tuple[str, str], ...] = (
    ("SPY", "S&P 500"),
    ("RSP", "Equal Weight S&P 500"),
    ("IWM", "Russell 2000 - Small Cap"),
    ("QQQ", "Nasdaq-100 - Growth/Tech Focused"),
    ("DIA", "Dow Jones Industrial Average - Mature Companies"),
)
GLOBAL_ROWS: tuple[tuple[str, str], ...] = GLOBAL_MARKET_ETFS
RATIO_ROWS: tuple[tuple[str, str], ...] = tuple((symbol, title) for symbol, title, _caption in US_LEADERSHIP)
COMMODITY_ROWS: tuple[tuple[str, str], ...] = (
    ("CL", "WTI Crude"),
    ("BZ", "Brent Crude"),
    ("NG", "Natural Gas"),
    ("GC", "Gold"),
    ("SI", "Silver"),
    ("HG", "Copper"),
    ("ZC", "Corn"),
    ("ZW", "Wheat"),
    ("ZS_F", "Soybeans"),
)
CRYPTO_ROWS: tuple[tuple[str, str], ...] = (("BTC", "BTC/USD"), ("ETH", "ETH/USD"))
VIX_TERM_ROWS: tuple[tuple[str, str], ...] = (("VIX_1M", "VIX"), ("VIX_3M", "VIX 3M"), ("VIX_6M", "VIX 6M"), ("VIX_1Y", "VIX 1Y"))
CREDIT_LABELS: dict[str, str] = {"IG": "IG OAS", "HY": "HY OAS", "EM": "EM OAS"}

# Canonical sector -> subsector display group on US Markets (XLK is "Tech" there).
SUBSECTOR_GROUP_BY_SECTOR: dict[str, str] = {
    sector: next((label for symbol, label in SECTOR_ETFS if symbol == etf), sector) for sector, etf in SECTOR_PROXIES.items()
}

_SECTION_TITLES: dict[str, str] = {
    SECTION_US_INDEXES: "US Indexes",
    SECTION_MARKET_RATIOS: "Market Ratios",
    SECTION_SECTORS: "Sectors",
    SECTION_YIELD_CURVE: "Yield Curve",
    SECTION_CREDIT: "Credit Spreads",
    SECTION_VIX_TERM: "VIX Term Structure",
    SECTION_GLOBAL: "Global Equity Performance",
    SECTION_COMMODITIES: "Commodities",
    SECTION_FOREX: "FOREX",
    SECTION_CRYPTO: "Crypto",
}
_SECTION_TARGETS: dict[str, tuple[str, str | None]] = {
    SECTION_US_INDEXES: ("us_markets", "index-snapshot"),
    SECTION_MARKET_RATIOS: ("us_markets", "relative-performance"),
    SECTION_SECTORS: ("us_markets", "sector-performance"),
    SECTION_YIELD_CURVE: ("rates", None),
    SECTION_CREDIT: ("credit", None),
    SECTION_VIX_TERM: ("options", "vix-term-structure"),
    SECTION_GLOBAL: ("global_markets", None),
    SECTION_COMMODITIES: ("commodities", None),
    SECTION_FOREX: ("forex", None),
    SECTION_CRYPTO: ("crypto", None),
}


def _iso(day: Any) -> str | None:
    parsed = as_day(day)
    return None if parsed is None else parsed.isoformat()


def _status(as_of: date | None, *, today: date, has_value: bool) -> str:
    if not has_value or as_of is None:
        return "missing"
    if (today - as_of).days > STALE_AFTER_DAYS:
        return "stale"
    return "ok"


def _row(
    key: str,
    label: str,
    *,
    level: float | None,
    as_of: Any,
    changes: Mapping[str, float | None],
    today: date,
    description: str | None = None,
    risk: Mapping[str, float | None] | None = None,
    changes_pct: Mapping[str, float | None] | None = None,
    source: str | None = None,
    note: str | None = None,
    drill: Mapping[str, Any] | None = None,
    change_kind: str | None = None,
    level_kind: str | None = None,
) -> dict[str, Any]:
    day = as_day(as_of)
    return {
        "key": key,
        "change_kind": change_kind,
        "level_kind": level_kind,
        "label": label,
        "description": description,
        "level": None if level is None else float(level),
        "as_of": None if day is None else day.isoformat(),
        "changes": {name: (None if value is None else float(value)) for name, value in changes.items()},
        "changes_pct": None if changes_pct is None else {name: (None if value is None else float(value)) for name, value in changes_pct.items()},
        "risk": None if risk is None else {name: (None if value is None else float(value)) for name, value in risk.items()},
        "status": _status(day, today=today, has_value=level is not None),
        "source": source,
        "note": note,
        "drill": dict(drill) if drill else None,
    }


def _section(
    section_id: str,
    *,
    change_columns: Sequence[str],
    change_kind: str,
    level_kind: str,
    rows: Sequence[Mapping[str, Any]],
    risk: bool,
    source: str,
    notes: Sequence[str] = (),
    export_change_kind: str | None = None,
) -> dict[str, Any]:
    route_id, anchor = _SECTION_TARGETS[section_id]
    dates = sorted({row["as_of"] for row in rows if row.get("as_of")})
    return {
        "section_id": section_id,
        "title": _SECTION_TITLES[section_id],
        "route_id": route_id,
        "anchor": anchor,
        "change_columns": list(change_columns),
        "change_kind": change_kind,
        "export_change_kind": export_change_kind or change_kind,
        "level_kind": level_kind,
        "risk": risk,
        "rows": [dict(row) for row in rows],
        "source": source,
        "notes": list(notes),
        "as_of_min": dates[0] if dates else None,
        "as_of_max": dates[-1] if dates else None,
    }


def _bar_points(history: Mapping[str, Any], symbol: str) -> list[tuple[date, float]]:
    return points_from_rows(history.get("bars", {}).get(symbol) or [], date_key="date", value_key="value")


def _monitor_rows(history: Mapping[str, Any], rows: Sequence[tuple[str, str]], *, today: date, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    returns = history.get("returns") or {}
    for symbol, description in rows:
        points = _bar_points(history, symbol)
        window = returns.get(symbol) or {}
        out.append(
            _row(
                symbol,
                symbol,
                description=description,
                level=(history.get("latest_price") or {}).get(symbol),
                as_of=(history.get("meta") or {}).get(symbol, {}).get("latest"),
                changes={label: window.get(label) for label in SHORT_WINDOWS},
                risk=risk_window_stats(points),
                today=today,
                source=source,
            )
        )
    return out


def _ratio_rows(history: Mapping[str, Any], *, today: date) -> list[dict[str, Any]]:
    bench = _bar_points(history, BENCHMARK_SPY)
    out: list[dict[str, Any]] = []
    for symbol, title in RATIO_ROWS:
        ratio = price_ratio_points(_bar_points(history, symbol), bench)
        series = {day: value for day, value in ratio}
        last = ratio[-1][0] if ratio else None
        window = session_window_returns(series, last) if last is not None else {}
        out.append(
            _row(
                "{0}_{1}".format(symbol, BENCHMARK_SPY),
                title,
                level=ratio[-1][1] if ratio else None,
                as_of=last,
                changes={label: window.get(label) for label in SHORT_WINDOWS},
                today=today,
                source="MARKET_MONITOR_EOD (Yahoo adjusted closes)",
            )
        )
    return out


def _sector_rows(closes: Mapping[str, Mapping[str, Any]], panel: Mapping[str, Any], *, today: date) -> list[dict[str, Any]]:
    columns = [label for label, _field, _sessions in HORIZONS]
    by_label = {str(row.get("label")): row for row in panel.get("sectors") or []}
    endpoint = as_day(panel.get("endpoint"))
    out: list[dict[str, Any]] = []
    for sector in CANONICAL_SECTORS:
        symbol = SECTOR_PROXIES[sector]
        record = closes.get(symbol) or {}
        prices: Mapping[date, float] = record.get("prices") or {}
        points = sorted(prices.items())
        panel_row = by_label.get(sector) or {}
        values = dict(zip(columns, panel_row.get("values") or []))
        level = prices.get(endpoint) if endpoint is not None else None
        out.append(
            _row(
                symbol,
                sector,
                description=symbol,
                level=level,
                as_of=endpoint if level is not None else None,
                changes={label: values.get(label) for label in SHORT_WINDOWS},
                risk=risk_window_stats(points),
                today=today,
                source="EQUITY_EOD shared SPY session endpoint",
                note=None if record.get("rejection") in (None, "") else str(record.get("rejection")),
                drill={
                    "route_id": "us_markets",
                    "anchor": "subsector-performance",
                    "state": {"us_subsector_sector": SUBSECTOR_GROUP_BY_SECTOR.get(sector, sector)},
                    "sector": sector,
                },
            )
        )
    return out


def _observation_map(rows: Sequence[Mapping[str, Any]], *, date_key: str, value_key: str) -> dict[date, float]:
    return dict(points_from_rows(rows, date_key=date_key, value_key=value_key))


def _calendar_moves(obs: Mapping[date, float], at: date | None, *, scale: float) -> dict[str, float | None]:
    """1D..1Y level differences with the ingest's calendar anchors. Values already in the stored unit times ``scale``."""
    if at is None or at not in obs:
        return {label: None for label in LONG_WINDOWS}
    return {
        "1D": previous_observation_change(obs, at, units="level", scale=scale).value,
        "1W": calendar_change(obs, at, days=7, cadence="D", units="level", scale=scale).value,
        "1M": calendar_change(obs, at, months=1, cadence="D", units="level", scale=scale).value,
        "3M": calendar_change(obs, at, months=3, cadence="D", units="level", scale=scale).value,
        "6M": calendar_change(obs, at, months=6, cadence="D", units="level", scale=scale).value,
        "1Y": calendar_change(obs, at, months=12, cadence="D", units="level", scale=scale).value,
    }


def _yield_rows(rates: Mapping[str, Any], observations: Mapping[str, Sequence[Mapping[str, Any]]], *, today: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    legs = {str(row.get("tenor")): row for row in rates.get("curve") or []}
    for tenor, series_id in CURVE_TENORS.items():
        leg = legs.get(tenor) or {}
        level = leg.get("yield_pct")
        at = as_day(leg.get("observation_date"))
        obs = _observation_map(observations.get(series_id) or [], date_key="observation_date", value_key="value")
        computed = _calendar_moves(obs, at, scale=100.0)
        changes = {
            "1D": leg.get("chg_prev_bps"),
            "1W": leg.get("chg_1w_bps"),
            "1M": leg.get("chg_1m_bps"),
            "3M": leg.get("chg_3m_bps"),
            "6M": computed["6M"],
            "1Y": computed["1Y"],
        }
        out.append(
            _row(
                tenor,
                tenor,
                level=level,
                as_of=at,
                changes=changes,
                changes_pct={label: percent_change_of_level(level, move) for label, move in changes.items()},
                today=today,
                source=str(leg.get("source_id") or "TREASURY/FRED"),
            )
        )
    return out


def _metric_rows(
    entries: Sequence[tuple[str, str, str, Mapping[str, Any] | None, Sequence[Mapping[str, Any]]]],
    *,
    today: date,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key, label, source, latest, history in entries:
        obs = _observation_map(history, date_key="as_of", value_key="value")
        value = None if latest is None else latest.get("value")
        at = None if latest is None else as_day(latest.get("as_of"))
        if at is None and obs:
            at = max(obs)
            value = obs[at] if value is None else value
        changes = _calendar_moves(obs, at, scale=1.0)
        out.append(
            _row(
                key,
                label,
                level=value,
                as_of=at,
                changes=changes,
                changes_pct={name: (None if value is None or move is None or (float(value) - move) == 0 else move / abs(float(value) - move)) for name, move in changes.items()},
                today=today,
                source=source,
            )
        )
    return out


def _move_row(bars: Sequence[Mapping[str, Any]], *, today: date) -> dict[str, Any]:
    points = close_points([row for row in bars if row.get("instrument_id") == MOVE_INSTRUMENT_ID])
    returns = observation_returns(points, FX_WINDOWS)
    return _row(
        MOVE_INSTRUMENT_ID,
        "MOVE",
        level=points[-1][1] if points else None,
        as_of=points[-1][0] if points else None,
        changes={label: returns.get(label) for label in LONG_WINDOWS},
        changes_pct={label: returns.get(label) for label in LONG_WINDOWS},
        today=today,
        source="Yahoo ^MOVE (ICE BofA MOVE), EOD",
        note=MOVE_SOURCE_NOTE,
        drill={"route_id": "rates", "anchor": "move-index", "state": {}},
        change_kind="fraction",
        level_kind="index",
    )


def _credit_rows(credit: Mapping[str, Any], observations: Mapping[str, Sequence[Mapping[str, Any]]], *, today: date) -> list[dict[str, Any]]:
    by_id = {str(row.get("series_id")): row for row in credit.get("buckets") or []}
    out: list[dict[str, Any]] = []
    for series_id, short in CREDIT_BROAD_TILES:
        bucket = by_id.get(series_id) or {}
        level = bucket.get("oas_bps")
        at = as_day(bucket.get("as_of"))
        obs = _observation_map(observations.get(series_id) or [], date_key="observation_date", value_key="value")
        computed = _calendar_moves(obs, at, scale=100.0)
        out.append(
            _row(
                series_id,
                CREDIT_LABELS.get(short, short),
                level=level,
                as_of=at,
                changes={
                    "1D": bucket.get("change_1d_bps"),
                    "1W": bucket.get("change_1w_bps"),
                    "1M": bucket.get("change_1m_bps"),
                    "3M": bucket.get("change_3m_bps"),
                    "6M": computed["6M"],
                    "1Y": computed["1Y"],
                },
                today=today,
                source="FRED / ICE BofA OAS",
            )
        )
    return out


def _vix_rows(vol: Mapping[str, Any], *, today: date) -> list[dict[str, Any]]:
    history = vol.get("history") or {}
    out: list[dict[str, Any]] = []
    for metric_id, label in VIX_TERM_ROWS:
        points = points_from_rows(history.get(metric_id) or [], date_key="as_of", value_key="value")
        returns = observation_returns(points, FX_WINDOWS)
        out.append(
            _row(
                metric_id,
                label,
                level=points[-1][1] if points else None,
                as_of=points[-1][0] if points else None,
                changes={name: returns.get(name) for name in SHORT_WINDOWS},
                today=today,
                source="YAHOO_VOL index closes",
            )
        )
    return out


def _cross_asset_rows(
    bars: Sequence[Mapping[str, Any]],
    rows: Sequence[tuple[str, str]],
    *,
    columns: Sequence[str],
    today: date,
    source: str,
    risk: bool,
    note_by_key: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for instrument_id, label in rows:
        points = close_points([row for row in bars if row.get("instrument_id") == instrument_id])
        returns = observation_returns(points, FX_WINDOWS)
        out.append(
            _row(
                instrument_id,
                label,
                level=points[-1][1] if points else None,
                as_of=points[-1][0] if points else None,
                changes={name: returns.get(name) for name in columns},
                risk=risk_window_stats(points) if risk else None,
                today=today,
                source=source,
                note=(note_by_key or {}).get(instrument_id),
            )
        )
    return out


def _crypto_rows(bars: Sequence[Mapping[str, Any]], *, today: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for instrument_id, label in CRYPTO_ROWS:
        points = close_points([row for row in bars if row.get("instrument_id") == instrument_id])
        out.append(
            _row(
                instrument_id,
                label,
                level=points[-1][1] if points else None,
                as_of=points[-1][0] if points else None,
                changes={name: calendar_return(points, days=days) for name, days in CRYPTO_TEMPLATE_WINDOWS},
                risk=risk_window_stats(points, window=90, periods_per_year=CRYPTO_DAYS_PER_YEAR),
                today=today,
                source="YAHOO_CRYPTO UTC daily closes",
            )
        )
    return out


def _snapshot_id(sections: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(strict_dumps(list(sections)).encode("utf-8")).hexdigest()[:16]


def _guarded(conn, name: str, loader: Any, default: Any, errors: dict[str, str]) -> Any:
    """Run one read inside a savepoint so a missing view empties one section, not the page."""
    try:
        with conn.begin_nested():
            return loader()
    except Exception as exc:  # noqa: BLE001 - the section reports the sanitized failure class
        errors[name] = exc.__class__.__name__
        return default


def overview_snapshot(conn, *, today: date | None = None) -> dict[str, Any]:
    """Build the full template-shaped snapshot from stored PostgreSQL reads only."""
    reference = today or date.today()
    since = reference - timedelta(days=HISTORY_LOOKBACK_DAYS)
    errors: dict[str, str] = {}
    monitor_symbols: list[str] = []
    for symbol, _label in (*US_INDEX_ROWS, *GLOBAL_ROWS):
        if symbol not in monitor_symbols:
            monitor_symbols.append(symbol)
    monitor = _guarded(conn, "market_monitor", lambda: load_monitor_history(conn, monitor_symbols, since=since), {}, errors)
    sector_symbols = [BENCHMARK_SPY, *SECTOR_PROXIES.values()]
    closes = _guarded(conn, "equity_eod", lambda: load_equity_eod_closes(conn, sector_symbols, since=since), {}, errors)
    panel = build_aligned_us_panel(closes, baskets=())
    rates = _guarded(conn, "rates", lambda: read_models.rates_context(conn), {}, errors)
    credit = _guarded(conn, "credit", lambda: read_models.credit_context(conn), {}, errors)
    series_ids = [*CURVE_TENORS.values(), *(series_id for series_id, _short in CREDIT_BROAD_TILES)]
    observations = _guarded(conn, "observations", lambda: read_models.recent_observations(conn, series_ids, limit=CALENDAR_LOOKBACK_OBSERVATIONS), {}, errors)
    metrics = _guarded(conn, "metrics", lambda: read_models.metric_latest(conn), {}, errors)
    slope_history = _guarded(conn, "slope_history", lambda: read_models.metric_history(conn, SLOPE_10Y2Y_METRIC, limit=CALENDAR_LOOKBACK_OBSERVATIONS), [], errors)
    fly_history = _guarded(conn, "fly_history", lambda: read_models.metric_history(conn, FLY_2S5S10S_METRIC, limit=CALENDAR_LOOKBACK_OBSERVATIONS), [], errors)
    vol = _guarded(conn, "yahoo_vol", lambda: read_models.yahoo_vol_core(conn), {}, errors)
    bars = _guarded(conn, "yahoo_cross_asset", lambda: yahoo_cross_asset_bars(conn, since=since), [], errors)
    monitor_source = "MARKET_MONITOR_EOD (Yahoo adjusted closes)"

    sections = [
        _section(
            SECTION_US_INDEXES,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="price",
            rows=_monitor_rows(monitor, US_INDEX_ROWS, today=reference, source=monitor_source),
            risk=True,
            source=monitor_source,
            notes=(
                "Returns are stored trading sessions (1D = 1, 1W = 5, 1M = 21, 3M = 63) on Yahoo adjusted closes, the same windows as US Markets.",
                RISK_METHODOLOGY,
            ),
        ),
        _section(
            SECTION_MARKET_RATIOS,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="ratio",
            rows=_ratio_rows(monitor, today=reference),
            risk=False,
            source=monitor_source,
            notes=("Level is the point-in-time adjusted price ratio (numerator / SPY). Changes are the session-window change of that ratio; rising means the numerator outperformed SPY.",),
        ),
        _section(
            SECTION_SECTORS,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="price",
            rows=_sector_rows(closes, panel, today=reference),
            risk=True,
            source="EQUITY_EOD adjusted closes on the shared SPY session calendar",
            notes=(
                "Sector ETF return between the shared EQUITY_EOD SPY session endpoints, the absolute mode of the US Markets sector heatmap. Select a row to open that sector's subsector heatmap.",
                RISK_METHODOLOGY,
            ),
        ),
        _section(
            SECTION_YIELD_CURVE,
            change_columns=LONG_WINDOWS,
            change_kind="bps",
            level_kind="yield_pct",
            rows=[
                *_yield_rows(rates, observations, today=reference),
                *_metric_rows(
                    (
                        ("2s10s", "2s10s", "Derived curve slope (bps)", metrics.get(SLOPE_10Y2Y_METRIC), slope_history),
                        ("2s5s10s", "2s5s10s", "Derived curve butterfly (bps)", metrics.get(FLY_2S5S10S_METRIC), fly_history),
                    ),
                    today=reference,
                ),
                _move_row(bars, today=reference),
            ],
            risk=False,
            source="Treasury / FRED par yields; Yahoo ^MOVE",
            notes=(
                "Yields in percent; yield, slope, and butterfly moves in basis points (1D vs prior observation; 1W = 7 calendar days; 1M-1Y = calendar months, same anchors as Rates & Curve). The Excel export writes the template's %Change as the fractional change of each level.",
                "2s10s = 10Y minus 2Y; 2s5s10s = 2x5Y minus 2Y minus 10Y, both in basis points on a common observation date.",
                "MOVE is an index level in points, not a yield; its moves are percent changes over provider observations.",
            ),
            export_change_kind="fraction",
        ),
        _section(
            SECTION_CREDIT,
            change_columns=LONG_WINDOWS,
            change_kind="bps",
            level_kind="bps",
            rows=_credit_rows(credit, observations, today=reference),
            risk=False,
            source="FRED / ICE BofA option-adjusted spreads",
            notes=("OAS levels and changes in basis points. 1D-3M are the stored Credit page metrics; 6M and 1Y use the same calendar anchors on the stored observations.",),
        ),
        _section(
            SECTION_VIX_TERM,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="index",
            rows=_vix_rows(vol, today=reference),
            risk=False,
            source="YAHOO_VOL (^VIX, ^VIX3M, ^VIX6M, ^VIX1Y closes)",
            notes=("Index levels in volatility points. Changes are percent moves over 1, 5, 21, and 63 stored observations.",),
        ),
        _section(
            SECTION_GLOBAL,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="price",
            rows=_monitor_rows(monitor, GLOBAL_ROWS, today=reference, source=monitor_source),
            risk=True,
            source=monitor_source,
            notes=("USD-listed ETF proxies; returns are what a USD investor experienced. Session windows match Global Markets.", RISK_METHODOLOGY),
        ),
        _section(
            SECTION_COMMODITIES,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="price",
            rows=_cross_asset_rows(
                bars,
                COMMODITY_ROWS,
                columns=SHORT_WINDOWS,
                today=reference,
                source="YAHOO_FUTURES_PROXY front-month closes",
                risk=True,
            ),
            risk=True,
            source="YAHOO_FUTURES_PROXY (provider-maintained rolling front-month proxies)",
            notes=("Futures proxies, not official continuous settlements. Returns count provider daily observations (1, 5, 21, 63), as on Commodities.", RISK_METHODOLOGY),
        ),
        _section(
            SECTION_FOREX,
            change_columns=LONG_WINDOWS,
            change_kind="fraction",
            level_kind="fx",
            rows=_cross_asset_rows(
                bars,
                FX_PAIRS,
                columns=LONG_WINDOWS,
                today=reference,
                source="YAHOO_FX daily closes",
                risk=False,
                note_by_key={"USDCNH": USDCNH_SOURCE_NOTE},
            ),
            risk=False,
            source="YAHOO_FX (CNH via CME CNH=F futures proxy)",
            notes=(
                "Quoted-pair convention: a positive return means the base currency appreciated against the quote currency (USD/JPY up = USD stronger). The FOREX page's 'vs USD' view inverts USD-base pairs; the pair levels here are not inverted.",
                USDCNH_SOURCE_NOTE,
            ),
        ),
        _section(
            SECTION_CRYPTO,
            change_columns=SHORT_WINDOWS,
            change_kind="fraction",
            level_kind="price",
            rows=_crypto_rows(bars, today=reference),
            risk=True,
            source="YAHOO_CRYPTO (UTC calendar days)",
            notes=("Crypto trades every day: 1D/1W/1M/3M are 1, 7, 30, and 90 calendar days, as on the Crypto page. Risk statistics use 90 daily observations annualized with 365.",),
        ),
    ]
    rows_total = sum(len(section["rows"]) for section in sections)
    missing = sum(1 for section in sections for row in section["rows"] if row["status"] == "missing")
    stale = sum(1 for section in sections for row in section["rows"] if row["status"] == "stale")
    dates = sorted({row["as_of"] for section in sections for row in section["rows"] if row.get("as_of")})
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": _snapshot_id(sections),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reference_date": reference.isoformat(),
        "sections": sections,
        "as_of_min": dates[0] if dates else None,
        "as_of_max": dates[-1] if dates else None,
        "rows_total": rows_total,
        "rows_missing": missing,
        "rows_stale": stale,
        "read_errors": errors,
        "stale_after_days": STALE_AFTER_DAYS,
        "missing_policy": "A missing or unavailable value is left blank (None in the snapshot, an empty cell in Excel). It is never written as zero.",
    }


def section_by_id(snapshot: Mapping[str, Any], section_id: str) -> dict[str, Any] | None:
    for section in snapshot.get("sections") or []:
        if section.get("section_id") == section_id:
            return section
    return None


__all__ = [
    "CRYPTO_TEMPLATE_WINDOWS",
    "LONG_WINDOWS",
    "SECTION_ORDER",
    "SHORT_WINDOWS",
    "SNAPSHOT_VERSION",
    "STALE_AFTER_DAYS",
    "SUBSECTOR_GROUP_BY_SECTOR",
    "overview_snapshot",
    "section_by_id",
]
