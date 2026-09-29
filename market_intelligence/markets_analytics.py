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
    "Live 1D from stored quotes: current last divided by the prior completed session close. "
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
    "Index charts, ratios, snapshot badges, and drawdowns use Yahoo MARKET_MONITOR_EOD adjusted closes from mi_v_market_monitor_closes. Sector and subsector heatmaps use EQUITY_EOD adjusted closes. Those sources are not spliced. A heatmap cell does not subtract a market-monitor SPY return from an EQUITY_EOD sector return. Raw closes are not mixed into either return. Each symbol keeps one stored provider. Yahoo rows keep SPLIT_ADJUSTED_UNKNOWN_DIVIDEND when that is the stored basis. IBKR rows keep their own basis.",
    "Normalized performance rebases every selected series to 100 on the first date inside From/To where all selected series have a valid adjusted close. Changing the selection recomputes that date. Later missing sessions stay missing. Nothing is forward-filled.",
    "Relative leadership is the point-in-time price ratio (asset / SPY). Indexed mode rebases that ratio to 100 at the start of the selected interval. Absolute mode shows the raw price ratio. The ratio of two already-indexed price series is not used. Rising means the numerator outperformed SPY. It is not the windowed `rs_chg_*` ratio change.",
    "Return windows are stored trading sessions: 1D = 1, 1W = 5, 1M = 21, 3M = 63, 6M = 126, 1Y = 252. They are not calendar-day offsets. Header badges, the sector heatmap, and subsector returns share these windows.",
    "Sector ETFs follow the repository taxonomy: XLK Technology, XLF Financials, XLI Industrials, XLY Consumer Discretionary, XLC Communication Services, XLV Health Care, XLP Consumer Staples, XLE Energy, XLU Utilities, XLRE Real Estate, XLB Materials. The canonical Technology label is the Information Technology sector.",
    "Sector heatmap absolute mode is the sector ETF return between the shared EQUITY_EOD SPY session endpoints for that horizon. Relative vs SPY subtracts the SPY return on those same dates, in percentage points. It is not `rs_chg_*` and it is not a ratio of the two percentage returns. Each horizon column has its own symmetric color scale. A sector ETF that misses either endpoint, or that uses a different adjustment basis than SPY, is N/A. Stored sector snapshots are not a substitute.",
    "Subsector rows are curated current-context baskets, not official GICS industries and not industry ETFs. Cross-sector themes are omitted. The only basket method is equal-dollar daily rebalancing (`equal_dollar_daily_rebalance_v1`): each horizon rebuilds that index from members that have the shared start session and the shared SPY endpoint, then takes the index window return. Fewer than 2 such members is N/A. The count is those endpoint-eligible names. Each daily step includes only members with a price on that session and on the previous session inside the window. This is not the average of each name's holding-period return, and a stored snapshot is not used when constituent prices are missing. A missing price is not zero.",
    "Index snapshot badges stay on finalized EOD session returns. Live 1D on a heatmap is a stored IBKR last divided by the prior regular EQUITY_EOD close when that quote is fresh. Delayed and frozen quotes are labeled and are not called live. A symbol without a stored quote keeps its EQUITY_EOD 1D return. Longer horizons stay EQUITY_EOD. Weighting stays equal-dollar daily rebalance. Streamlit reads the shared quote cache and does not open TWS.",
    "Drawdown from the 52-week high is adjusted_close / max(adjusted_close over the trailing 252 stored sessions, including that session) - 1. Fewer than 252 sessions stays missing. The value is never positive. It is not replaced with zero. The same price/peak formula on a full running peak is the definition inside each window.",
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
    """Start and end sessions on one explicit calendar. Not each symbol's own last date."""
    if sessions < 1 or len(calendar) <= sessions:
        return None
    end = calendar[-1]
    start = calendar[-1 - sessions]
    if sessions == 1 and (end - start).days > 4:
        return None
    return start, end


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
    allowed = {day for day in calendar if start <= day <= end}
    restricted = {
        symbol: {day: price for day, price in prices.items() if day in allowed}
        for symbol, prices in member_prices.items()
    }
    index = daily_rebalanced_equal_weight(restricted, start=start, end=end)
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


def _quote_return(symbol: str, by_symbol: Mapping[str, Any]) -> tuple[float | None, str, str | None]:
    row = by_symbol.get(symbol) or by_symbol.get(str(symbol).upper()) or {}
    if not isinstance(row, Mapping):
        return None, "HISTORICAL", None
    current = row.get("current") or {}
    status = str((current or {}).get("market_data_status") or "HISTORICAL").upper()
    updated = (current or {}).get("observation_ts")
    live = row.get("live_return")
    if status == "HISTORICAL" or not isinstance(live, (int, float)) or not math.isfinite(float(live)):
        return None, "HISTORICAL", None
    return float(live), status, str(updated) if updated else None


