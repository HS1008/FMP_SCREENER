"""U.S. Treasury daily interest-rate XML feed (not Fiscal Data JSON).

Documented pattern:
https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=YYYYMM

Real curve uses data=daily_treasury_real_yield_curve.

This is the official daily XML feed of constant-maturity par yields. It is not
average debt interest, auction yields, or Fiscal Data accounting rates.
Observation dates are the economic quotation date (indicative quotes near
15:30 ET). The feed does not publish a guaranteed API timestamp.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

FEED_PAGE = "https://home.treasury.gov/treasury-daily-interest-rate-xml-feed"
BASE_XML = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
NOMINAL_DATA = "daily_treasury_yield_curve"
REAL_DATA = "daily_treasury_real_yield_curve"
USER_AGENT = "FMP-MarketIntelligence/treasury-xml (+https://github.com/hs1008/fmp_screener)"

# Provider-native field -> (canonical series_id, FRED equivalent or None, tenor label)
NOMINAL_FIELDS: dict[str, tuple[str, str | None, str]] = {
    "BC_1MONTH": ("UST_NOM_1M", "DGS1MO", "1M"),
    "BC_1_5MONTH": ("UST_NOM_1_5M", None, "1.5M"),
    "BC_2MONTH": ("UST_NOM_2M", None, "2M"),
    "BC_3MONTH": ("UST_NOM_3M", "DGS3MO", "3M"),
    "BC_4MONTH": ("UST_NOM_4M", None, "4M"),
    "BC_6MONTH": ("UST_NOM_6M", "DGS6MO", "6M"),
    "BC_1YEAR": ("UST_NOM_1Y", "DGS1", "1Y"),
    "BC_2YEAR": ("UST_NOM_2Y", "DGS2", "2Y"),
    "BC_3YEAR": ("UST_NOM_3Y", "DGS3", "3Y"),
    "BC_5YEAR": ("UST_NOM_5Y", "DGS5", "5Y"),
    "BC_7YEAR": ("UST_NOM_7Y", "DGS7", "7Y"),
    "BC_10YEAR": ("UST_NOM_10Y", "DGS10", "10Y"),
    "BC_20YEAR": ("UST_NOM_20Y", "DGS20", "20Y"),
    "BC_30YEAR": ("UST_NOM_30Y", "DGS30", "30Y"),
}

REAL_FIELDS: dict[str, tuple[str, str | None, str]] = {
    "TC_5YEAR": ("UST_REAL_5Y", "DFII5", "5Y"),
    "TC_7YEAR": ("UST_REAL_7Y", None, "7Y"),
    "TC_10YEAR": ("UST_REAL_10Y", "DFII10", "10Y"),
    "TC_20YEAR": ("UST_REAL_20Y", "DFII20", "20Y"),
    "TC_30YEAR": ("UST_REAL_30Y", "DFII30", "30Y"),
}

# Complete contemporaneous nominal curve used for slopes (same observation date).
COMPLETE_NOMINAL_TENORS = ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y")


@dataclass(frozen=True)
class TreasuryPoint:
    observation_date: date
    field_name: str
    series_id: str
    fred_equivalent: str | None
    tenor: str
    value: Decimal | None
    raw_value: str | None
    curve: str  # nominal | real


@dataclass
class TreasuryFeedPage:
    month: str
    data: str
    points: list[TreasuryPoint] = field(default_factory=list)
    next_url: str | None = None
    raw_fields: tuple[str, ...] = ()


def month_key(year: int, month: int) -> str:
    return "{0:04d}{1:02d}".format(year, month)


def feed_url(data: str, yyyymm: str) -> str:
    return "{0}?data={1}&field_tdr_date_value_month={2}".format(BASE_XML, data, yyyymm)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    text = el.text.strip()
    return text or None


def _is_null(el: ET.Element) -> bool:
    for key, value in el.attrib.items():
        if _local(key).lower() == "null" and str(value).lower() == "true":
            return True
    return False


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_number(raw: str | None) -> tuple[Decimal | None, str | None]:
    if raw is None:
        return None, None
    token = raw.strip()
    if token in {"", ".", "N/A", "NA", "null", "ND"}:
        return None, token
    try:
        return Decimal(token), token
    except Exception:
        return None, token


def parse_feed_xml(payload: str, *, curve: str, field_map: MappingFields | None = None) -> TreasuryFeedPage:
    """Parse one Atom/OData or bare XML page. Unknown tenors are recorded, not zeroed."""
    field_map = field_map or (NOMINAL_FIELDS if curve == "nominal" else REAL_FIELDS)
    root = ET.fromstring(payload)
    points: list[TreasuryPoint] = []
    seen_fields: set[str] = set()
    next_url = None
    for el in root.iter():
        if _local(el.tag) == "link" and el.attrib.get("rel") == "next":
            href = el.attrib.get("href")
            if href:
                next_url = href
    for entry in root.iter():
        if _local(entry.tag) != "entry" and _local(entry.tag) != "item":
            continue
        props = None
        for child in entry.iter():
            if _local(child.tag) in {"properties", "content"} or _local(child.tag).endswith("properties"):
                if any(_local(g.tag).startswith("BC_") or _local(g.tag).startswith("TC_") or _local(g.tag) in {"NEW_DATE", "INDEX_DATE"} for g in child):
                    props = child
                    break
        if props is None:
            props = entry
        obs = None
        values: dict[str, ET.Element] = {}
        for child in list(props):
            name = _local(child.tag)
            seen_fields.add(name)
            if name in {"NEW_DATE", "INDEX_DATE", "Date", "NEW_DATE"}:
                obs = _parse_date(_text(child)) or obs
            values[name] = child
        if obs is None:
            continue
        for field_name, (series_id, fred_eq, tenor) in field_map.items():
            el = values.get(field_name)
            if el is None:
                continue
            if _is_null(el):
                value, raw = None, _text(el)
            else:
                value, raw = _parse_number(_text(el))
            points.append(TreasuryPoint(obs, field_name, series_id, fred_eq, tenor, value, raw, curve))
        extra = [name for name in values if re.match(r"^(BC_|TC_)", name) and name not in field_map and name != "BC_30YEARDISPLAY"]
        for name in extra:
            el = values[name]
            raw = _text(el)
            value = None if _is_null(el) else _parse_number(raw)[0]
            points.append(
                TreasuryPoint(
                    obs,
                    name,
                    "UST_{0}_{1}".format(curve.upper(), name),
                    None,
                    name,
                    value,
                    raw,
                    curve,
                )
            )
    return TreasuryFeedPage(month="", data=curve, points=points, next_url=next_url, raw_fields=tuple(sorted(seen_fields)))


# typing alias used above
MappingFields = dict[str, tuple[str, str | None, str]]


def months_to_fetch(*, today: date, lookback_months: int = 2) -> list[str]:
    """Current month plus prior months so month/year rollover is covered. No hardcoded year."""
    year, month = today.year, today.month
    out: list[str] = []
    for _ in range(max(1, lookback_months)):
        out.append(month_key(year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return out


def fetch_url(url: str, *, timeout: int = 30) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


class TreasuryXmlClient:
    def __init__(self, *, opener=None, timeout: int = 30):
        self.opener = opener or fetch_url
        self.timeout = timeout

    def fetch_month(self, data: str, yyyymm: str) -> TreasuryFeedPage:
        url = feed_url(data, yyyymm)
        curve = "real" if "real" in data else "nominal"
        field_map = REAL_FIELDS if curve == "real" else NOMINAL_FIELDS
        page = parse_feed_xml(self.opener(url, timeout=self.timeout), curve=curve, field_map=field_map)
        page.month = yyyymm
        page.data = data
        seen = 0
        while page.next_url and seen < 8:
            nxt = page.next_url
            if not nxt.startswith("http"):
                nxt = urljoin(url, nxt)
            more = parse_feed_xml(self.opener(nxt, timeout=self.timeout), curve=curve, field_map=field_map)
            page.points.extend(more.points)
            page.next_url = more.next_url
            seen += 1
        return page

    def fetch_recent(self, *, today: date, lookback_months: int = 2) -> list[TreasuryPoint]:
        points: list[TreasuryPoint] = []
        for data in (NOMINAL_DATA, REAL_DATA):
            for yyyymm in months_to_fetch(today=today, lookback_months=lookback_months):
                page = self.fetch_month(data, yyyymm)
                points.extend(page.points)
        return points


def latest_complete_curve(points: Iterable[TreasuryPoint], *, curve: str = "nominal") -> tuple[date | None, dict[str, TreasuryPoint], dict[date, dict[str, TreasuryPoint]]]:
    """Same-date curve only. Newer incomplete dates are returned in ``by_date`` separately."""
    by_date: dict[date, dict[str, TreasuryPoint]] = {}
    for point in points:
        if point.curve != curve or point.value is None:
            continue
        by_date.setdefault(point.observation_date, {})[point.tenor] = point
    required = set(COMPLETE_NOMINAL_TENORS) if curve == "nominal" else {"5Y", "10Y", "20Y", "30Y"}
    complete_dates = [d for d, legs in by_date.items() if required <= set(legs)]
    latest = max(complete_dates) if complete_dates else None
    return latest, (by_date.get(latest) or {} if latest else {}), by_date


__all__ = [
    "COMPLETE_NOMINAL_TENORS",
    "NOMINAL_DATA",
    "NOMINAL_FIELDS",
    "REAL_DATA",
    "REAL_FIELDS",
    "TreasuryFeedPage",
    "TreasuryPoint",
    "TreasuryXmlClient",
    "feed_url",
    "latest_complete_curve",
    "months_to_fetch",
    "parse_feed_xml",
]
