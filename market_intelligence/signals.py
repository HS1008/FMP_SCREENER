"""Structured market signals for Overview cards and takeaways.

Factual stored observations only. Ranking is transparent and separate from strategy scoring.
Missing / NaN / infinity never become zero or directional labels.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from market_intelligence.overview import _num, _signed, _signed_pct, _level


ROUTE_BY_AREA = {
    "Sectors": "sectors",
    "Equities": "sectors",
    "Rates": "rates",
    "Credit": "credit",
    "Macro": "macro",
    "Inflation": "macro",
    "Labor": "macro",
    "Growth": "macro",
    "Liquidity": "macro",
    "Commodities": "commodities",
    "Energy": "commodities",
    "Options": "options",
    "Bond activity": "order_flow",
    "Order Flow": "order_flow",
}


@dataclass(frozen=True)
class Signal:
    metric_key: str
    category: str
    label: str
    value: float | None
    units: str | None
    change: float | None
    comparison_period: str
    direction: str | None
    observation_date: str | None
    source: str | None
    quality: str
    drilldown_route: str
    text: str
    materiality: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction_for(change: float | None, *, rising_label: str, falling_label: str) -> str | None:
    if change is None:
        return None
    if change > 0:
        return rising_label
    if change < 0:
        return falling_label
    return "unchanged"


def build_what_matters(
    *,
    rates: dict[str, Any] | None = None,
    credit: dict[str, Any] | None = None,
    sectors: dict[str, Any] | None = None,
    macro: dict[str, Any] | None = None,
    order_flow: dict[str, Any] | None = None,
    commodities: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    horizon: str = "1D",
    limit: int = 5,
) -> list[Signal]:
    """Three-to-five nonduplicative takeaways. One signal per category max."""
    candidates: list[Signal] = []
    candidates.extend(_sector_signals(sectors or {}, horizon=horizon))
    candidates.extend(_rate_signals(rates or {}))
    candidates.extend(_credit_signals(credit or {}))
    candidates.extend(_macro_signals(macro or {}))
    candidates.extend(_commodity_signals(commodities or [], macro or {}))
    candidates.extend(_order_flow_signals(order_flow or {}))
    candidates.extend(_options_signals(options or {}))

    by_category: dict[str, Signal] = {}
    for signal in sorted(candidates, key=lambda item: item.materiality, reverse=True):
        if signal.category in by_category:
            continue
        if signal.materiality <= 0:
            continue
        by_category[signal.category] = signal
        if len(by_category) >= limit:
            break
    return list(by_category.values())[:limit]


def _sector_signals(sectors: dict[str, Any], *, horizon: str) -> list[Signal]:
    rows = list((sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or [])
    metric = {"1D": "ret_1d", "1W": "ret_1w", "1M": "ret_1m"}.get(horizon, "ret_1d")
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        metrics = row.get("metrics") or {}
        ret = _num(metrics.get(metric))
        if ret is None:
            continue
        ranked.append((ret, row))
    if len(ranked) < 2:
        return []
    ranked.sort(key=lambda item: item[0], reverse=True)
    lead_ret, lead = ranked[0]
    lag_ret, lag = ranked[-1]
    breadth = sum(1 for value, _ in ranked if value > 0)
    as_of = str(lead.get("as_of") or lag.get("as_of") or "")
    participation = "{0}/{1} sectors positive".format(breadth, len(ranked))
    text = (
        "{0} led {1} at {2}; {3} lagged at {4} ({5}; as of {6})."
    ).format(
        lead.get("sector_key") or lead.get("instrument_id"),
        horizon,
        _signed_pct(lead_ret),
        lag.get("sector_key") or lag.get("instrument_id"),
        _signed_pct(lag_ret),
        participation,
        as_of or "—",
    )
    materiality = abs(lead_ret) + abs(lag_ret)
    return [
        Signal(
            metric_key="sector_leadership_{0}".format(horizon.lower()),
            category="Sectors",
            label="Sector leadership",
            value=lead_ret,
            units="fraction",
            change=lead_ret - lag_ret,
            comparison_period=horizon,
            direction=_direction_for(lead_ret, rising_label="leading", falling_label="lagging"),
            observation_date=as_of or None,
            source=str(lead.get("source_id") or "sector_snapshot"),
            quality="eligible" if as_of else "incomplete",
            drilldown_route="sectors",
            text=text,
            materiality=materiality,
        )
    ]


def _rate_signals(rates: dict[str, Any]) -> list[Signal]:
    curve = {row.get("tenor"): row for row in (rates.get("curve") or []) if row.get("tenor")}
    ten = curve.get("10Y")
    if not ten or ten.get("yield_pct") is None:
        return []
    change = _num(ten.get("chg_prev_bps"))
    slope = (rates.get("slopes") or {}).get("2s10s") or (rates.get("slopes") or {}).get("2Y10Y") or {}
    slope_value = _num(slope.get("value") if isinstance(slope, dict) else None)
    slope_chg = _num(slope.get("chg_prev_bps") if isinstance(slope, dict) else None)
    parts = [
        "10Y yield {0}".format(_level(ten.get("yield_pct"), "pct")),
    ]
    if change is not None:
        parts.append("{0} vs prior session".format(_signed(change, "bps")))
    if slope_value is not None:
        shape = "steepening" if (slope_chg or 0) > 0 else ("flattening" if (slope_chg or 0) < 0 else "unchanged shape")
        parts.append("2s10s {0} ({1})".format(_signed(slope_value, "bps"), shape if slope_chg is not None else "level"))
    text = "{0} (observation {1}).".format("; ".join(parts), ten.get("observation_date") or "—")
    materiality = abs(change or 0) / 100.0 + abs(slope_chg or 0) / 100.0
    if materiality == 0 and ten.get("yield_pct") is not None:
        materiality = 0.01
    return [
        Signal(
            metric_key="ust_10y_session",
            category="Rates",
            label="Treasury 10Y",
            value=_num(ten.get("yield_pct")),
            units="pct",
            change=change,
            comparison_period="prior session",
            direction=_direction_for(change, rising_label="yields rising", falling_label="yields falling"),
            observation_date=str(ten.get("observation_date") or "") or None,
            source=str(ten.get("source_id") or "TREASURY"),
            quality="eligible",
            drilldown_route="rates",
            text=text,
            materiality=materiality,
        )
    ]


def _credit_signals(credit: dict[str, Any]) -> list[Signal]:
    wanted = {"ig_broad": "IG OAS", "hy_broad": "HY OAS"}
    parts = []
    as_of = None
    materiality = 0.0
    primary_change = None
    primary_value = None
    for bucket in credit.get("buckets") or []:
        label = wanted.get(bucket.get("bucket"))
        if not label or bucket.get("oas_bps") is None:
            continue
        as_of = as_of or bucket.get("as_of")
        change = _num(bucket.get("change_1d_bps"))
        primary_value = primary_value if primary_value is not None else _num(bucket.get("oas_bps"))
        primary_change = primary_change if primary_change is not None else change
        if change is None:
            parts.append("{0} {1}".format(label, _level(bucket.get("oas_bps"), "bps")))
        else:
            widen = "widening" if change > 0 else ("tightening" if change < 0 else "unchanged")
            parts.append("{0} {1} ({2}, {3})".format(label, _level(bucket.get("oas_bps"), "bps"), _signed(change, "bps"), widen))
            materiality += abs(change) / 50.0
    if not parts:
        return []
    if materiality == 0:
        materiality = 0.01
    return [
        Signal(
            metric_key="credit_oas_session",
            category="Credit",
            label="Credit spreads",
            value=primary_value,
            units="bps",
            change=primary_change,
            comparison_period="prior session",
            direction=_direction_for(primary_change, rising_label="widening", falling_label="tightening"),
            observation_date=str(as_of or "") or None,
            source="ICE_BofA_OAS",
            quality="eligible",
            drilldown_route="credit",
            text="{0} as of {1}.".format("; ".join(parts), as_of or "—"),
            materiality=materiality,
        )
    ]


def _macro_signals(macro: dict[str, Any]) -> list[Signal]:
    categories = macro.get("categories") or {}
    preferred = (
        ("inflation", ("yoy_pct", "mom_pct"), "Inflation"),
        ("labor", ("mom_change", "yoy_change_pp", "chg_prev"), "Labor"),
        ("growth", ("qoq_saar_pct", "yoy_pct", "mom_pct"), "Growth"),
        ("liquidity", ("chg_4w", "wow_change", "chg_prev"), "Liquidity"),
    )
    out: list[Signal] = []
    for category, keys, label in preferred:
        blocks = categories.get(category) or []
        if not blocks:
            continue
        block = blocks[0]
        transforms = block.get("transforms") or {}
        headline = None
        kind = None
        for key in keys:
            if key in transforms and (transforms[key] or {}).get("value") is not None:
                headline = transforms[key]
                kind = key
                break
        latest = block.get("latest") or {}
        if headline is None and latest.get("value") is None:
            continue
        change_val = _num((headline or {}).get("value"))
        units = (headline or {}).get("units") if headline else None
        text = "{0} {1}".format(block.get("label") or block.get("series_id"), _level(latest.get("value"), "") if latest.get("value") is not None else "—")
        if headline is not None:
            text += "; {0} {1}".format(
                str(kind).replace("_", " "),
                _signed(change_val, units) if units in {"bps", "pct", "pp", "fraction"} else _level(change_val, units or ""),
            )
        text += " (observation {0}).".format(latest.get("observation_date") or "—")
        out.append(
            Signal(
                metric_key="macro_{0}".format(category),
                category=label,
                label=str(block.get("label") or category),
                value=_num(latest.get("value")),
                units=units,
                change=change_val,
                comparison_period="latest release",
                direction=None,
                observation_date=str(latest.get("observation_date") or "") or None,
                source="FRED",
                quality="eligible",
                drilldown_route="macro",
                text=text,
                materiality=abs(change_val or 0) / 10.0 + 0.02,
            )
        )
        break
    return out


def _commodity_signals(commodities: list[dict[str, Any]], macro: dict[str, Any]) -> list[Signal]:
    blocks = commodities or (macro.get("categories") or {}).get("commodities") or []
    for block in blocks:
        transforms = block.get("transforms") or {}
        headline = transforms.get("chg_prev") or transforms.get("wow_change") or transforms.get("mom_pct")
        latest = block.get("latest") or {}
        if not headline or headline.get("value") is None:
            continue
        change = _num(headline.get("value"))
        units = headline.get("units")
        cadence = str(block.get("catalog_frequency") or latest.get("frequency") or "release")
        text = "{0} {1}; change {2} ({3} cadence, observation {4}).".format(
            block.get("label") or block.get("series_id"),
            _level(latest.get("value"), ""),
            _signed(change, units) if units in {"bps", "pct", "pp", "fraction"} else _level(change, units or ""),
            cadence,
            latest.get("observation_date") or "—",
        )
        return [
            Signal(
                metric_key="commodity_{0}".format(block.get("series_id") or "level"),
                category="Commodities",
                label=str(block.get("label") or "Commodity"),
                value=_num(latest.get("value")),
                units=units,
                change=change,
                comparison_period=cadence,
                direction=_direction_for(change, rising_label="up", falling_label="down"),
                observation_date=str(latest.get("observation_date") or "") or None,
                source="FRED/EIA",
                quality="eligible",
                drilldown_route="commodities",
                text=text,
                materiality=abs(change or 0) / 100.0 + 0.015,
            )
        ]
    return []


def _order_flow_signals(order_flow: dict[str, Any]) -> list[Signal]:
    rows = (order_flow.get("breadth") or {}).get("rows") or []
    headline = next((row for row in rows if (row.get("product_category") or "").lower() == "all securities"), None)
    if not headline:
        return []
    volume_change = _num(headline.get("volume_change"))
    if volume_change is None and headline.get("total_volume") is None:
        return []
    text = (
        "Corporate bond reported volume (all securities) {0} vs prior session "
        "(session {1}; dealer-reported TRACE aggregate, not live institutional flow)."
    ).format(
        _signed(volume_change, "") if volume_change is not None else _level(headline.get("total_volume"), ""),
        headline.get("observation_date") or "—",
    )
    return [
        Signal(
            metric_key="finra_breadth_volume",
            category="Bond activity",
            label="Bond trading activity",
            value=_num(headline.get("total_volume")),
            units=None,
            change=volume_change,
            comparison_period="prior session",
            direction=_direction_for(volume_change, rising_label="volume up", falling_label="volume down"),
            observation_date=str(headline.get("observation_date") or "") or None,
            source="FINRA_QUERY",
            quality="eligible",
            drilldown_route="order_flow",
            text=text,
            materiality=(abs(volume_change) / 1e9 if volume_change is not None else 0.01),
        )
    ]


def _options_signals(options: dict[str, Any]) -> list[Signal]:
    symbols = options.get("symbols") or []
    if not symbols:
        return []
    row = symbols[0]
    iv = _num(row.get("iv_30d"))
    if iv is None:
        return []
    text = "{0} 30D ATM IV {1} (session {2}; {3}).".format(
        row.get("underlying_symbol") or "Underlying",
        _level(iv * 100 if iv <= 3 else iv, "pct"),
        row.get("session_date") or "—",
        row.get("delay_label") or "stored snapshot",
    )
    return [
        Signal(
            metric_key="options_atm_iv",
            category="Options",
            label="ATM IV",
            value=iv,
            units="pct",
            change=None,
            comparison_period="latest snapshot",
            direction=None,
            observation_date=str(row.get("session_date") or row.get("observation_date") or "") or None,
            source=str(row.get("source_id") or "options_snapshot"),
            quality="eligible",
            drilldown_route="options",
            text=text,
            materiality=0.02,
        )
    ]


def credit_sector_coverage(credit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Honest coverage report: ICE BofA OAS buckets are not sector observations."""
    buckets = list((credit or {}).get("buckets") or [])
    series_ids = [str(row.get("series_id")) for row in buckets if row.get("series_id")]
    return {
        "status": "UNAVAILABLE",
        "sector_oas_available": False,
        "subsector_oas_available": False,
        "available_series": series_ids,
        "available_dimensions": ["broad_market", "rating_bucket"],
        "missing_inputs": [
            "No provider-published sector/subsector OAS series are stored.",
            "mi_bond_securities has no source-backed sector classification column.",
            "Individual bond quotes/ratings are not entitlement-complete for a display-quality sample aggregate.",
        ],
        "note": (
            "The nine stored ICE BofA OAS series are broad IG/HY and rating buckets only. "
            "They cannot populate a sector heatmap. Sector & subsector views stay capability-aware until "
            "a verified sector OAS feed or a documented bond-sample aggregation meets minimum coverage rules."
        ),
    }


__all__ = [
    "ROUTE_BY_AREA",
    "Signal",
    "build_what_matters",
    "credit_sector_coverage",
]
