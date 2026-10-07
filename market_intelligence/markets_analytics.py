"""Canonical analytics for the US Markets and Global Markets pages.

Two different relative-strength quantities are used and must not be mixed:

* A point-in-time price ratio (``normalized_ratio``) is the leadership chart.
  ``ratio_t = adjusted_close_asset / adjusted_close_benchmark``, then rebased
  to 100 on the first common date. A rising line means the numerator
  outperformed the benchmark over the displayed interval.
* A windowed ratio change (``rs_chg_*`` on sector snapshots, from
  ``baskets.ratio_change_rs``) is ``(P_t/B_t) / (P_prev/B_prev) - 1`` across a
  fixed session window. Sector relative-strength bars use that stored field.

Drawdown here is a per-session trailing path for the dashboard. It is not the
snapshot scalar ``max_drawdown_252d``.

Session windows match ``equity_eod.RETURN_WINDOWS`` (1, 5, 21, 63, 126, 252).
This module does not import the equity ingest package, so Streamlit can import
it without loading a writer or a provider client.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from market_intelligence.baskets import BASKET_METHOD_VERSION, daily_rebalanced_equal_weight, window_return
from market_intelligence.calendars import skipped_session
from market_intelligence.live_session import equal_dollar_live_return
from market_intelligence.return_policy import BASIS_LABELS, BASIS_LAST_CLOSE, BASIS_SESSION_OPEN, last_updated_label
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.taxonomy import (
    BENCHMARK_SPY,
    SECTOR_PROXIES,
    canonical_basket_sector,
    constituent_company_name,
    cross_sector_themes,
    stock_subsector_baskets,
)

DRAWDOWN_SESSIONS = 252
MIN_SUBSECTOR_CONSTITUENTS = 2
# Live 1D legs whose reference is one named session (open or completed close).
# "RTH_OPEN" is the legacy name for SESSION_OPEN kept for stored callers.
QUOTE_POLICY_BASES = frozenset({"RTH_OPEN", BASIS_SESSION_OPEN, BASIS_LAST_CLOSE})
_HEAT_RED = (179, 64, 64)
_HEAT_NEUTRAL = (44, 48, 54)
_HEAT_GREEN = (61, 140, 90)

# Labels, snapshot field, stored-session count. Counts must match RETURN_WINDOWS.
HORIZONS: tuple[tuple[str, str, int], ...] = (
    ("1D", "ret_1d", 1),
    ("1W", "ret_1w", 5),
    ("1M", "ret_1m", 21),
    ("3M", "ret_3m", 63),
    ("6M", "ret_6m", 126),
    ("1Y", "ret_12m", 252),
)
RS_HORIZONS: tuple[tuple[str, str], ...] = (
    ("1D", "rs_chg_1d"),
    ("1W", "rs_chg_1w"),
    ("1M", "rs_chg_1m"),
    ("3M", "rs_chg_3m"),
    ("6M", "rs_chg_6m"),
    ("1Y", "rs_chg_12m"),
)
HORIZON_SESSIONS: dict[str, int] = {label: sessions for label, _field, sessions in HORIZONS}
HORIZON_RETURN_FIELD: dict[str, str] = {label: field for label, field, _sessions in HORIZONS}
HORIZON_RS_FIELD: dict[str, str] = {label: field for label, field in RS_HORIZONS}

LIVE_RETURN_STATE = (
    "Live 1D from stored IBKR quotes: current price divided by the latest regular-session open. A missing open stays blank. vs Prior Close is not labeled 1D. "
    "A Yahoo fallback is labeled as stored live quotes, not as IBKR."
)
EOD_RETURN_STATE = (
    "Finalized EOD 1D. Live quotes are unavailable or incomplete, so every sector uses the stored EOD return."
)
EOD_WINDOW_STATE = "Finalized EOD · {horizon} · {sessions} stored sessions."
LIVE_RS_STATE = (
    "Live 1D relative strength from stored quotes, using the same ratio-change formula as rs_chg_1d "
    "when the asset and SPY sessions align."
)
EOD_RS_STATE = (
    "Finalized EOD relative strength: (P_t/SPY_t) / (P_prev/SPY_prev) - 1 over the aligned session window. "
    "This is not a difference of the two returns."
)

US_METHODOLOGY: tuple[str, ...] = (
    "ETF proxies: SPY (S&P 500), QQQ (Nasdaq-100), IWM (Russell 2000), DIA (Dow Jones Industrial Average), RSP (S&P 500 equal weight). These are current investable proxies, not a point-in-time or survivorship-free universe.",
    "Index charts, ratios, and snapshot badges use Yahoo MARKET_MONITOR_EOD adjusted closes from mi_v_market_monitor_closes. Sector ETF returns use those Yahoo closes when SPY and every sector ETF are at least as new as EQUITY_EOD; otherwise the sector rows stay on EQUITY_EOD. Stock baskets stay on EQUITY_EOD. Those sources are not spliced. Relative sector performance subtracts the SPY return from the same source as the sector rows. Raw closes are not mixed into either return. Each symbol keeps one stored provider. Yahoo rows keep SPLIT_ADJUSTED_UNKNOWN_DIVIDEND when that is the stored basis. IBKR rows keep their own basis.",
    "Normalized performance rebases every selected series to 100 on the first date inside From/To where all selected series have a valid adjusted close. Changing the selection recomputes that date. Later missing sessions stay missing. Nothing is forward-filled.",
    "Relative leadership is the point-in-time price ratio (asset / SPY). Indexed mode rebases that ratio to 100 at the start of the selected interval. Absolute mode shows the raw price ratio. The ratio of two already-indexed price series is not used. Rising means the numerator outperformed SPY. It is not the windowed `rs_chg_*` ratio change.",
    "Return windows are stored trading sessions: 1D = 1, 1W = 5, 1M = 21, 3M = 63, 6M = 126, 1Y = 252. They are not calendar-day offsets. Header badges, the sector heatmap, and subsector returns share these windows.",
    "Sector ETFs follow the repository taxonomy: XLK Technology, XLF Financials, XLI Industrials, XLY Consumer Discretionary, XLC Communication Services, XLV Health Care, XLP Consumer Staples, XLE Energy, XLU Utilities, XLRE Real Estate, XLB Materials. The canonical Technology label is the Information Technology sector.",
    "Sector heatmap absolute mode is the sector ETF return between the shared SPY session endpoints for that horizon. When Yahoo market-monitor history is current, those endpoints are MARKET_MONITOR_EOD; otherwise they are EQUITY_EOD. Relative vs SPY subtracts that same SPY return, in percentage points. It is not `rs_chg_*` and it is not a ratio of the two percentage returns. Each horizon column has its own symmetric color scale. A sector ETF that misses either endpoint, or that uses a different adjustment basis than SPY, is N/A. Stored sector snapshots are not a substitute.",
    "Subsector rows are curated current-context baskets, not official GICS industries and not industry ETFs. Cross-sector themes are omitted. The only basket method is equal-dollar daily rebalancing (`equal_dollar_daily_rebalance_v1`): each horizon rebuilds that index from members that have the shared start session and the shared SPY endpoint, then takes the index window return. Fewer than 2 such members is N/A. The count is those endpoint-eligible names. Each daily step includes only members with a price on that session and on the previous session inside the window. A calendar session with no observable member return makes the horizon N/A. The index does not skip that session or carry it as a zero return. This is not the average of each name's holding-period return, and a stored snapshot is not used when constituent prices are missing. A missing price is not zero.",
    "Index snapshot badges stay on finalized EOD session returns. Live 1D from an IBKR quote is current price divided by the latest regular-session open, and it replaces a heatmap cell only when every contributor shares that open session. A relative cell also requires SPY on that same open. A missing open keeps the EQUITY_EOD 1D pair. Delayed and frozen quotes are labeled and are not called live. Longer horizons stay EQUITY_EOD. Weighting stays equal-dollar daily rebalance. Streamlit reads the shared quote cache and does not open TWS.",
)

GLOBAL_METHODOLOGY: tuple[str, ...] = (
    "USD-listed ETF proxies: SPY United States, VEA Developed ex-US, VGK Europe, EWJ Japan, VWO Emerging Markets, MCHI China, INDA India, EWZ Brazil. One broad ETF per region. Returns are what a USD investor experienced in that ETF, including currency effects embedded in the USD share price.",
    "Local-index comparison (local currency versus USD ETF) is deferred. Free index history was not added as a second mode because a partial or unreliable ticker map would overstate coverage.",
    "Adjusted closes are Yahoo MARKET_MONITOR_EOD from mi_v_market_monitor_closes, the same source as the US index charts. They are not EQUITY_EOD and they are not spliced with it. History starts at the fund's first stored session. Nothing is fabricated before inception, and symbols are not required to share a start date.",
    "Return windows are stored trading sessions: 1D = 1, 1W = 5, 1M = 21, 3M = 63, 6M = 126, 1Y = 252.",
    "Regional performance is rebased to 100 on the first common valid date of the series currently selected, inside From/To. The default selection is United States, Developed ex-US, Europe, Japan, and Emerging Markets.",
    "VEA/SPY and VWO/SPY are point-in-time price ratios rebased to 100. Rising means that international ETF outperformed SPY. They are not windowed `rs_chg_*` values and they are not a bullish or bearish label.",
    "These pages monitor current benchmark proxies. They are not a research backtest and they do not change point-in-time universe methodology.",
)


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_day(value: Any) -> date | None:
    """Date-only parsing. A `YYYY-MM-DD` string is not shifted through a timezone."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            try:
                return date.fromisoformat(text[:10])
            except ValueError:
                return None
    return None


