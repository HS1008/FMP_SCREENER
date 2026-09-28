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

from market_intelligence.baskets import window_return
from market_intelligence.sector_mapping import CANONICAL_SECTORS
from market_intelligence.taxonomy import KIND_ETF_COMPARISON, SECTOR_PROXIES, stock_subsector_baskets

DRAWDOWN_SESSIONS = 252
MIN_SUBSECTOR_CONSTITUENTS = 2
_HEAT_RED = (179, 64, 64)
_HEAT_NEUTRAL = (44, 48, 54)
_HEAT_GREEN = (61, 140, 90)
_COMPARISON_ETFS = frozenset({"SMH", "XSD", "KRE", "XBI", "XOP", "XRT", "KBE", "BOTZ"})
_ETF_PROXIES = frozenset(SECTOR_PROXIES.values()) | _COMPARISON_ETFS

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
    "Historical comparisons use stored adjusted closes (`adj_close_price`) from EQUITY_EOD. Raw closes are not mixed into the same return. Each symbol keeps a single stored provider for its whole history; providers are not spliced. The stored `adjustment_basis` is shown with the coverage metadata. Yahoo history uses auto-adjusted closes labeled SPLIT_ADJUSTED_UNKNOWN_DIVIDEND when that is the stored basis. IBKR rows keep their own basis.",
    "Normalized performance rebases every selected series to 100 on the first date inside From/To where all selected series have a valid adjusted close. Changing the selection recomputes that date. Later missing sessions stay missing. Nothing is forward-filled.",
    "Relative leadership is the point-in-time price ratio (asset / SPY). Indexed mode rebases that ratio to 100 at the start of the selected interval. Absolute mode shows the raw price ratio. The ratio of two already-indexed price series is not used. Rising means the numerator outperformed SPY. It is not the windowed `rs_chg_*` ratio change.",
    "Return windows are stored trading sessions: 1D = 1, 1W = 5, 1M = 21, 3M = 63, 6M = 126, 1Y = 252. They are not calendar-day offsets. Header badges, the sector heatmap, and subsector returns share these windows.",
    "Sector ETFs follow the repository taxonomy: XLK Technology, XLF Financials, XLI Industrials, XLY Consumer Discretionary, XLC Communication Services, XLV Health Care, XLP Consumer Staples, XLE Energy, XLU Utilities, XLRE Real Estate, XLB Materials. The canonical Technology label is the Information Technology sector.",
    "Sector heatmap absolute mode is the sector ETF session return. Relative vs SPY is that return minus the SPY return over the same window, in percentage points. It is not `rs_chg_*` and it is not a ratio of the two percentage returns. Each horizon column has its own symmetric color scale.",
    "Subsectors use the canonical current-context industry baskets (not a guessed GICS list and not industry ETFs). When constituent adjusted closes are stored, each horizon is the equal-weighted mean of valid constituent returns, with at least 2 names. Otherwise the stored equal-dollar basket return is shown. A missing constituent does not become zero.",
    "Live 1D is current last / prior completed session close, and only when the stored quote is fresh. The index snapshot and both heatmaps stay on finalized EOD session returns. Stale live quotes are not mixed into those cells.",
    "Drawdown from the 52-week high is adjusted_close / max(adjusted_close over the trailing 252 stored sessions, including that session) - 1. Fewer than 252 sessions stays missing. The value is never positive. It is not replaced with zero. The same price/peak formula on a full running peak is the definition inside each window.",
)

