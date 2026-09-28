"""CFTC Traders in Financial Futures and Disaggregated futures-only positions.

Contract codes were read from the public Socrata datasets on 2026-09-28.
Matching uses ``cftc_contract_market_code`` plus the exact market name.
There is no publication timestamp on the returned record. A Tuesday position
date maps to the following Friday only as the CFTC regular weekly release
schedule. Any other weekday leaves the scheduled publication date empty.

Net contracts are long minus short. Net/OI is that net divided by open
interest, in percent. Open interest of zero or missing stays missing.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence


TFF_RESOURCE = "gpe5-46if"
DISAGG_RESOURCE = "72hh-3qpy"
TFF_FAMILY = "TFF_FUTONLY"
DISAGG_FAMILY = "DISAGG_FUTONLY"
MIN_PERCENTILE_1Y = 40
MIN_PERCENTILE_3Y = 100
MIN_ZSCORE = 52
PRICE_LOOKBACK_DAYS = 5

TFF_CATEGORIES = (
    ("dealer", "Dealer / Intermediary", "dealer_positions_long_all", "dealer_positions_short_all"),
    ("asset_manager", "Asset Manager / Institutional", "asset_mgr_positions_long", "asset_mgr_positions_short"),
    ("leveraged_funds", "Leveraged Funds", "lev_money_positions_long", "lev_money_positions_short"),
    ("tff_other", "Other Reportables (financial)", "other_rept_positions_long", "other_rept_positions_short"),
)
DISAGG_CATEGORIES = (
    ("producer_merchant", "Producer / Merchant / Processor / User", "prod_merc_positions_long", "prod_merc_positions_short"),
    ("swap_dealer", "Swap Dealer", "swap_positions_long_all", "swap__positions_short_all"),
    ("managed_money", "Managed Money", "m_money_positions_long_all", "m_money_positions_short_all"),
    ("disagg_other", "Other Reportables (physical)", "other_rept_positions_long", "other_rept_positions_short"),
)
CATEGORY_LABELS = {key: label for key, label, _long, _short in TFF_CATEGORIES + DISAGG_CATEGORIES}

POSITIONING_METHODOLOGY = (
    "Financial futures use CFTC Traders in Financial Futures, futures-only dataset gpe5-46if. "
    "Physical commodities use Disaggregated Commitments of Traders, futures-only dataset 72hh-3qpy. "
    "Legacy futures-only rows remain stored and are not shown on this page. "
    "Net contracts = long - short. Net/OI = (long - short) / open interest * 100. "
    "Missing open interest, or open interest of zero, leaves Net/OI missing. "
    "1-week change is the current Net/OI minus the prior report. "
    "4-week change is the current Net/OI minus the value four report periods earlier. "
    "Both changes are percentage points of Net/OI. "
    "1-year and 3-year percentiles rank the current Net/OI against that category's own earlier observations in the window, including the current report. "
    "They are missing until 40 weekly observations exist inside one year, or 100 inside three years. "
    "The z-score uses the expanding history of that same series and is missing until 52 observations exist or the standard deviation is zero. "
    "A percentile is not a bullish or bearish signal. "
    "Position date is report_date_as_yyyy_mm_dd. "
    "The record has no publication timestamp. When the position date is a Tuesday, the scheduled publication date is the following Friday, which is the CFTC regular weekly release day. "
    "Price versus positioning uses a verified proxy aligned to the position date. "
    "An exact price date is preferred. Otherwise the latest stored price on or before the position date, within five calendar days, is used. Prices after the position date are never used, and the publication Friday is not the alignment date."
)


@dataclass(frozen=True)
class CftcContract:
    market_key: str
    label: str
    asset_group: str
    report_family: str
    resource: str
    contract_code: str
    market_name: str
    price_proxy: str | None
    price_note: str


CONTRACTS: tuple[CftcContract, ...] = (
    CftcContract("spx", "S&P 500", "Equities", TFF_FAMILY, TFF_RESOURCE, "13874A", "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE", "SPY", "SPY adjusted close from stored Yahoo market-monitor history. ETF proxy, not the E-mini contract."),
    CftcContract("nasdaq", "Nasdaq", "Equities", TFF_FAMILY, TFF_RESOURCE, "209742", "NASDAQ MINI - CHICAGO MERCANTILE EXCHANGE", "QQQ", "QQQ adjusted close from stored Yahoo market-monitor history. ETF proxy, not the Nasdaq mini contract."),
    CftcContract("russell", "Russell 2000", "Equities", TFF_FAMILY, TFF_RESOURCE, "239742", "RUSSELL E-MINI - CHICAGO MERCANTILE EXCHANGE", "IWM", "IWM adjusted close from stored Yahoo market-monitor history. ETF proxy, not the Russell E-mini."),
    CftcContract("ust2", "2Y Treasury", "Rates", TFF_FAMILY, TFF_RESOURCE, "042601", "UST 2Y NOTE - CHICAGO BOARD OF TRADE", None, "No futures price is stored for this contract. A Treasury yield is not used as a price."),
    CftcContract("ust5", "5Y Treasury", "Rates", TFF_FAMILY, TFF_RESOURCE, "044601", "UST 5Y NOTE - CHICAGO BOARD OF TRADE", None, "No futures price is stored for this contract. A Treasury yield is not used as a price."),
    CftcContract("ust10", "10Y Treasury", "Rates", TFF_FAMILY, TFF_RESOURCE, "043602", "UST 10Y NOTE - CHICAGO BOARD OF TRADE", None, "No futures price is stored for this contract. A Treasury yield is not used as a price."),
    CftcContract("ust30", "30Y Treasury", "Rates", TFF_FAMILY, TFF_RESOURCE, "020601", "UST BOND - CHICAGO BOARD OF TRADE", None, "No futures price is stored for this contract. A Treasury yield is not used as a price."),
    CftcContract("eur", "EUR", "FX", TFF_FAMILY, TFF_RESOURCE, "099741", "EURO FX - CHICAGO MERCANTILE EXCHANGE", "EURUSD", "Standardized EUR versus USD from Yahoo EURUSD=X. CME euro FX futures are a different contract."),
    CftcContract("jpy", "JPY", "FX", TFF_FAMILY, TFF_RESOURCE, "097741", "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE", "USDJPY", "Standardized JPY versus USD, the inverse of Yahoo USDJPY=X."),
    CftcContract("gbp", "GBP", "FX", TFF_FAMILY, TFF_RESOURCE, "096742", "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE", "GBPUSD", "Standardized GBP versus USD from Yahoo GBPUSD=X."),
    CftcContract("cad", "CAD", "FX", TFF_FAMILY, TFF_RESOURCE, "090741", "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE", "USDCAD", "Standardized CAD versus USD, the inverse of Yahoo USDCAD=X."),
    CftcContract("aud", "AUD", "FX", TFF_FAMILY, TFF_RESOURCE, "232741", "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE", "AUDUSD", "Standardized AUD versus USD from Yahoo AUDUSD=X."),
    CftcContract("chf", "CHF", "FX", TFF_FAMILY, TFF_RESOURCE, "092741", "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE", "USDCHF", "Standardized CHF versus USD, the inverse of Yahoo USDCHF=X."),
    CftcContract("wti", "WTI Crude", "Commodities", DISAGG_FAMILY, DISAGG_RESOURCE, "06765A", "WTI FINANCIAL CRUDE OIL - NEW YORK MERCANTILE EXCHANGE", "CL", "Yahoo CL=F futures proxy. Not the CFTC contract's official settlement."),
    CftcContract("natgas", "Natural Gas", "Commodities", DISAGG_FAMILY, DISAGG_RESOURCE, "023651", "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE", "NG", "Yahoo NG=F futures proxy. Not the CFTC contract's official settlement."),
    CftcContract("gold", "Gold", "Commodities", DISAGG_FAMILY, DISAGG_RESOURCE, "088691", "GOLD - COMMODITY EXCHANGE INC.", "GC", "Yahoo GC=F futures proxy. Not the CFTC contract's official settlement."),
    CftcContract("silver", "Silver", "Commodities", DISAGG_FAMILY, DISAGG_RESOURCE, "084691", "SILVER - COMMODITY EXCHANGE INC.", "SI", "Yahoo SI=F futures proxy. Not the CFTC contract's official settlement."),
    CftcContract("copper", "Copper", "Commodities", DISAGG_FAMILY, DISAGG_RESOURCE, "085692", "COPPER- #1 - COMMODITY EXCHANGE INC.", "HG", "Yahoo HG=F futures proxy. Not the CFTC contract's official settlement."),
    CftcContract("vix", "VIX", "Volatility", TFF_FAMILY, TFF_RESOURCE, "1170E1", "VIX FUTURES - CBOE FUTURES EXCHANGE", "VIX", "Yahoo ^VIX spot. Positioning is VIX futures, not this spot index."),
)
CONTRACT_BY_CODE = {row.contract_code: row for row in CONTRACTS}
CONTRACT_BY_KEY = {row.market_key: row for row in CONTRACTS}
ASSET_GROUPS = ("All", "Equities", "Rates", "FX", "Commodities", "Volatility")


def scheduled_publication_date(position_date: date) -> date | None:
    """Friday after a Tuesday position date. Otherwise unknown."""
    if position_date.weekday() != 1:
        return None
    return position_date + timedelta(days=3)


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _position_date(raw: Mapping[str, Any]) -> date | None:
    text = str(raw.get("report_date_as_yyyy_mm_dd") or "")[:10]
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_report_rows(raw_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Long-form category rows. Identity mismatches are rejected, not guessed."""
    parsed: list[dict[str, Any]] = []
    rejected = 0
    for raw in raw_rows:
        code = str(raw.get("cftc_contract_market_code") or "").strip()
        contract = CONTRACT_BY_CODE.get(code)
        name = str(raw.get("market_and_exchange_names") or "")
        position = _position_date(raw)
        if contract is None or position is None or name != contract.market_name:
            rejected += 1
            continue
        categories = TFF_CATEGORIES if contract.report_family == TFF_FAMILY else DISAGG_CATEGORIES
        open_interest = _int(raw.get("open_interest_all"))
        published = scheduled_publication_date(position)
        for key, _label, long_field, short_field in categories:
            parsed.append(
                {
                    "report_family": contract.report_family,
                    "cftc_contract_market_code": code,
                    "market_key": contract.market_key,
                    "market_name": contract.market_name,
                    "asset_group": contract.asset_group,
                    "position_date": position,
                    "scheduled_publication_date": published,
                    "trader_category": key,
                    "long_contracts": _int(raw.get(long_field)),
                    "short_contracts": _int(raw.get(short_field)),
                    "open_interest": open_interest,
                }
            )
    return parsed, rejected


