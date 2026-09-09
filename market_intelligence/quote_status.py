"""Derive IBKR quote messaging from PostgreSQL only. Never calls IBKR or TWS.

A CONNECTED collector is not proof of fresh quotes. Freshness uses the stored
quote / callback / ingest timestamps and the declared delay class.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from market_intelligence.freshness import assess_freshness

# Collector heartbeat older than this is already COLLECTOR_OFFLINE in the SQL view.
QUOTE_RECENT_SECONDS = 15 * 60
FRESH_QUOTE_HOURS = 24

LIVE_TYPES = {"LIVE"}
DELAYED_TYPES = {"DELAYED", "DELAYED_FROZEN"}
FROZEN_TYPES = {"FROZEN", "DELAYED_FROZEN"}

CODE_NO_SOURCE = "NO_SOURCE"
CODE_FRESH = "FRESH_QUOTES"
CODE_CONNECTED_STALE = "CONNECTED_NO_RECENT"
CODE_OFFLINE_CACHED = "OFFLINE_CACHED"
CODE_OFFLINE_EMPTY = "OFFLINE_EMPTY"
CODE_CONNECTED_EMPTY = "CONNECTED_EMPTY"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: Any, *, now: datetime) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _quote_receipt(row: dict[str, Any]) -> datetime | None:
    for key in ("last_callback_at", "quote_ts", "retrieved_at", "last_quote_at"):
        parsed = _parse_dt(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _delay_label(quotes: list[dict[str, Any]]) -> str:
    types = {(str(row.get("market_data_type") or row.get("delay_status") or "")).upper() for row in quotes}
    types.discard("")
    if types & LIVE_TYPES and not (types & DELAYED_TYPES) and not (types & FROZEN_TYPES):
        return "live"
    if types & DELAYED_TYPES:
        return "delayed"
    if types & FROZEN_TYPES:
        return "frozen"
    return "stored"


def _collector_connected(collectors: list[dict[str, Any]]) -> bool | None:
    if not collectors:
        return None
    for row in collectors:
        observed = str(row.get("observed_state") or "").upper()
        reported = str(row.get("reported_state") or "").upper()
        if observed == "COLLECTOR_OFFLINE":
            continue
        if observed in {"CONNECTED", "COLLECTOR_ACTIVE"} or reported in {"CONNECTED", "COLLECTOR_ACTIVE"}:
            return True
        if observed and observed != "COLLECTOR_OFFLINE":
            return True
    return False


def _newest_quote_age(quotes: list[dict[str, Any]], collectors: list[dict[str, Any]], *, now: datetime) -> float | None:
    ages = []
    for row in quotes:
        age = _age_seconds(_quote_receipt(row), now=now)
        if age is not None:
            ages.append(age)
    for row in collectors:
        for key in ("last_quote_at", "last_callback_at", "last_ingest_ok_at"):
            age = _age_seconds(row.get(key), now=now)
            if age is not None:
                ages.append(age)
    return min(ages) if ages else None


def _coverage(quotes: list[dict[str, Any]], collectors: list[dict[str, Any]]) -> dict[str, Any]:
    watch = []
    for row in collectors:
        raw = row.get("watchlist_json")
        if isinstance(raw, list):
            watch.extend(str(item) for item in raw)
        elif isinstance(raw, dict):
            watch.extend(str(item) for item in (raw.get("instruments") or raw.get("symbols") or []))
    instruments = [str(row.get("instrument_id") or row.get("display_name") or "") for row in quotes]
    instruments = [item for item in instruments if item]
    return {
        "quote_instruments": len(set(instruments)),
        "watchlist_instruments": len(set(watch)) if watch else None,
        "instruments": sorted(set(instruments)),
    }


def derive_quote_status(
    *,
    collectors: list[dict[str, Any]] | None = None,
    quotes: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a concise, accurate quote-source state from stored collector/quote rows."""
    now = now or datetime.now(timezone.utc)
    collectors = list(collectors or [])
    quotes = list(quotes or [])
    connected = _collector_connected(collectors)
    newest_age = _newest_quote_age(quotes, collectors, now=now)
    coverage = _coverage(quotes, collectors)
    delay = _delay_label(quotes) if quotes else None
    last_ingest = None
    last_heartbeat = None
    last_quote = None
    for row in collectors:
        last_ingest = last_ingest or row.get("last_ingest_ok_at")
        last_heartbeat = last_heartbeat or row.get("last_heartbeat_at")
        last_quote = last_quote or row.get("last_quote_at")
    if quotes:
        receipt = _quote_receipt(max(quotes, key=lambda row: _quote_receipt(row) or datetime.min.replace(tzinfo=timezone.utc)))
        last_quote = last_quote or (receipt.isoformat() if receipt else None)

    fresh_quotes = bool(quotes) and newest_age is not None and newest_age <= FRESH_QUOTE_HOURS * 3600
    recent_quotes = bool(quotes) and newest_age is not None and newest_age <= QUOTE_RECENT_SECONDS

    if connected is None and not quotes:
        code = CODE_NO_SOURCE
        headline = "No live quote source has reported."
        detail = "Prior-session closes are not labeled as overnight."
    elif connected is True and fresh_quotes:
        code = CODE_FRESH
        headline = "Fresh {0} quotes available.".format(delay or "stored")
        detail = "{0} instrument(s) stored. A connected collector is not by itself proof of freshness.".format(
            coverage["quote_instruments"]
        )
    elif connected is True and quotes and not fresh_quotes:
        code = CODE_CONNECTED_STALE
        headline = "Collector connected; no recent quotes."
        detail = "Last stored quote age is {0:.0f} minutes. Showing last available quotes.".format(
            (newest_age or 0) / 60.0
        )
    elif connected is True and not quotes:
        code = CODE_CONNECTED_EMPTY
        headline = "Collector connected; no recent quotes."
        detail = "Heartbeat is current, but no quote rows have been persisted."
    elif quotes:
        code = CODE_OFFLINE_CACHED
        headline = "Collector offline; last available quotes shown."
        detail = "Last stored quote age is {0:.0f} minutes.".format((newest_age or 0) / 60.0) if newest_age is not None else "Stored quotes are available; collector heartbeat is offline."
    else:
        code = CODE_OFFLINE_EMPTY
        headline = "Collector offline; no quotes stored."
        detail = "FRED and FINRA do not depend on this laptop collector."

    quote_date = None
    if quotes:
        receipt = _quote_receipt(quotes[0])
        quote_date = receipt.date() if receipt else None
    freshness = assess_freshness(quote_date, "INTRADAY", now.date()) if quote_date else None

    return {
        "code": code,
        "headline": headline,
        "detail": detail,
        "collector_connected": connected,
        "quote_count": len(quotes),
        "newest_quote_age_seconds": newest_age,
        "fresh_quotes": fresh_quotes,
        "recent_quotes": recent_quotes,
        "delay_class": delay,
        "coverage": coverage,
        "last_ingest_ok_at": last_ingest,
        "last_heartbeat_at": last_heartbeat,
        "last_quote_at": last_quote,
        "intraday_freshness": freshness.status if freshness else None,
        "calls_ibkr": False,
    }