GLOBAL_METHODOLOGY: tuple[str, ...] = (
    "USD-listed ETF proxies: SPY United States, VEA Developed ex-US, VGK Europe, EWJ Japan, VWO Emerging Markets, MCHI China, INDA India, EWZ Brazil. One broad ETF per region. Returns are what a USD investor experienced in that ETF, including currency effects embedded in the USD share price.",
    "Local-index comparison (local currency versus USD ETF) is deferred. Free index history was not added as a second mode because a partial or unreliable ticker map would overstate coverage.",
    "Adjusted closes and provider lineage follow the same EQUITY_EOD rules as US Markets. History starts at the fund's first stored session. Nothing is fabricated before inception, and symbols are not required to share a start date.",
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


def equal_weight_return(values: Sequence[Any], *, minimum: int = MIN_SUBSECTOR_CONSTITUENTS) -> tuple[float | None, int]:
    """Mean of finite returns. Missing values are dropped, never treated as zero."""
    valid = [number for number in (_finite(value) for value in values) if number is not None]
    if len(valid) < minimum:
        return None, len(valid)
    return sum(valid) / float(len(valid)), len(valid)


def aggregate_subsectors(
    constituents: Sequence[Mapping[str, Any]],
    *,
    minimum: int = MIN_SUBSECTOR_CONSTITUENTS,
) -> dict[str, list[dict[str, Any]]]:
    """Equal-weight each industry inside a sector. Horizons stay independent.

    Rows are alphabetical and are not reordered per column.
    """
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in constituents:
        sector = str(item.get("sector") or "")
        industry = str(item.get("industry") or "")
        if not sector or not industry:
            continue
        grouped.setdefault((sector, industry), []).append(item)
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for (sector, industry), members in grouped.items():
        values: list[float | None] = []
        counts: list[int] = []
        for label, _field, _sessions in HORIZONS:
            number, count = equal_weight_return(
                [(member.get("returns") or {}).get(label) for member in members],
                minimum=minimum,
            )
            values.append(number)
            counts.append(count)
        detail = []
        for member in sorted(members, key=lambda row: str(row.get("symbol") or "")):
            detail.append(
                {
                    "symbol": str(member.get("symbol") or ""),
                    "company": str(member.get("company") or member.get("symbol") or ""),
                    "returns": {label: _finite((member.get("returns") or {}).get(label)) for label, _field, _sessions in HORIZONS},
                }
            )
        by_sector.setdefault(sector, []).append(
            {
                "industry": industry,
                "sector": sector,
                "values": values,
                "counts": counts,
                "constituents": detail,
            }
        )
    for sector, rows in by_sector.items():
        rows.sort(key=lambda row: row["industry"])
        by_sector[sector] = rows
    return by_sector


def constituent_horizon_rows(bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """One horizon-return record per canonical stock-basket member.

    A name can sit in more than one basket. Each copy shares that symbol's returns.
    A short history leaves the long horizons missing without affecting shorter ones.
    """
    cached: dict[str, dict[str, float | None]] = {}
    rows: list[dict[str, Any]] = []
    for basket in stock_subsector_baskets():
        for symbol in basket.members:
            if symbol not in cached:
                series = bars_to_series(bars_by_symbol.get(symbol) or ())
                if not series:
                    cached[symbol] = {label: None for label in HORIZON_SESSIONS}
                else:
                    cached[symbol] = session_window_returns(series, max(series))
            rows.append(
                {
                    "symbol": symbol,
                    "company": symbol,
                    "sector": basket.parent_sector,
                    "industry": basket.label,
                    "returns": cached[symbol],
                }
            )
    return rows


def _member_symbols(row: Mapping[str, Any]) -> list[str]:
    coverage = row.get("coverage") or {}
    membership = [str(symbol) for symbol in (coverage.get("membership") or []) if symbol]
    if membership:
        return membership
    instrument = str(row.get("instrument_id") or "")
    if "," in instrument:
        return [part.strip() for part in instrument.split(",") if part.strip()]
    return []


def is_constituent_subsector(row: Mapping[str, Any]) -> bool:
    """Drop ETF comparisons and the explicit unavailable placeholder."""
    industry = str(row.get("industry_key") or "")
    if not industry or industry == "NO_CURATED_SUBGROUP":
        return False
    coverage = row.get("coverage") or {}
    if str(coverage.get("kind") or "") == KIND_ETF_COMPARISON:
        return False
    symbols = _member_symbols(row)
    instrument = str(row.get("instrument_id") or "").strip()
    if not symbols and instrument and "," not in instrument and " " not in instrument:
        symbols = [instrument]
    if symbols and all(symbol in _ETF_PROXIES for symbol in symbols):
        return False
    return True


def snapshot_subsector_table(rows_by_sector: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Stored basket returns. Membership below the minimum becomes N/A."""
    table: dict[str, list[dict[str, Any]]] = {}
    for sector, rows in rows_by_sector.items():
        built: list[dict[str, Any]] = []
        for row in rows:
            if not is_constituent_subsector(row):
                continue
            metrics = row.get("metrics") or {}
            symbols = _member_symbols(row)
            known_count = len(symbols) if symbols else None
            values: list[float | None] = []
            counts: list[int | None] = []
            for label, field, _sessions in HORIZONS:
                number = _finite(metrics.get(field))
                if known_count is not None and known_count < MIN_SUBSECTOR_CONSTITUENTS:
                    number = None
                values.append(number)
                counts.append(known_count)
            built.append(
                {
                    "industry": str(row.get("industry_key") or ""),
                    "sector": sector,
                    "values": values,
                    "counts": counts,
                    "constituents": [{"symbol": symbol, "company": symbol, "returns": {}} for symbol in symbols],
                }
            )
        built.sort(key=lambda item: item["industry"])
        if built:
            table[str(sector)] = built
    return table


def compose_subsector_view(
    computed: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    snapshot: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> dict[str, dict[str, Any]]:
    """Prefer equal-weight constituent math when any horizon is finite.

    A sector with no constituent prices keeps the stored basket series.
    """
    view: dict[str, dict[str, Any]] = {}
    for sector in CANONICAL_SECTORS:
        computed_rows = list((computed or {}).get(sector) or [])
        finite = any(_finite(value) is not None for row in computed_rows for value in row.get("values") or [])
        if finite:
            view[sector] = {"rows": computed_rows, "method": "equal_weight"}
            continue
        stored = list((snapshot or {}).get(sector) or [])
        if stored:
            view[sector] = {"rows": stored, "method": "stored_basket"}
        else:
            view[sector] = {"rows": [], "method": "unavailable"}
    return view


def subsector_matrix(rows: Sequence[Mapping[str, Any]], spy_returns: Mapping[str, Any] | None, *, mode: str) -> dict[str, Any]:
    """Apply absolute or relative-to-SPY mode without recomputing constituent returns."""
    if mode not in {"absolute", "relative"}:
        raise ValueError("mode must be absolute or relative")
    columns = [label for label, _field, _sessions in HORIZONS]
    matrix_rows: list[dict[str, Any]] = []
    for row in rows:
        absolute = list(row.get("values") or [])
        counts = list(row.get("counts") or [])
        values: list[float | None] = []
        notes: list[str | None] = []
        for index, label in enumerate(columns):
            asset = absolute[index] if index < len(absolute) else None
            if mode == "relative":
                shown = return_spread(asset, (spy_returns or {}).get(label))
            else:
                shown = _finite(asset)
            values.append(shown)
            count = counts[index] if index < len(counts) else None
            if isinstance(count, int):
                notes.append("Constituents: {0}".format(count))
            else:
                notes.append(None)
        matrix_rows.append(
            {
                "label": str(row.get("industry") or ""),
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
    "aggregate_subsectors",
    "as_day",
    "bars_to_series",
    "classify_return",
    "clip_points",
    "column_color_scale",
    "column_color_scales",
    "compose_subsector_view",
    "constituent_horizon_rows",
    "equal_weight_return",
    "heatmap_cell_color",
    "heatmap_rows",
    "is_constituent_subsector",
    "live_field_complete",
    "normalize_selected_to_100",
    "normalize_to_100",
    "normalized_ratio",
    "period_change",
    "price_ratio_points",
    "return_spread",
    "running_peak_drawdown",
    "sector_bar_pairs",
    "sector_heatmap_matrix",
    "session_window_returns",
    "snapshot_subsector_table",
    "subsector_matrix",
    "trailing_drawdown",
]