def net_contracts(long_contracts: int | None, short_contracts: int | None) -> int | None:
    if long_contracts is None or short_contracts is None:
        return None
    return int(long_contracts) - int(short_contracts)


def net_oi_pct(long_contracts: int | None, short_contracts: int | None, open_interest: int | None) -> float | None:
    net = net_contracts(long_contracts, short_contracts)
    if net is None or open_interest is None or open_interest == 0:
        return None
    return (net / float(open_interest)) * 100.0


def _percentile(values: Sequence[float], current: float) -> float:
    if not values:
        return 0.0
    rank = sum(1 for value in values if value <= current)
    return 100.0 * rank / len(values)


def _zscore_moments(total: float, total_sq: float, count: int, current: float) -> float | None:
    """Sample z-score of ``current`` inside the expanding window that produced the moments.

    Variance is the sum of squared deviations from the mean, divided by count minus one.
    A zero standard deviation stays missing.
    """
    if count < 2:
        return None
    variance = (total_sq - (total * total) / count) / (count - 1)
    if variance <= 0:
        return None
    return (current - (total / count)) / math.sqrt(variance)


def enrich_category_history(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Chronological metrics. Each row uses only observations on or before its date."""
    ordered = sorted(points, key=lambda row: row["position_date"])
    enriched: list[dict[str, Any]] = []
    pcts: list[float | None] = []
    start_1y = 0
    start_3y = 0
    moment_n = 0
    moment_sum = 0.0
    moment_sq = 0.0
    for row in ordered:
        current_pct = net_oi_pct(row.get("long_contracts"), row.get("short_contracts"), row.get("open_interest"))
        prior = pcts[-1] if pcts else None
        four = pcts[-4] if len(pcts) >= 4 else None
        cutoff_1y = row["position_date"] - timedelta(days=365)
        cutoff_3y = row["position_date"] - timedelta(days=365 * 3)
        while start_1y < len(enriched) and enriched[start_1y]["position_date"] < cutoff_1y:
            start_1y += 1
        while start_3y < len(enriched) and enriched[start_3y]["position_date"] < cutoff_3y:
            start_3y += 1
        window_1y = [value for value in pcts[start_1y:] if value is not None]
        window_3y = [value for value in pcts[start_3y:] if value is not None]
        if current_pct is not None:
            window_1y.append(current_pct)
            window_3y.append(current_pct)
            moment_n += 1
            moment_sum += current_pct
            moment_sq += current_pct * current_pct
        pcts.append(current_pct)
        enriched.append(
            {
                **row,
                "net_contracts": net_contracts(row.get("long_contracts"), row.get("short_contracts")),
                "net_oi_pct": current_pct,
                "change_1w": None if current_pct is None or prior is None else current_pct - prior,
                "change_4w": None if current_pct is None or four is None else current_pct - four,
                "percentile_1y": _percentile(window_1y, current_pct) if current_pct is not None and len(window_1y) >= MIN_PERCENTILE_1Y else None,
                "percentile_3y": _percentile(window_3y, current_pct) if current_pct is not None and len(window_3y) >= MIN_PERCENTILE_3Y else None,
                "zscore": _zscore_moments(moment_sum, moment_sq, moment_n, current_pct) if current_pct is not None and moment_n >= MIN_ZSCORE else None,
            }
        )
    return enriched


def positive_price_days(prices: Mapping[date, float]) -> list[date]:
    """Ascending dates whose price is present and positive."""
    return sorted(day for day, value in prices.items() if value is not None and value > 0)


def align_price_to_position(
    position_date: date,
    prices: Mapping[date, float],
    *,
    lookback_days: int = PRICE_LOOKBACK_DAYS,
    ordered_days: Sequence[date] | None = None,
) -> tuple[date, float] | None:
    """Exact position date, else the latest price on or before it within the lookback.

    Never returns a price after the position date. ``ordered_days`` is the sorted
    positive-price calendar from :func:`positive_price_days` so each weekly row
    does not rescan the whole price history.
    """
    exact = prices.get(position_date)
    if exact is not None and exact > 0:
        return position_date, exact
    days = ordered_days if ordered_days is not None else positive_price_days(prices)
    index = bisect_left(days, position_date) - 1
    if index < 0:
        return None
    day = days[index]
    if day >= position_date or (position_date - day).days > lookback_days:
        return None
    value = prices.get(day)
    if value is None or value <= 0:
        return None
    return day, value