def overview_caption(status: dict[str, Any]) -> str:
    headline = str(status.get("headline") or "").strip()
    detail = str(status.get("detail") or "").strip()
    if status.get("code") == CODE_NO_SOURCE:
        return "{0} {1}".format(headline, detail).strip()
    if detail:
        return "{0} {1}".format(headline, detail).strip()
    return headline


def morning_overnight_section(status: dict[str, Any]) -> dict[str, Any]:
    code = status.get("code")
    if code == CODE_FRESH:
        section_status = "OK"
    elif code in {CODE_CONNECTED_STALE, CODE_OFFLINE_CACHED}:
        section_status = "PARTIAL"
    else:
        section_status = "UNAVAILABLE"
    return {
        "status": section_status,
        "reason": overview_caption(status),
        "code": code,
        "delay_class": status.get("delay_class"),
        "quote_count": status.get("quote_count"),
        "collector_connected": status.get("collector_connected"),
    }


def exception_note(row: dict[str, Any]) -> str:
    """Plain-language cause for a Data Health exception. Does not loosen thresholds."""
    source = str(row.get("source_id") or "")
    dataset = str(row.get("freshness_dataset") or row.get("dataset") or "")
    transport = str(row.get("transport_status") or "").upper()
    access = str(row.get("access_status") or "").upper()
    freshness = str(row.get("freshness_status") or "").upper()
    cadence = str(row.get("dataset_cadence") or row.get("cadence") or "")
    error = str(row.get("last_error_redacted") or "")
    capability = str(row.get("capability_status") or "").upper()
    joined = " ".join((source, dataset, error, capability, access)).upper()

    if "TRACE" in joined and ("INDIVIDUAL" in joined or "ENTITLEMENT" in joined or transport in {"FAILED", "METADATA_REJECTED"}):
        return "Individual TRACE is an entitlement/capability limit. Aggregate Query API rows are a different dataset."
    if source == "IBKR_MARKET_DATA" or "IBKR" in source:
        return "Windows collector / TWS path. CONNECTED is not proof of fresh quotes; FRED and FINRA do not depend on this laptop."
    if transport in {"FAILED", "METADATA_REJECTED"} and "403" in error:
        return "Last retrieval was rejected by the provider (entitlement or authorization). Stored values were not overwritten."
    if freshness == "STALE":
        cadence_bit = " ({0} cadence)".format(cadence) if cadence else ""
        return "Latest observation is older than the configured freshness tolerance{0}. Thresholds were not loosened.".format(cadence_bit)
    if transport == "PARTIAL":
        return "Last ingest completed with rejected or incomplete rows. Checkpoint does not claim full coverage."
    if error:
        return error[:160]
    return "See transport status and latest observation date."
