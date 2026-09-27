"""Macro dashboard grouping, display scales, and recession intervals.

Catalog categories stay canonical. These groups are only the Macro page layout.
Nothing here calls FRED, fills missing observations, or builds a composite score.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from market_intelligence.catalog import CATALOG_BY_ID, FRED_ATTRIBUTION, MACRO_MAX_BACKFILL_SERIES
from market_intelligence.components.market_chart import observation_day
from market_intelligence.history_range import filter_history_rows, union_history_bounds

VINTAGE_NOTE = (
    "Historical macro series use the latest stored FRED vintage unless explicitly identified otherwise."
)
LEADING_NOTE = "Curated leading indicators; not the Conference Board Leading Economic Index."
COINCIDENT_NOTE = "Curated coincident indicators; not a proprietary coincident index."
NFCI_NOTE = (
    "Positive values = tighter-than-average financial conditions. "
    "Negative values = looser-than-average financial conditions."
)
BREAKEVEN_NOTE = "Market-implied inflation compensation, not survey expectations."

GROUP_ORDER: tuple[str, ...] = ("fed", "inflation", "leading", "coincident", "lagging")
GROUP_LABELS: dict[str, str] = {
    "fed": "Fed",
    "inflation": "Inflation",
    "leading": "Leading indicators",
    "coincident": "Coincident indicators",
    "lagging": "Lagging indicators",
}

# One conversion from provider-native stored units. Not applied on top of level_display.
DISPLAY_SCALE: dict[str, dict[str, Any]] = {
    "WALCL": {"divisor": 1_000_000.0, "units": "USD tn", "source_units": "millions_usd"},
    "M2SL": {"divisor": 1_000.0, "units": "USD tn", "source_units": "billions_usd"},
    "WRESBAL": {"divisor": 1_000.0, "units": "USD bn", "source_units": "millions_usd"},
    "WTREGEN": {"divisor": 1_000.0, "units": "USD bn", "source_units": "millions_usd"},
    "RRPONTSYD": {"divisor": 1.0, "units": "USD bn", "source_units": "billions_usd"},
}

CHART_TITLES: dict[str, tuple[str, ...]] = {
    "fed": (
        "Fed Policy Rates",
        "Federal Reserve Balance Sheet",
        "System Liquidity Components",
        "M2 Money Supply",
        "Financial Conditions",
    ),
    "inflation": (
        "CPI Inflation",
        "CPI Short-Run Momentum",
        "PCE Inflation",
        "PCE Short-Run Momentum",
        "Services Inflation ex Rent of Shelter",
        "Market Inflation Compensation",
    ),
    "leading": (
        "Initial Jobless Claims",
        "Building Permits",
        "Core Capital Goods Orders",
        "Manufacturing Weekly Hours",
    ),
    "coincident": (
        "Nonfarm Payroll Growth",
        "Industrial Production",
        "Real Manufacturing & Trade Sales",
        "Real Consumer Spending",
        "Real Personal Income ex Transfers",
    ),
    "lagging": (
        "Unemployment Rate",
        "Median Duration of Unemployment",
        "Inventory-to-Sales Ratio",
        "C&I Loan Growth",
        "Business Loan Delinquency Rate",
        "Unit Labor Cost Growth",
    ),
}

# source is a series id or ``SERIES.metric`` id. scale marks raw observations that
# need DISPLAY_SCALE. kind is lines, policy, or toggle.
CHARTS: dict[str, tuple[dict[str, Any], ...]] = {
    "fed": (
        {
            "title": "Fed Policy Rates",
            "kind": "policy",
            "unit": "Percent",
            "format": "percent",
            "series": (
                ("DFEDTARL", "Target lower"),
                ("DFEDTARU", "Target upper"),
                ("DFF", "Effective Fed Funds"),
                ("SOFR", "SOFR"),
            ),
        },
        {
            "title": "Federal Reserve Balance Sheet",
            "kind": "lines",
            "unit": "USD tn",
            "format": "number",
            "series": (("WALCL", "Total assets", True),),
        },
        {
            "title": "System Liquidity Components",
            "kind": "lines",
            "unit": "USD bn",
            "format": "number",
            "series": (
                ("WRESBAL", "Reserve balances", True),
                ("WTREGEN", "Treasury General Account", True),
                ("RRPONTSYD", "Overnight reverse repo", True),
            ),
        },
        {
            "title": "M2 Money Supply",
            "kind": "toggle",
            "unit_by_mode": {"Level": "USD tn", "YoY %": "Percent"},
            "format_by_mode": {"Level": "number", "YoY %": "percent"},
            "modes": ("Level", "YoY %"),
            "default": "Level",
            "series_by_mode": {
                "Level": (("M2SL", "M2", True),),
                "YoY %": (("M2SL.yoy_pct", "M2 YoY", False),),
            },
        },
        {
            "title": "Financial Conditions",
            "kind": "lines",
            "unit": "Index",
            "format": "index",
            "reference": 0,
            "caption": NFCI_NOTE,
            "series": (("NFCI", "NFCI", False),),
        },
    ),
    "inflation": (
        {
            "title": "CPI Inflation",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (
                ("CPIAUCSL.yoy_pct", "Headline CPI YoY", False),
                ("CPILFESL.yoy_pct", "Core CPI YoY", False),
            ),
        },
        {
            "title": "CPI Short-Run Momentum",
            "kind": "momentum",
            "unit": "Percent",
            "format": "percent",
            "series_modes": ("Headline", "Core", "Both"),
            "window_modes": ("3M annualized", "6M annualized", "Both"),
            "default_series": "Both",
            "default_window": "3M annualized",
            "members": {"Headline": "CPIAUCSL", "Core": "CPILFESL"},
            "windows": {"3M annualized": "ann3m_pct", "6M annualized": "ann6m_pct"},
        },
        {
            "title": "PCE Inflation",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (
                ("PCEPI.yoy_pct", "Headline PCE YoY", False),
                ("PCEPILFE.yoy_pct", "Core PCE YoY", False),
            ),
        },
        {
            "title": "PCE Short-Run Momentum",
            "kind": "momentum",
            "unit": "Percent",
            "format": "percent",
            "series_modes": ("Headline", "Core", "Both"),
            "window_modes": ("3M annualized", "6M annualized", "Both"),
            "default_series": "Both",
            "default_window": "3M annualized",
            "members": {"Headline": "PCEPI", "Core": "PCEPILFE"},
            "windows": {"3M annualized": "ann3m_pct", "6M annualized": "ann6m_pct"},
        },
        {
            "title": "Services Inflation ex Rent of Shelter",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized", "6M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("CUSR0000SASL2RS.yoy_pct", "Services ex rent YoY", False),),
                "3M annualized": (("CUSR0000SASL2RS.ann3m_pct", "Services ex rent 3M ann.", False),),
                "6M annualized": (("CUSR0000SASL2RS.ann6m_pct", "Services ex rent 6M ann.", False),),
            },
        },
        {
            "title": "Market Inflation Compensation",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "caption": BREAKEVEN_NOTE,
            "series": (
                ("T5YIE", "5Y Breakeven", False),
                ("T10YIE", "10Y Breakeven", False),
                ("T5YIFR", "5Y5Y Forward", False),
            ),
        },
    ),
    "leading": (
        {
            "title": "Initial Jobless Claims",
            "kind": "lines",
            "unit": "Claims",
            "format": "claims",
            "series": (
                ("ICSA", "Weekly claims", False),
                ("ICSA.avg_4w", "4-week average", False),
            ),
        },
        {
            "title": "Building Permits",
            "kind": "toggle",
            "unit_by_mode": {"Level": "SAAR, thousands", "YoY %": "Percent"},
            "format_by_mode": {"Level": "thousands", "YoY %": "percent"},
            "modes": ("Level", "YoY %"),
            "default": "Level",
            "series_by_mode": {
                "Level": (("PERMIT", "Building permits", False),),
                "YoY %": (("PERMIT.yoy_pct", "Building permits YoY", False),),
            },
        },
        {
            "title": "Core Capital Goods Orders",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized", "6M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("NEWORDER.yoy_pct", "Core capital goods YoY", False),),
                "3M annualized": (("NEWORDER.ann3m_pct", "Core capital goods 3M ann.", False),),
                "6M annualized": (("NEWORDER.ann6m_pct", "Core capital goods 6M ann.", False),),
            },
        },
        {
            "title": "Manufacturing Weekly Hours",
            "kind": "lines",
            "unit": "Hours",
            "format": "hours",
            "series": (("AWHMAN", "Manufacturing hours", False),),
        },
    ),
    "coincident": (
        {
            "title": "Nonfarm Payroll Growth",
            "kind": "lines",
            "unit": "Thousands of persons",
            "format": "thousands",
            "series": (("PAYEMS.mom_change", "Monthly payroll change", False),),
        },
        {
            "title": "Industrial Production",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("INDPRO.yoy_pct", "Industrial production YoY", False),),
                "3M annualized": (("INDPRO.ann3m_pct", "Industrial production 3M ann.", False),),
            },
        },
        {
            "title": "Real Manufacturing & Trade Sales",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("CMRMT.yoy_pct", "Real sales YoY", False),),
                "3M annualized": (("CMRMT.ann3m_pct", "Real sales 3M ann.", False),),
            },
        },
        {
            "title": "Real Consumer Spending",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("PCEC96.yoy_pct", "Real consumption YoY", False),),
                "3M annualized": (("PCEC96.ann3m_pct", "Real consumption 3M ann.", False),),
            },
        },
        {
            "title": "Real Personal Income ex Transfers",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "3M annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("W875RX1.yoy_pct", "Real income ex transfers YoY", False),),
                "3M annualized": (("W875RX1.ann3m_pct", "Real income ex transfers 3M ann.", False),),
            },
        },
    ),
    "lagging": (
        {
            "title": "Unemployment Rate",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (("UNRATE", "Unemployment rate", False),),
        },
        {
            "title": "Median Duration of Unemployment",
            "kind": "lines",
            "unit": "Weeks",
            "format": "weeks",
            "series": (("UEMPMED", "Median duration", False),),
        },
        {
            "title": "Inventory-to-Sales Ratio",
            "kind": "lines",
            "unit": "Ratio",
            "format": "ratio",
            "series": (("ISRATIO", "Inventory / sales", False),),
        },
        {
            "title": "C&I Loan Growth",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (("BUSLOANS.yoy_pct", "C&I loans YoY", False),),
        },
        {
            "title": "Business Loan Delinquency Rate",
            "kind": "lines",
            "unit": "Percent",
            "format": "percent",
            "series": (("DRBLACBS", "Business loan delinquency", False),),
        },
        {
            "title": "Unit Labor Cost Growth",
            "kind": "toggle",
            "unit": "Percent",
            "format": "percent",
            "modes": ("YoY", "QoQ annualized"),
            "default": "YoY",
            "series_by_mode": {
                "YoY": (("ULCNFB.yoy_pct", "Unit labor costs YoY", False),),
                "QoQ annualized": (("ULCNFB.qoq_saar_pct", "Unit labor costs QoQ ann.", False),),
            },
        },
    ),
}


def scale_level(series_id: str, value: Any) -> float | None:
    """Convert one stored raw observation into the chart unit. Missing stays missing."""
    spec = DISPLAY_SCALE[series_id]
    if value is None:
        return None
    number = float(value)
    return number / float(spec["divisor"])


def recession_intervals(rows: Sequence[Mapping[str, Any]], *, date_key: str = "as_of") -> list[dict[str, str]]:
    """Contiguous USREC == 1 observations become one inclusive interval.

    A stored value other than 1 ends the interval. Missing rows are skipped and
    do not become a recession. Dates are not extended before the first 1 or
    after the last 1.
    """
    points: list[tuple[date, float]] = []
    for row in rows:
        day = observation_day(row.get(date_key) if date_key in row else row.get("observation_date"))
        raw = row.get("value")
        if day is None or raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        points.append((day, value))
    points.sort(key=lambda item: item[0])
    intervals: list[dict[str, str]] = []
    start: date | None = None
    end: date | None = None
    for day, value in points:
        if value == 1.0:
            if start is None:
                start = day
            end = day
            continue
        if start is not None and end is not None:
            intervals.append({"start": start.isoformat(), "end": end.isoformat()})
        start = None
        end = None
    if start is not None and end is not None:
        intervals.append({"start": start.isoformat(), "end": end.isoformat()})
    return intervals


def group_source_ids(group: str) -> list[str]:
    """Series and metric ids the group charts read, plus USREC shading."""
    found: list[str] = []
    seen: set[str] = set()

    def add(source_id: str) -> None:
        if source_id not in seen:
            seen.add(source_id)
            found.append(source_id)

    for chart in CHARTS[group]:
        for item in chart.get("series") or ():
            add(item[0])
        for mode_rows in (chart.get("series_by_mode") or {}).values():
            for item in mode_rows:
                add(item[0])
        members = chart.get("members") or {}
        windows = chart.get("windows") or {}
        for series_id in members.values():
            for metric in windows.values():
                add("{0}.{1}".format(series_id, metric))
    add("USREC")
    return found


def selected_lines(
    chart: Mapping[str, Any],
    *,
    mode: str | None = None,
    series_mode: str | None = None,
    window_mode: str | None = None,
) -> list[tuple[str, str, bool]]:
    """``(source_id, label, scale)`` for the active toggle. One unit family only."""
    kind = chart["kind"]
    if kind == "lines":
        return [(item[0], item[1], bool(item[2])) for item in chart["series"]]
    if kind == "toggle":
        chosen = mode or chart["default"]
        rows = chart["series_by_mode"][chosen]
        return [(item[0], item[1], bool(item[2])) for item in rows]
    if kind == "momentum":
        series_choice = series_mode or chart["default_series"]
        window_choice = window_mode or chart["default_window"]
        member_names = list(chart["members"]) if series_choice == "Both" else [series_choice]
        window_names = list(chart["windows"]) if window_choice == "Both" else [window_choice]
        lines: list[tuple[str, str, bool]] = []
        for member_name in member_names:
            series_id = chart["members"][member_name]
            for window_name in window_names:
                metric = chart["windows"][window_name]
                lines.append(("{0}.{1}".format(series_id, metric), "{0} {1}".format(member_name, window_name), False))
        return lines
    return []


def chart_unit(chart: Mapping[str, Any], *, mode: str | None = None) -> str:
    if chart.get("unit_by_mode"):
        return str(chart["unit_by_mode"][mode or chart["default"]])
    return str(chart.get("unit") or "")


def chart_format(chart: Mapping[str, Any], *, mode: str | None = None) -> str:
    if chart.get("format_by_mode"):
        return str(chart["format_by_mode"][mode or chart["default"]])
    return str(chart.get("format") or "number")


def history_window_bounds(
    groups: Sequence[Sequence[Mapping[str, Any]]],
) -> tuple[date | None, date | None]:
    """Union of real observations. A later-starting series does not move the start."""
    return union_history_bounds(groups, date_key="as_of")


def rows_in_range(
    rows: Sequence[Mapping[str, Any]],
    *,
    start: date | None,
    end: date | None,
) -> list[dict[str, Any]]:
    """Inclusive filter. From after To yields an empty list and changes nothing."""
    if start is None or end is None or start > end:
        return []
    return filter_history_rows(rows, start=start, end=end, date_key="as_of")


def prepare_line_points(
    rows: Sequence[Mapping[str, Any]],
    *,
    start: date,
    end: date,
    scale_series: str | None = None,
) -> list[dict[str, Any]]:
    """Chart points inside the window. Missing values are omitted, never zero."""
    points: list[dict[str, Any]] = []
    for row in rows_in_range(rows, start=start, end=end):
        raw = row.get("value")
        if raw is None:
            continue
        value = scale_level(scale_series, raw) if scale_series else float(raw)
        if value is None:
            continue
        points.append({"as_of": row.get("as_of"), "value": value})
    return points


def policy_rate_frame(
    lower: Sequence[Mapping[str, Any]],
    upper: Sequence[Mapping[str, Any]],
    effective: Sequence[Mapping[str, Any]],
    sofr: Sequence[Mapping[str, Any]],
    bands: Sequence[Mapping[str, str]],
    *,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Aligned policy series. The span is upper minus lower on dates where both exist.

    No target is copied onto a date that lacks that print. DFF and SOFR stay on
    their own observation dates.
    """

    def lookup(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for row in rows_in_range(rows, start=start, end=end):
            day = observation_day(row.get("as_of"))
            if day is None or row.get("value") is None:
                continue
            out[day.isoformat()] = float(row["value"])
        return out

    lower_map = lookup(lower)
    upper_map = lookup(upper)
    dff_map = lookup(effective)
    sofr_map = lookup(sofr)
    days = sorted(set(lower_map) | set(upper_map) | set(dff_map) | set(sofr_map))

    def column(values: Mapping[str, float]) -> list[list[Any]]:
        return [[day, values[day]] if day in values else [day, None] for day in days]

    span: list[list[Any]] = []
    for day in days:
        if day in lower_map and day in upper_map:
            span.append([day, upper_map[day] - lower_map[day]])
        else:
            span.append([day, None])
    clipped: list[dict[str, str]] = []
    if days:
        first, last = days[0], days[-1]
        for band in bands:
            band_start = str(band.get("start") or "")
            band_end = str(band.get("end") or "")
            if not band_start or not band_end or band_end < first or band_start > last:
                continue
            clipped.append({"start": max(band_start, first), "end": min(band_end, last)})
    return {
        "categories": days,
        "lower": column(lower_map),
        "upper": column(upper_map),
        "span": span,
        "effective": column(dff_map),
        "sofr": column(sofr_map),
        "bands": clipped,
    }


def methodology_lines(group: str) -> list[str]:
    """Series identity, units, frequency, and the shared vintage sentence."""
    lines = [VINTAGE_NOTE, "FRED is the retrieval layer. Stored PostgreSQL observations are the chart source."]
    if group == "leading":
        lines.append(LEADING_NOTE)
    if group == "coincident":
        lines.append(COINCIDENT_NOTE)
    lines.append(
        "YoY = 100·(X_t/X_{t−12} − 1). "
        "3M annualized = 100·((X_t/X_{t−3})^4 − 1). "
        "6M annualized = 100·((X_t/X_{t−6})^2 − 1). "
        "Quarterly annualized = 100·((X_t/X_{t−1})^4 − 1). "
        "Lags use the calendar date. A missing lag stays missing."
    )
    lines.append(
        "Weekly C&I loan YoY uses the last observation on or before t−12 months within 8 days. "
        "Payroll growth is the monthly difference in thousands of persons. "
        "Claims 4-week average is the mean of the last four stored weekly prints."
    )
    lines.append(
        "WALCL millions ÷ 1,000,000 = trillions. WRESBAL and WTREGEN millions ÷ 1,000 = billions. "
        "M2 billions ÷ 1,000 = trillions. RRP is already billions. Raw rows are not rewritten."
    )
    seen: set[str] = set()
    for source_id in group_source_ids(group):
        series_id = source_id.split(".", 1)[0]
        if series_id in seen:
            continue
        seen.add(series_id)
        spec = CATALOG_BY_ID.get(series_id)
        if spec is None:
            continue
        units = ", ".join(spec.expected_units_contains)
        lines.append(
            "{0} ({1}): {2}; frequency {3}; seasonal adjustment {4}. {5}".format(
                spec.label or series_id,
                series_id,
                units,
                spec.expected_frequency,
                spec.expected_sa or "n/a",
                spec.notes,
            )
        )
    lines.append(FRED_ATTRIBUTION)
    if "USREC" not in seen:
        lines.append("USREC shades NBER recession months. It is not plotted as its own series.")
    return lines


def macro_series_ids() -> tuple[str, ...]:
    return MACRO_MAX_BACKFILL_SERIES