def _price_map(points: Sequence[tuple[date, float | None] | Mapping[str, Any]]) -> dict[date, float]:
    out: dict[date, float] = {}
    for item in points:
        if isinstance(item, Mapping):
            day = as_day(item.get("date") if "date" in item else item.get("as_of"))
            number = _finite(item.get("value"))
        else:
            day = item[0]
            number = _finite(item[1])
        if day is None or number is None or number == 0:
            continue
        out[day] = number
    return out


def clip_points(
    points: Sequence[tuple[date, float]],
    *,
    start: date | None,
    end: date | None,
) -> list[tuple[date, float]]:
    if start is None or end is None or start > end:
        return []
    return [(day, value) for day, value in points if start <= day <= end]


def normalize_selected_to_100(
    series: Mapping[str, Sequence[tuple[date, float | None] | Mapping[str, Any]]],
    selected: Sequence[str],
) -> dict[str, Any]:
    """Rebase every selected series to 100 on their first common valid date.

    Dates before that common start are dropped. A later session missing on one
    series is omitted for that series only. Nothing is forward-filled.
    """
    chosen = [symbol for symbol in selected if symbol]
    maps = {symbol: _price_map(series.get(symbol) or ()) for symbol in chosen}
    empty = {"start": None, "series": {symbol: [] for symbol in chosen}}
    if not chosen or any(not maps[symbol] for symbol in chosen):
        return empty
    shared = set.intersection(*(set(maps[symbol]) for symbol in chosen))
    if not shared:
        return empty
    start = min(shared)
    scaled: dict[str, list[tuple[date, float]]] = {}
    for symbol in chosen:
        base = maps[symbol][start]
        scaled[symbol] = [
            (day, 100.0 * maps[symbol][day] / base)
            for day in sorted(maps[symbol])
            if day >= start
        ]
    return {"start": start, "series": scaled}


def price_ratio_points(
    asset: Sequence[tuple[date, float | None] | Mapping[str, Any]],
    benchmark: Sequence[tuple[date, float | None] | Mapping[str, Any]],
) -> list[tuple[date, float]]:
    """Point-in-time adjusted price ratio. Not a windowed ratio change."""
    left = _price_map(asset)
    right = _price_map(benchmark)
    points: list[tuple[date, float]] = []
    for day in sorted(set(left) & set(right)):
        bench = right[day]
        if bench == 0:
            continue
        points.append((day, left[day] / bench))
    return points


def normalized_ratio(
    asset: Sequence[tuple[date, float | None] | Mapping[str, Any]],
    benchmark: Sequence[tuple[date, float | None] | Mapping[str, Any]],
) -> dict[str, Any]:
    """Price ratio rebased to 100 on the first date both prices exist.

    Rising means the asset outperformed the benchmark. This is not `rs_chg_*`.
    """
    ratios = price_ratio_points(asset, benchmark)
    if not ratios or ratios[0][1] == 0:
        return {"start": None, "points": []}
    base = ratios[0][1]
    return {
        "start": ratios[0][0],
        "points": [(day, 100.0 * value / base) for day, value in ratios],
    }


