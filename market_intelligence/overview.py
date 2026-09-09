"""Deterministic Overview 'what changed' lines from stored analytics.

Factual descriptions only. No causal language, recommendations, or generated commentary.
"""

from __future__ import annotations

from typing import Any


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signed_pct(value: Any, *, digits: int = 2) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return "{0:+.{1}f}%".format(number * 100, digits)


def _signed(value: Any, units: str, *, digits: int = 2) -> str:
    number = _num(value)
    if number is None:
        return "—"
    if units == "bps":
        return "{0:+.0f} bps".format(number)
    if units == "pct":
        return "{0:+.{1}f}%".format(number, digits)
    return "{0:+,.{1}f}".format(number, digits)


def _level(value: Any, units: str, *, digits: int = 2) -> str:
    number = _num(value)
    if number is None:
        return "—"
    if units == "bps":
        return "{0:.0f} bps".format(number)
    if units == "pct":
        return "{0:.{1}f}%".format(number, digits)
    return "{0:,.{1}f}".format(number, digits)


def build_what_changed(
    *,
    rates: dict[str, Any] | None = None,
    credit: dict[str, Any] | None = None,
    sectors: dict[str, Any] | None = None,
    macro: dict[str, Any] | None = None,
    order_flow: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return ordered factual change statements for the Overview page."""
    items: list[dict[str, str]] = []
    items.extend(_sector_changes(sectors or {}))
    items.extend(_rate_changes(rates or {}))
    items.extend(_credit_changes(credit or {}))
    items.extend(_macro_changes(macro or {}))
    items.extend(_order_flow_changes(order_flow or {}))
    return items


def _sector_changes(sectors: dict[str, Any]) -> list[dict[str, str]]:
    rows = list((sectors.get("datasets") or {}).get("ETF_RS_VS_SPY") or [])
    ranked = []
    for row in rows:
        metrics = row.get("metrics") or {}
        rs = _num(metrics.get("rs_chg_1m"))
        ranked.append((rs, row))
    usable = [item for item in ranked if item[0] is not None]
    if not usable:
        return []
    usable.sort(key=lambda item: item[0], reverse=True)
    lead_rs, lead = usable[0]
    lag_rs, lag = usable[-1]
    as_of = lead.get("as_of") or lag.get("as_of") or "—"
    benchmark = lead.get("benchmark") or lag.get("benchmark") or "SPY"
    text = (
        "{0} led 1-month relative strength vs {1} at {2}; {3} lagged at {4} (as of {5})."
    ).format(
        lead.get("sector_key") or lead.get("instrument_id"),
        benchmark,
        _signed_pct(lead_rs),
        lag.get("sector_key") or lag.get("instrument_id"),
        _signed_pct(lag_rs),
        as_of,
    )
    return [{"area": "Sectors", "text": text, "period": "1M relative strength", "detail": "Sectors"}]


def _rate_changes(rates: dict[str, Any]) -> list[dict[str, str]]:
    curve = [row for row in (rates.get("curve") or []) if row.get("tenor") == "10Y" and row.get("yield_pct") is not None]
    if not curve:
        return []
    ten = curve[0]
    change = ten.get("chg_prev_bps")
    period = "vs prior session" if change is not None else "level"
    text = "10-year Treasury yield {0} ({1} {2}; observation {3}).".format(
        _level(ten.get("yield_pct"), "pct"),
        _signed(change, "bps") if change is not None else "no prior-session change stored",
        period if change is not None else "",
        ten.get("observation_date") or "—",
    )
    if change is None:
        text = "10-year Treasury yield {0} (observation {1}); no prior-session change is stored.".format(
            _level(ten.get("yield_pct"), "pct"),
            ten.get("observation_date") or "—",
        )
    slope = (rates.get("slopes") or {}).get("2s10s") or (rates.get("slopes") or {}).get("2Y10Y")
    if isinstance(slope, dict) and slope.get("value") is not None:
        text += " 2s10s slope {0}.".format(_signed(slope.get("value"), slope.get("units") or "bps"))
    return [{"area": "Rates", "text": text, "period": "recent session", "detail": "Rates"}]


def _credit_changes(credit: dict[str, Any]) -> list[dict[str, str]]:
    wanted = {"ig_broad": "Investment-grade OAS", "hy_broad": "High-yield OAS"}
    parts = []
    as_of = None
    for bucket in credit.get("buckets") or []:
        label = wanted.get(bucket.get("bucket"))
        if not label or bucket.get("oas_bps") is None:
            continue
        as_of = as_of or bucket.get("as_of")
        change = bucket.get("change_1d_bps")
        period = "vs prior session" if change is not None else "level"
        if change is None:
            parts.append("{0} {1}".format(label, _level(bucket.get("oas_bps"), "bps")))
        else:
            parts.append("{0} {1} ({2} {3})".format(label, _level(bucket.get("oas_bps"), "bps"), _signed(change, "bps"), period))
    if not parts:
        return []
    text = "{0} as of {1}.".format("; ".join(parts), as_of or "—")
    return [{"area": "Credit", "text": text, "period": "recent session", "detail": "Credit"}]


def _macro_changes(macro: dict[str, Any]) -> list[dict[str, str]]:
    items = []
    categories = macro.get("categories") or {}
    preferred = (
        ("inflation", ("yoy_pct", "mom_pct"), "prior release / YoY"),
        ("labor", ("mom_change", "yoy_change_pp", "chg_prev"), "prior release"),
        ("growth", ("qoq_saar_pct", "yoy_pct", "mom_pct"), "prior release / YoY"),
        ("liquidity", ("chg_4w", "wow_change", "chg_prev"), "comparable reporting period"),
    )
    for category, keys, period in preferred:
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
        change_text = ""
        if headline is not None:
            units = headline.get("units") or ""
            change_text = "; {0} {1}".format(
                kind.replace("_", " "),
                _signed(headline.get("value"), units) if units in {"bps", "pct", "pp", "fraction"} else _level(headline.get("value"), units or ""),
            )
        items.append(
            {
                "area": category.title(),
                "text": "{0} {1}{2} (observation {3}).".format(
                    block.get("label") or block.get("series_id"),
                    _level(latest.get("value"), "") if latest.get("value") is not None else "—",
                    change_text,
                    latest.get("observation_date") or "—",
                ),
                "period": period,
                "detail": "Macro",
            }
        )
        if len(items) >= 3:
            break
    return items


def _order_flow_changes(order_flow: dict[str, Any]) -> list[dict[str, str]]:
    rows = (order_flow.get("breadth") or {}).get("rows") or []
    headline = next((row for row in rows if (row.get("product_category") or "").lower() == "all securities"), None)
    if not headline:
        return []
    volume_change = headline.get("volume_change")
    trade_change = headline.get("trade_count_change")
    if volume_change is None and trade_change is None:
        text = "Corporate bond reported activity (all securities) {0} volume / {1} trades on {2}.".format(
            _level(headline.get("total_volume"), ""),
            _level(headline.get("total_trades"), ""),
            headline.get("observation_date") or "—",
        )
    else:
        text = (
            "Corporate bond reported volume (all securities) {0} vs the prior session; "
            "trade count {1} (session {2})."
        ).format(
            _signed(volume_change, "") if volume_change is not None else "was unchanged or not stored",
            _signed(trade_change, "") if trade_change is not None else "was unchanged or not stored",
            headline.get("observation_date") or "—",
        )
    return [{"area": "Order Flow", "text": text, "period": "recent session", "detail": "Order Flow"}]