def overlay_stored_quote_returns(panel: Mapping[str, Any], by_symbol: Mapping[str, Any] | None) -> dict[str, Any]:
    """Replace 1D cells from the shared quote cache. Longer horizons stay EQUITY_EOD.

    A missing quote keeps that symbol's stored session return. One missing quote
    does not drop the basket. Weighting stays the equal-dollar 1-session mean.
    """
    result = dict(panel)
    quotes = by_symbol or {}
    if not result.get("available") or not quotes:
        result["quote_freshness"] = {"statuses": ["HISTORICAL"], "updated": None, "active": False}
        return result
    statuses: set[str] = set()
    updated: str | None = None

    def _note_update(stamp: str | None) -> None:
        nonlocal updated
        if stamp and (updated is None or stamp > updated):
            updated = stamp

    sectors: list[dict[str, Any]] = []
    for row in result.get("sectors") or []:
        item = dict(row)
        values = list(item.get("values") or [])
        live, status, stamp = _quote_return(str(item.get("symbol") or ""), quotes)
        if values and live is not None:
            values[0] = live
            statuses.add(status)
            _note_update(stamp)
            item["quote_1d"] = status
        else:
            item["quote_1d"] = "HISTORICAL"
        item["values"] = values
        sectors.append(item)
    subsectors: dict[str, list[dict[str, Any]]] = {}
    for sector, rows in (result.get("subsectors") or {}).items():
        copied: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            members: list[dict[str, Any]] = []
            ones: list[float] = []
            used_quote = False
            for member in item.get("constituents") or []:
                mem = dict(member)
                returns = dict(mem.get("returns") or {})
                live, status, stamp = _quote_return(str(mem.get("symbol") or ""), quotes)
                if live is not None:
                    returns["1D"] = live
                    mem["quote_1d"] = status
                    statuses.add(status)
                    _note_update(stamp)
                    used_quote = True
                    ones.append(live)
                else:
                    mem["quote_1d"] = "HISTORICAL"
                    eod = returns.get("1D")
                    if mem.get("included") and isinstance(eod, (int, float)) and math.isfinite(float(eod)):
                        ones.append(float(eod))
                mem["returns"] = returns
                members.append(mem)
            values = list(item.get("values") or [])
            if values and used_quote and len(ones) >= MIN_SUBSECTOR_CONSTITUENTS:
                values[0] = sum(ones) / len(ones)
            item["values"] = values
            item["constituents"] = members
            copied.append(item)
        subsectors[sector] = copied
    spy_returns = dict(result.get("spy_returns") or {})
    spy_live, spy_status, spy_stamp = _quote_return(BENCHMARK_SPY, quotes)
    if spy_live is not None:
        spy_returns["1D"] = spy_live
        statuses.add(spy_status)
        _note_update(spy_stamp)
    result["sectors"] = sectors
    result["subsectors"] = subsectors
    result["spy_returns"] = spy_returns
    result["quote_freshness"] = {
        "statuses": sorted(statuses) or ["HISTORICAL"],
        "updated": updated,
        "active": bool(statuses),
    }
    return result


def subsector_matrix(rows: Sequence[Mapping[str, Any]], spy_returns: Mapping[str, Any] | None, *, mode: str) -> dict[str, Any]:
    """Apply absolute or relative-to-SPY mode without recomputing constituent returns."""
    if mode not in {"absolute", "relative"}:
        raise ValueError("mode must be absolute or relative")
    columns = [label for label, _field, _sessions in HORIZONS]
    matrix_rows: list[dict[str, Any]] = []
    for row in rows:
        absolute = list(row.get("values") or [])
        counts = list(row.get("counts") or [])
        supplied = list(row.get("notes") or [])
        values: list[float | None] = []
        notes: list[str | None] = []
        for index, label in enumerate(columns):
            asset = absolute[index] if index < len(absolute) else None
            if mode == "relative":
                shown = return_spread(asset, (spy_returns or {}).get(label))
            else:
                shown = _finite(asset)
            values.append(shown)
            supplied_note = supplied[index] if index < len(supplied) else None
            count = counts[index] if index < len(counts) else None
            if supplied_note:
                notes.append(str(supplied_note))
            elif isinstance(count, int):
                notes.append("Constituents: {0} · equal-dollar daily rebalance".format(count))
            else:
                notes.append(None)
        matrix_rows.append(
            {
                "label": str(row.get("industry") or row.get("label") or ""),
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