def trailing_drawdown(
    points: Sequence[tuple[date, float | None] | Mapping[str, Any]],
    *,
    sessions: int = DRAWDOWN_SESSIONS,
) -> list[tuple[date, float]]:
    """Trailing drawdown versus the max adjusted close of ``sessions`` stored observations.

    Null prices are omitted and do not count as sessions. The path stays missing
    until ``sessions`` valid observations exist. The result is never positive.
    """
    if sessions < 1:
        return []
    valid: list[tuple[date, float]] = []
    for item in points:
        if isinstance(item, Mapping):
            day = as_day(item.get("date") if "date" in item else item.get("as_of"))
            number = _finite(item.get("value"))
        else:
            day = item[0]
            number = _finite(item[1])
        if day is None or number is None:
            continue
        valid.append((day, number))
    valid.sort(key=lambda item: item[0])
    out: list[tuple[date, float]] = []
    for index in range(sessions - 1, len(valid)):
        window = valid[index - sessions + 1 : index + 1]
        peak = max(price for _day, price in window)
        price = valid[index][1]
        if peak == 0:
            continue
        out.append((valid[index][0], min(price / peak - 1.0, 0.0)))
    return out


def session_window_returns(series: Mapping[date, float], as_of: date) -> dict[str, float | None]:
    """Canonical session returns for 1D, 1W, 1M, 3M, 6M, and 1Y."""
    return {label: window_return(series, as_of, sessions) for label, sessions in HORIZON_SESSIONS.items()}


def bars_to_series(points: Sequence[Mapping[str, Any]]) -> dict[date, float]:
    mapped = _price_map(points)
    return mapped


def live_field_complete(rows: Sequence[Mapping[str, Any]], field: str) -> bool:
    if not rows:
        return False
    for row in rows:
        metrics = row.get("metrics") or {}
        if _finite(metrics.get(field)) is None:
            return False
    return True


def sector_bar_pairs(
    rows: Sequence[Mapping[str, Any]],
    horizon: str,
    *,
    kind: str,
) -> tuple[list[tuple[str, float]], str]:
    """Rank inputs and an honest live-versus-EOD label.

    ``kind`` is ``return`` or ``rs``. Live 1D is used only when every row has
    a finite live value. Otherwise every row uses the finalized EOD field.
    Missing values are omitted, never drawn as zero.
    """
    if kind == "rs":
        eod_field = HORIZON_RS_FIELD.get(horizon)
        live_field = "live_rs_chg_1d"
        live_state = LIVE_RS_STATE
        eod_state = EOD_RS_STATE
    elif kind == "return":
        eod_field = HORIZON_RETURN_FIELD.get(horizon)
        live_field = "live_ret_1d"
        live_state = LIVE_RETURN_STATE
        eod_state = EOD_RETURN_STATE
    else:
        raise ValueError("kind must be return or rs")
    if eod_field is None:
        return [], "Unknown horizon."
    use_live = horizon == "1D" and live_field_complete(rows, live_field)
    field = live_field if use_live else eod_field
    if use_live:
        state = live_state
    elif horizon == "1D":
        state = eod_state
    else:
        state = EOD_WINDOW_STATE.format(horizon=horizon, sessions=HORIZON_SESSIONS[horizon])
    pairs: list[tuple[str, float]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        number = _finite(metrics.get(field))
        if number is None:
            continue
        label = str(row.get("canonical_sector") or row.get("sector_key") or row.get("label") or "")
        if not label:
            continue
        pairs.append((label, number))
    return pairs, state


def heatmap_rows(
    order: Sequence[tuple[str, str]],
    returns_by_symbol: Mapping[str, Mapping[str, float | None]],
) -> dict[str, Any]:
    """Fixed row order. Cell values are fractional returns or null. Not sorted."""
    columns = [label for label, _field, _sessions in HORIZONS]
    rows: list[dict[str, Any]] = []
    for symbol, label in order:
        values = returns_by_symbol.get(symbol) or {}
        rows.append({"symbol": symbol, "label": label, "values": [values.get(column) for column in columns]})
    return {"columns": columns, "rows": rows}


def classify_return(value: Any) -> str:
    """positive, negative, neutral, or unavailable. Zero is neutral, not missing."""
    number = _finite(value)
    if number is None:
        return "unavailable"
    if number > 0:
        return "positive"
    if number < 0:
        return "negative"
    return "neutral"


def normalize_to_100(levels: Sequence[Any]) -> list[float | None]:
    """Scale a single series so its first finite observation is 100.

    Later missing values stay missing. A zero base cannot be a start and stays missing.
    """
    base: float | None = None
    out: list[float | None] = []
    for value in levels:
        number = _finite(value)
        if number is None:
            out.append(None)
            continue
        if base is None:
            if number == 0:
                out.append(None)
                continue
            base = number
        out.append(100.0 * number / base)
    return out


def return_spread(asset: Any, benchmark: Any) -> float | None:
    """Percentage-point spread. Missing on either side stays missing, never zero."""
    left = _finite(asset)
    right = _finite(benchmark)
    if left is None or right is None:
        return None
    return left - right


def column_color_scale(values: Sequence[Any]) -> dict[str, Any]:
    """Symmetric scale around zero for one horizon column.

    ``max_abs`` is zero when the column is empty or every finite value is zero,
    so callers do not divide by that range.
    """
    finite = [number for number in (_finite(value) for value in values) if number is not None]
    if not finite:
        return {"max_abs": 0.0, "min": None, "max": None, "degenerate": True}
    low = min(finite)
    high = max(finite)
    max_abs = max(abs(low), abs(high))
    return {"max_abs": max_abs, "min": low, "max": high, "degenerate": max_abs <= 1e-12}


def column_color_scales(matrix: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    """One scale per column. A wide 1Y range does not flatten 1D."""
    if not matrix:
        return []
    width = max((len(row) for row in matrix), default=0)
    columns: list[list[Any]] = []
    for index in range(width):
        columns.append([row[index] if index < len(row) else None for row in matrix])
    return [column_color_scale(column) for column in columns]


def _lerp(start: tuple[int, int, int], end: tuple[int, int, int], weight: float) -> tuple[int, int, int]:
    bounded = max(0.0, min(1.0, weight))
    return tuple(int(round(left + (right - left) * bounded)) for left, right in zip(start, end))


def _rgb(color: tuple[int, int, int]) -> str:
    return "rgb({0}, {1}, {2})".format(color[0], color[1], color[2])


def heatmap_cell_color(value: Any, max_abs: float) -> str:
    """Map one cell onto its column scale. Missing and a zero range stay neutral."""
    number = _finite(value)
    if number is None or not math.isfinite(max_abs) or max_abs <= 1e-12:
        return _rgb(_HEAT_NEUTRAL)
    weight = max(-1.0, min(1.0, number / max_abs))
    if weight >= 0:
        return _rgb(_lerp(_HEAT_NEUTRAL, _HEAT_GREEN, weight))
    return _rgb(_lerp(_HEAT_NEUTRAL, _HEAT_RED, -weight))


_HEAT_AMBER = (191, 134, 46)


def magnitude_color_scale(values: Sequence[Any]) -> dict[str, Any]:
    """Sequential scale for a non-negative magnitude column such as volatility.

    Colour runs from neutral at the column minimum to amber at the column
    maximum. It is not green/red: a higher volatility is not "better" and must
    not borrow the return palette. A single-valued column is degenerate.
    """
    finite = [number for number in (_finite(value) for value in values) if number is not None]
    if not finite:
        return {"min": None, "max": None, "degenerate": True}
    low = min(finite)
    high = max(finite)
    return {"min": low, "max": high, "degenerate": (high - low) <= 1e-12}


def magnitude_cell_color(value: Any, scale: Mapping[str, Any]) -> str:
    """Neutral to amber by position inside the column's [min, max]. Missing stays neutral."""
    number = _finite(value)
    low = _finite(scale.get("min"))
    high = _finite(scale.get("max"))
    if number is None or low is None or high is None or bool(scale.get("degenerate")):
        return _rgb(_HEAT_NEUTRAL)
    weight = (number - low) / (high - low)
    return _rgb(_lerp(_HEAT_NEUTRAL, _HEAT_AMBER, weight))


def running_peak_drawdown(prices: Sequence[Any]) -> list[float | None]:
    """Drawdown versus the running peak: price / peak - 1. Never positive.

    This is the definition used inside the trailing 252-session window. A missing
    price stays missing and does not move the peak.
    """
    peak: float | None = None
    out: list[float | None] = []
    for price in prices:
        number = _finite(price)
        if number is None:
            out.append(None)
            continue
        peak = number if peak is None else max(peak, number)
        if peak == 0:
            out.append(None)
            continue
        out.append(min(number / peak - 1.0, 0.0))
    return out


def period_change(points: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """Latest level and change from the first visible point. No fabricated start."""
    if not points:
        return {"latest": None, "change": None, "start": None, "end": None}
    start_day, start_value = points[0]
    end_day, end_value = points[-1]
    change = None
    if len(points) > 1 and start_value not in (0, None):
        change = end_value / start_value - 1.0
    return {"latest": end_value, "change": change, "start": start_day, "end": end_day}


def sector_heatmap_matrix(
    rows: Sequence[Mapping[str, Any]],
    spy_returns: Mapping[str, Any] | None,
    *,
    mode: str,
) -> dict[str, Any]:
    """Canonical sector rows by horizon. Relative mode is a return spread versus SPY."""
    if mode not in {"absolute", "relative"}:
        raise ValueError("mode must be absolute or relative")
    columns = [label for label, _field, _sessions in HORIZONS]
    by_name: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        name = str(row.get("canonical_sector") or row.get("sector_key") or "")
        if name:
            by_name[name] = row
    matrix_rows: list[dict[str, Any]] = []
    for name in CANONICAL_SECTORS:
        metrics = (by_name.get(name) or {}).get("metrics") or {}
        symbol = str((by_name.get(name) or {}).get("instrument_id") or SECTOR_PROXIES.get(name) or "")
        values: list[float | None] = []
        for label, field, _sessions in HORIZONS:
            asset = _finite(metrics.get(field))
            if mode == "relative":
                values.append(return_spread(asset, (spy_returns or {}).get(label)))
            else:
                values.append(asset)
        matrix_rows.append({"label": name, "symbol": symbol, "values": values})
    return {
        "columns": columns,
        "rows": matrix_rows,
        "scales": column_color_scales([row["values"] for row in matrix_rows]),
        "mode": mode,
    }


def series_prices(record: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> dict[date, float]:
    """Adjusted closes keyed by session. A zero price cannot be a return base."""
    if record is None:
        return {}
    if isinstance(record, Mapping) and "prices" in record:
        mapped: dict[date, float] = {}
        for key, value in (record.get("prices") or {}).items():
            day = as_day(key)
            number = _finite(value)
            if day is None or number is None or number == 0:
                continue
            mapped[day] = number
        return mapped
    if isinstance(record, Mapping) and ("bars" in record or "date" in record or "value" in record):
        bars = record.get("bars")
        if bars is None and ("date" in record or "as_of" in record):
            return bars_to_series((record,))
        return bars_to_series(bars or ())
    if isinstance(record, Sequence) and not isinstance(record, (str, bytes)):
        return bars_to_series(record)
    return {}


def series_basis(record: Mapping[str, Any] | None) -> str | None:
    if not isinstance(record, Mapping):
        return None
    basis = record.get("adjustment_basis")
    if basis is None:
        return None
    text = str(basis).strip()
    return text or None


def shared_session_bounds(calendar: Sequence[date], sessions: int) -> tuple[date, date] | None:
    """Start and end sessions on one explicit calendar. Not each symbol's own last date.

    Every adjacent step in the selected window must be the next NYSE session.
    A missing ordinary weekday rejects the horizon. A weekend or holiday does not.
    """
    if sessions < 1 or len(calendar) <= sessions:
        return None
    window = list(calendar[-(sessions + 1) :])
    if len(window) != sessions + 1:
        return None
    if any(skipped_session(prev, day) for prev, day in zip(window, window[1:])):
        return None
    return window[0], window[-1]


def endpoint_session_return(prices: Mapping[date, float], start: date, end: date) -> float | None:
    """Price at the shared end divided by price at the shared start, minus one."""
    left = prices.get(start)
    right = prices.get(end)
    if left is None or right is None or left == 0:
        return None
    return right / left - 1.0


def snapshot_rejection_reason(row: Mapping[str, Any], *, endpoint: date, adjustment_basis: str) -> str:
    """Why a stored snapshot cannot fill a heatmap cell.

    A matching date and basis still does not make the snapshot canonical.
    The heatmap uses prices on the shared sessions, not ``ret_*`` fields.
    """
    as_of = as_day(row.get("as_of"))
    if as_of is None:
        return "missing_endpoint"
    if as_of != endpoint:
        return "stale_endpoint"
    coverage = row.get("coverage") or {}
    basis = row.get("adjustment_basis") or coverage.get("adjustment_basis")
    if not basis or str(basis) != adjustment_basis:
        return "incompatible_adjustment_basis"
    return "snapshot_not_canonical"


def rebalanced_basket_return(
    member_prices: Mapping[str, Mapping[date, float]],
    calendar: Sequence[date],
    start: date,
    end: date,
    *,
    minimum: int = MIN_SUBSECTOR_CONSTITUENTS,
) -> float | None:
    """Equal-dollar daily-rebalanced index return between two shared sessions.

    Members must already be limited to names with prices on both sessions.
    This is not the mean of each name's start-to-end return.
    """
    if len(member_prices) < minimum:
        return None
    sessions = sorted(day for day in calendar if start <= day <= end)
    if len(sessions) < 2 or sessions[0] != start or sessions[-1] != end:
        return None
    if any(skipped_session(prev, day) for prev, day in zip(sessions, sessions[1:])):
        return None
    allowed = set(sessions)
    restricted = {
        symbol: {day: price for day, price in prices.items() if day in allowed}
        for symbol, prices in member_prices.items()
    }
    index = daily_rebalanced_equal_weight(restricted, start=start, end=end, calendar=sessions)
    if [point.as_of for point in index] != sessions:
        return None
    levels = {point.as_of: point.level for point in index}
    base = levels.get(start)
    last = levels.get(end)
    if base is None or last is None or base == 0:
        return None
    return last / base - 1.0


def _empty_aligned_panel(reason: str) -> dict[str, Any]:
    columns = [label for label, _field, _sessions in HORIZONS]
    return {
        "available": False,
        "reason": reason,
        "method": BASKET_METHOD_VERSION,
        "source_id": "EQUITY_EOD",
        "endpoint": None,
        "adjustment_basis": None,
        "provider": None,
        "windows": {label: None for label in columns},
        "spy_returns": {label: None for label in columns},
        "sectors": [],
        "subsectors": {},
        "themes_omitted": [basket.label for basket in cross_sector_themes()],
    }


def _symbol_status(prices: Mapping[date, float], basis: str | None, *, endpoint: date, benchmark_basis: str) -> str | None:
    if endpoint not in prices:
        return "stale_or_missing_endpoint"
    if not basis or basis != benchmark_basis:
        return "incompatible_adjustment_basis"
    return None


def build_aligned_us_panel(
    records: Mapping[str, Mapping[str, Any]],
    *,
    minimum: int = MIN_SUBSECTOR_CONSTITUENTS,
    baskets: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Sector, curated-basket, and SPY returns on one EQUITY_EOD session calendar.

    The endpoint is SPY's latest stored session. Every horizon uses that date
    and the SPY session ``N`` observations earlier. A symbol that does not
    print the endpoint, or whose adjustment basis differs from SPY, is rejected.
    Stored snapshots are not consulted.
    """
    spy_record = records.get(BENCHMARK_SPY) or {}
    spy_prices = series_prices(spy_record)
    spy_basis = series_basis(spy_record)
    if not spy_prices or not spy_basis:
        return _empty_aligned_panel("missing_spy_endpoint")
    calendar = sorted(spy_prices)
    endpoint = calendar[-1]
    columns = [label for label, _field, _sessions in HORIZONS]
    windows: dict[str, dict[str, date] | None] = {}
    spy_returns: dict[str, float | None] = {}
    for label, _field, sessions in HORIZONS:
        bounds = shared_session_bounds(calendar, sessions)
        if bounds is None:
            windows[label] = None
            spy_returns[label] = None
            continue
        start, end = bounds
        windows[label] = {"start": start, "end": end}
        spy_returns[label] = endpoint_session_return(spy_prices, start, end)
    prepared = {
        symbol: {"prices": series_prices(record), "basis": series_basis(record)}
        for symbol, record in records.items()
    }
    sectors: list[dict[str, Any]] = []
    for name in CANONICAL_SECTORS:
        symbol = SECTOR_PROXIES[name]
        item = prepared.get(symbol) or {"prices": {}, "basis": None}
        status = _symbol_status(item["prices"], item["basis"], endpoint=endpoint, benchmark_basis=spy_basis)
        values: list[float | None] = []
        notes: list[str | None] = []
        for label in columns:
            bounds = windows[label]
            if status is not None or bounds is None:
                values.append(None)
                notes.append("Stale or missing endpoint" if status == "stale_or_missing_endpoint" else "Incompatible adjustment basis" if status == "incompatible_adjustment_basis" else "Missing session")
                continue
            number = endpoint_session_return(item["prices"], bounds["start"], bounds["end"])
            values.append(number)
            notes.append(None if number is not None else "Missing session")
        sectors.append({"label": name, "symbol": symbol, "values": values, "notes": notes})
    subsectors: dict[str, list[dict[str, Any]]] = {}
    selected_baskets = stock_subsector_baskets() if baskets is None else tuple(baskets)
    for basket in selected_baskets:
        parent = canonical_basket_sector(str(basket.parent_sector))
        if parent is None:
            continue
        member_records = []
        for symbol in basket.members:
            item = prepared.get(symbol) or {"prices": {}, "basis": None}
            member_records.append((symbol, item["prices"], item["basis"]))
        values = []
        counts = []
        for label, _field, _sessions in HORIZONS:
            bounds = windows[label]
            if bounds is None:
                values.append(None)
                counts.append(0)
                continue
            start, end = bounds["start"], bounds["end"]
            eligible = {
                symbol: prices
                for symbol, prices, basis in member_records
                if _symbol_status(prices, basis, endpoint=endpoint, benchmark_basis=spy_basis) is None
                and start in prices
                and end in prices
            }
            counts.append(len(eligible))
            values.append(
                rebalanced_basket_return(eligible, calendar, start, end, minimum=minimum)
            )
        detail = []
        for symbol, prices, basis in sorted(member_records, key=lambda item: item[0]):
            status = _symbol_status(prices, basis, endpoint=endpoint, benchmark_basis=spy_basis)
            member_returns: dict[str, float | None] = {}
            for label in columns:
                bounds = windows[label]
                if status is not None or bounds is None:
                    member_returns[label] = None
                else:
                    member_returns[label] = endpoint_session_return(prices, bounds["start"], bounds["end"])
            detail.append(
                {
                    "symbol": symbol,
                    "company": constituent_company_name(symbol),
                    "returns": member_returns,
                    "included": status is None,
                    "rejection": status,
                    "quote_1d": "HISTORICAL",
                }
            )
        subsectors.setdefault(parent, []).append(
            {
                "industry": basket.label,
                "sector": parent,
                "classification": "curated_basket",
                "values": values,
                "counts": counts,
                "constituents": detail,
            }
        )
    for sector, rows in list(subsectors.items()):
        rows.sort(key=lambda row: str(row["industry"]))
        subsectors[sector] = rows
    return {
        "available": True,
        "reason": None,
        "method": BASKET_METHOD_VERSION,
        "source_id": "EQUITY_EOD",
        "endpoint": endpoint,
        "adjustment_basis": spy_basis,
        "provider": str((spy_record.get("provider") or "") or "") or None,
        "windows": windows,
        "spy_returns": spy_returns,
        "sectors": sectors,
        "subsectors": subsectors,
        "themes_omitted": [basket.label for basket in cross_sector_themes()],
    }


def _quote_text(value: Any) -> str:
    text = str(value or "").strip()
    return text


def _historical_quote_detail(panel: Mapping[str, Any], contributors: int | None) -> dict[str, Any]:
    window = (panel.get("windows") or {}).get("1D") or {}
    current = as_day(window.get("end") or panel.get("endpoint"))
    baseline = as_day(window.get("start"))
    return {
        "status": "HISTORICAL",
        "statuses": ["HISTORICAL"],
        "current_session": current.isoformat() if current else None,
        "baseline_session": baseline.isoformat() if baseline else None,
        "basis": panel.get("adjustment_basis"),
        "contributors": contributors,
        "updated": None,
    }


def _quote_leg(symbol: str, by_symbol: Mapping[str, Any], *, panel_basis: str | None) -> dict[str, Any] | None:
    """A live 1D leg is usable only with a session pair and the panel price basis."""
    row = by_symbol.get(symbol) or by_symbol.get(str(symbol).upper()) or {}
    if not isinstance(row, Mapping):
        return None
    current = row.get("current") or {}
    prior = row.get("prior_close") or {}
    if not isinstance(current, Mapping):
        current = {}
    if not isinstance(prior, Mapping):
        prior = {}
    status = str(current.get("market_data_status") or "HISTORICAL").upper()
    live = _finite(row.get("live_return"))
    if status == "HISTORICAL" or live is None:
        return None
    policy_basis = str(row.get("return_basis") or "")
    if policy_basis in QUOTE_POLICY_BASES:
        # Policy 1D: SESSION_OPEN pairs the price with the named session's open;
        # LAST_CLOSE pairs it with that session's completed close. Both legs of a
        # relative cell must share the basis and the reference session.
        session = as_day(row.get("session_open_date"))
        if session is None:
            return None
        updated = current.get("observation_ts")
        return {
            "live_return": live,
            "status": status,
            "current_session": session,
            "baseline_session": session,
            "basis": policy_basis,
            "updated": str(updated) if updated else None,
        }
    current_session = as_day(row.get("current_session") or current.get("session_date"))
    baseline_session = as_day(row.get("baseline_session") or row.get("prior_session") or prior.get("session_date"))
    basis = _quote_text(row.get("basis") or row.get("adjustment_basis") or prior.get("adjustment_basis"))
    if current_session is None or baseline_session is None or not basis or current_session <= baseline_session:
        return None
    if panel_basis and basis != panel_basis:
        return None
    updated = current.get("observation_ts")
    return {
        "live_return": live,
        "status": status,
        "current_session": current_session,
        "baseline_session": baseline_session,
        "basis": basis,
        "updated": str(updated) if updated else None,
    }


def _same_quote_period(legs: Sequence[Mapping[str, Any]]) -> bool:
    if not legs:
        return False
    first = legs[0]
    return all(
        leg.get("current_session") == first.get("current_session")
        and leg.get("baseline_session") == first.get("baseline_session")
        and leg.get("basis") == first.get("basis")
        for leg in legs
    )


def _quote_detail_from_legs(legs: Sequence[Mapping[str, Any]], contributors: int) -> dict[str, Any]:
    statuses: list[str] = []
    updated: str | None = None
    for leg in legs:
        status = str(leg.get("status") or "")
        if status and status not in statuses:
            statuses.append(status)
        stamp = leg.get("updated")
        if stamp and (updated is None or str(stamp) > updated):
            updated = str(stamp)
    first = legs[0]
    current = first.get("current_session")
    baseline = first.get("baseline_session")
    return {
        "status": statuses[0] if len(statuses) == 1 else "MIXED",
        "statuses": statuses or ["HISTORICAL"],
        "current_session": current.isoformat() if isinstance(current, date) else None,
        "baseline_session": baseline.isoformat() if isinstance(baseline, date) else None,
        "basis": first.get("basis"),
        "contributors": contributors,
        "updated": updated,
    }


def quote_periods_match(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    """True when two non-historical 1D cells share a session pair and price basis."""
    if not left or not right:
        return False
    if str(left.get("status") or "HISTORICAL") == "HISTORICAL":
        return False
    if str(right.get("status") or "HISTORICAL") == "HISTORICAL":
        return False
    return bool(
        left.get("current_session")
        and left.get("current_session") == right.get("current_session")
        and left.get("baseline_session")
        and left.get("baseline_session") == right.get("baseline_session")
        and left.get("basis")
        and left.get("basis") == right.get("basis")
    )


def _cell_source_note(detail: Mapping[str, Any] | None, *, count: int | None, relative: bool) -> str:
    status = str((detail or {}).get("status") or "HISTORICAL")
    statuses = [str(item) for item in ((detail or {}).get("statuses") or [status])]
    captions = {
        "LIVE": "IBKR Live",
        "DELAYED": "IBKR Delayed",
        "FROZEN": "IBKR Frozen",
        "PROVIDER": "Yahoo",
        "STALE": "Yahoo stale",
        "HISTORICAL": "EQUITY_EOD",
    }
    if status == "MIXED":
        label = "Mixed · {0}".format(", ".join(statuses))
    else:
        label = captions.get(status, status)
    current = (detail or {}).get("current_session")
    baseline = (detail or {}).get("baseline_session")
    basis = str((detail or {}).get("basis") or "")
    if basis in QUOTE_POLICY_BASES:
        basis_label = BASIS_LABELS.get(basis, "Since session open")
        session = " · {0} · reference session {1}".format(basis_label, current) if current else " · {0}".format(basis_label)
    else:
        session = " · {0} vs {1}".format(current, baseline) if current and baseline else ""
    updated = (detail or {}).get("updated")
    stamp = " · {0}".format(last_updated_label(updated)) if updated and status != "HISTORICAL" else ""
    aligned = " · same session and basis as SPY" if relative else ""
    if isinstance(count, int):
        return "Constituents: {0} · equal-dollar daily rebalance · {1}{2}{3}{4}".format(count, label, session, stamp, aligned)
    return "{0}{1}{2}{3}".format(label, session, stamp, aligned)


def _later_stamp(current: str | None, stamp: str | None) -> str | None:
    if not stamp:
        return current
    if current is None or stamp > current:
        return stamp
    return current


def _freshness_from_cells(
    cells: Sequence[tuple[Mapping[str, Any], bool]],
    spy_detail: Mapping[str, Any],
) -> dict[str, Any]:
    absolute_statuses: set[str] = set()
    relative_statuses: set[str] = set()
    updated: str | None = None
    relative_updated: str | None = None
    for detail, has_value in cells:
        if not has_value:
            continue
        status = str(detail.get("status") or "HISTORICAL")
        if status == "HISTORICAL":
            absolute_statuses.add("HISTORICAL")
            relative_statuses.add("HISTORICAL")
            continue
        for item in detail.get("statuses") or [status]:
            absolute_statuses.add(str(item))
        updated = _later_stamp(updated, detail.get("updated"))
        if quote_periods_match(detail, spy_detail):
            for item in detail.get("statuses") or [status]:
                relative_statuses.add(str(item))
            relative_updated = _later_stamp(relative_updated, detail.get("updated"))
            relative_updated = _later_stamp(relative_updated, spy_detail.get("updated"))
        else:
            relative_statuses.add("HISTORICAL")
    if not absolute_statuses:
        absolute_statuses.add("HISTORICAL")
    if not relative_statuses:
        relative_statuses.add("HISTORICAL")
    return {
        "statuses": sorted(absolute_statuses),
        "updated": updated,
        "relative_statuses": sorted(relative_statuses),
        "relative_updated": relative_updated,
        "active": any(item != "HISTORICAL" for item in absolute_statuses),
    }


def overlay_stored_quote_returns(panel: Mapping[str, Any], by_symbol: Mapping[str, Any] | None) -> dict[str, Any]:
    """Replace a 1D cell only when every contributor shares one session and basis.

    A partial live quote does not average with the remaining prior-day EOD
    returns. The cell keeps the coherent EQUITY_EOD value, and relative mode
    subtracts SPY only on that same pair. Longer horizons stay EQUITY_EOD.
    """
    result = dict(panel)
    quotes = by_symbol or {}
    spy_eod = dict(result.get("spy_returns") or {})
    result["spy_eod_returns"] = spy_eod
    if not result.get("available") or not quotes:
        result["spy_quote_1d"] = _historical_quote_detail(result, None)
        result["quote_freshness"] = {
            "statuses": ["HISTORICAL"],
            "updated": None,
            "relative_statuses": ["HISTORICAL"],
            "relative_updated": None,
            "active": False,
        }
        return result
    panel_basis = _quote_text(result.get("adjustment_basis")) or None
    cells: list[tuple[Mapping[str, Any], bool]] = []

    sectors: list[dict[str, Any]] = []
    for row in result.get("sectors") or []:
        item = dict(row)
        values = list(item.get("values") or [])
        item["eod_values"] = list(values)
        item["eod_quote_1d"] = _historical_quote_detail(result, None)
        leg = _quote_leg(str(item.get("symbol") or ""), quotes, panel_basis=panel_basis)
        if values and leg is not None:
            values[0] = leg["live_return"]
            detail = _quote_detail_from_legs((leg,), 1)
            item["quote_1d"] = detail["status"]
        else:
            detail = _historical_quote_detail(result, None)
            item["quote_1d"] = "HISTORICAL"
        item["values"] = values
        item["quote_1d_detail"] = detail
        sectors.append(item)
        cells.append((detail, bool(values) and values[0] is not None))

    subsectors: dict[str, list[dict[str, Any]]] = {}
    for sector, rows in (result.get("subsectors") or {}).items():
        copied: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            values = list(item.get("values") or [])
            counts = list(item.get("counts") or [])
            item["eod_values"] = list(values)
            item["eod_counts"] = list(counts)
            item["eod_quote_1d"] = _historical_quote_detail(result, counts[0] if counts else None)
            prepared: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            eligible: list[str] = []
            for member in item.get("constituents") or []:
                mem = dict(member)
                returns = dict(mem.get("returns") or {})
                mem["returns"] = returns
                eod = _finite(returns.get("1D"))
                leg = None
                if mem.get("included") and eod is not None:
                    eligible.append(str(mem.get("symbol") or "").upper())
                    leg = _quote_leg(str(mem.get("symbol") or ""), quotes, panel_basis=panel_basis)
                prepared.append((mem, leg))
            legs = [leg for _mem, leg in prepared if leg is not None]
            aligned = (
                len(eligible) >= MIN_SUBSECTOR_CONSTITUENTS
                and len(legs) == len(eligible)
                and _same_quote_period(legs)
            )
            live_value = None
            used: tuple[str, ...] = ()
            if aligned:
                live_value, used, _missing = equal_dollar_live_return(
                    {str(mem.get("symbol") or "").upper(): leg["live_return"] for mem, leg in prepared if leg is not None},
                    eligible,
                )
                aligned = live_value is not None
            members: list[dict[str, Any]] = []
            if aligned and live_value is not None and values:
                detail = _quote_detail_from_legs(legs, len(used))
                values[0] = live_value
                if counts:
                    counts[0] = len(used)
                used_set = set(used)
                for mem, leg in prepared:
                    symbol = str(mem.get("symbol") or "").upper()
                    if leg is not None and symbol in used_set:
                        mem["returns"]["1D"] = leg["live_return"]
                        mem["quote_1d"] = str(leg["status"])
                        mem["quote_unused"] = None
                    else:
                        mem["quote_1d"] = "HISTORICAL"
                        mem["quote_unused"] = None
                    members.append(mem)
                item["quote_1d"] = detail["status"]
            else:
                detail = _historical_quote_detail(result, counts[0] if counts else None)
                for mem, leg in prepared:
                    mem["quote_1d"] = "HISTORICAL"
                    mem["quote_unused"] = "unaligned" if leg is not None else None
                    members.append(mem)
                item["quote_1d"] = "HISTORICAL"
            item["values"] = values
            item["counts"] = counts
            item["constituents"] = members
            item["quote_1d_detail"] = detail
            copied.append(item)
            cells.append((detail, bool(values) and values[0] is not None))
        subsectors[sector] = copied

    spy_returns = dict(spy_eod)
    spy_leg = _quote_leg(BENCHMARK_SPY, quotes, panel_basis=panel_basis)
    if spy_leg is not None:
        spy_returns["1D"] = spy_leg["live_return"]
        spy_detail = _quote_detail_from_legs((spy_leg,), 1)
    else:
        spy_detail = _historical_quote_detail(result, None)
    result["sectors"] = sectors
    result["subsectors"] = subsectors
    result["spy_returns"] = spy_returns
    result["spy_quote_1d"] = spy_detail
    result["quote_freshness"] = _freshness_from_cells(cells, spy_detail)
    return result


def subsector_matrix(
    rows: Sequence[Mapping[str, Any]],
    spy_returns: Mapping[str, Any] | None,
    *,
    mode: str,
    spy_eod_returns: Mapping[str, Any] | None = None,
    spy_quote: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply absolute or relative-to-SPY mode without recomputing constituent returns.

    Relative 1D uses the live pair only when the cell and SPY share a session
    and price basis. Otherwise it uses the stored EQUITY_EOD pair for both.
    """
    if mode not in {"absolute", "relative"}:
        raise ValueError("mode must be absolute or relative")
    columns = [label for label, _field, _sessions in HORIZONS]
    matrix_rows: list[dict[str, Any]] = []
    for row in rows:
        absolute = list(row.get("values") or [])
        eod_values = list(row.get("eod_values") or absolute)
        counts = list(row.get("counts") or [])
        eod_counts = list(row.get("eod_counts") or counts)
        supplied = list(row.get("notes") or [])
        values: list[float | None] = []
        notes: list[str | None] = []
        for index, label in enumerate(columns):
            asset = absolute[index] if index < len(absolute) else None
            eod_asset = eod_values[index] if index < len(eod_values) else asset
            count = counts[index] if index < len(counts) else None
            eod_count = eod_counts[index] if index < len(eod_counts) else count
            supplied_note = supplied[index] if index < len(supplied) else None
            detail = row.get("quote_1d_detail") if label == "1D" else None
            shown_detail = detail if isinstance(detail, Mapping) else None
            shown_count = count
            if mode == "relative" and label == "1D" and (shown_detail is not None or spy_quote is not None):
                if quote_periods_match(shown_detail, spy_quote):
                    shown = return_spread(asset, (spy_returns or {}).get(label))
                else:
                    benchmark = spy_eod_returns if spy_eod_returns is not None else spy_returns
                    shown = return_spread(eod_asset, (benchmark or {}).get(label))
                    eod_detail = row.get("eod_quote_1d")
                    shown_detail = eod_detail if isinstance(eod_detail, Mapping) else {"status": "HISTORICAL", "statuses": ["HISTORICAL"]}
                    shown_count = eod_count
            elif mode == "relative":
                shown = return_spread(asset, (spy_returns or {}).get(label))
            else:
                shown = _finite(asset)
            values.append(shown)
            if shown is None and supplied_note:
                notes.append(str(supplied_note))
            elif label == "1D" and shown is not None and shown_detail is not None:
                notes.append(_cell_source_note(shown_detail, count=shown_count if isinstance(shown_count, int) else None, relative=mode == "relative"))
            elif supplied_note:
                notes.append(str(supplied_note))
            elif isinstance(shown_count, int):
                notes.append("Constituents: {0} · equal-dollar daily rebalance".format(shown_count))
            else:
                notes.append(None)
        matrix_rows.append(
            {
                "label": str(row.get("industry") or row.get("label") or ""),
                "symbol": str(row.get("symbol") or "") or None,
                "values": values,
                "notes": notes,
                "constituents": list(row.get("constituents") or []),
            }
        )
    return {
        "columns": columns,
        "rows": matrix_rows,
        "scales": column_color_scales([row["values"] for row in matrix_rows]),
        "mode": mode,
    }


__all__ = [
    "DRAWDOWN_SESSIONS",
    "EOD_RETURN_STATE",
    "EOD_RS_STATE",
    "GLOBAL_METHODOLOGY",
    "HORIZON_RETURN_FIELD",
    "HORIZON_RS_FIELD",
    "HORIZON_SESSIONS",
    "HORIZONS",
    "LIVE_RETURN_STATE",
    "LIVE_RS_STATE",
    "MIN_SUBSECTOR_CONSTITUENTS",
    "RS_HORIZONS",
    "US_METHODOLOGY",
    "as_day",
    "bars_to_series",
    "build_aligned_us_panel",
    "classify_return",
    "clip_points",
    "column_color_scale",
    "column_color_scales",
    "endpoint_session_return",
    "heatmap_cell_color",
    "heatmap_rows",
    "rebalanced_basket_return",
    "live_field_complete",
    "normalize_selected_to_100",
    "overlay_stored_quote_returns",
    "normalize_to_100",
    "normalized_ratio",
    "period_change",
    "price_ratio_points",
    "return_spread",
    "running_peak_drawdown",
    "sector_bar_pairs",
    "sector_heatmap_matrix",
    "series_prices",
    "session_window_returns",
    "shared_session_bounds",
    "snapshot_rejection_reason",
    "subsector_matrix",
    "trailing_drawdown",
]
